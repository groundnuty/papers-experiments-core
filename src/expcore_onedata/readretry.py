# Provenance: new
"""A bounded, logged retry of idempotent plumbing reads (#185).

On 25.0 a read on a provider has been seen to hang past its timeout twice and then answer in a hundredth of a second on
a fresh connection. A harness that ends an upload or a reset on the first such hang loses the work for nothing, and a
harness that retries everything hides the stalls its instruments exist to record. This module is the rule between the
two, and the client (`OnezoneClient.get`) applies it:

- **What is retried:** a GET, and only when it timed out (`httpx` read, connect or pool timeout, named by the client).
  Three attempts in all, with pauses of 2 s and 5 s between them, each retry on a fresh connection, never a pooled one.
  httpcore closes a connection whose request failed before its answer was whole (`HTTP11Connection.handle_request`
  calls `_response_closed`, which closes unless both sides are done) and the pool drops it, so the stalled connection is
  never handed out again. The pool may still hold other idle connections, which a retry could be given, so a client
  that owns its connection pool sends each retry through a one-shot client of the same settings: its own new
  connection, closed when the answer is in. Tests against a real socket hold both.
- **What is never retried:** any answer (an error status or a 3xx included), a connection error (a provider that is
  down must look down), a write timeout, a PUT, POST, PATCH or DELETE (reading POSTs included), a range read and a
  streamed read. The client takes `retry` on `get` and on the reads that find a target's space (`space_name`,
  `file_space_id`), and `gate_retry` on `mutate`, `read_range` and `read_content`, which applies to their gate's reads
  alone, so no write, no range read and no streamed read can be handed a policy for itself.
- **Who asks:** every call site names its choice (`retry=`, or `gate_retry=` for a mutation's or a range read's gate).
  The harness's own plumbing passes a `ReadRetry`; whatever measures the platform passes `ONE_ATTEMPT`, so that a
  timeout stays the recorded timeout it is. A test holds the choice of every call site
  (`tests/onedata/test_read_retry_sites.py`).
- **The gate:** every mutation and range read first reads the space of its target (`OnezoneClient.mutate`), and a stall
  there would end an upload or a reset as surely as one in the read the caller asked for. The plumbing's `gate_retry`
  tries those reads again, and fails closed all the same: each attempt that is answered decides from its own answer,
  nothing an earlier attempt or call saw is reused (O09), and after the last timeout the target is unread
  (`ScopeUnread`) and nothing was sent.
- **Bounded:** with a `deadline_s` the whole call, its pauses included, stays within it: no retry starts unless its
  pause and `MIN_RETRY_S` more fit in what remains, and a retry's timeout is cut to what remains. The first attempt is
  the caller's own and is made as it always was.
- **Logged and counted:** each timed-out attempt is logged with the seconds it took and how its connection began, and
  for a reused connection how many requests it had served before, so that a per-connection limit can be sized; so is
  the outcome of the attempt after a retry. Each retry is counted by the `ReadRetry` it was made under, so the owner of
  a policy (a provisioner) reports "reads retried: N". A log line names the provider by its label (`zone`,
  `providers[i]`) and holds no path, id or token.

How a connection began is read from httpcore's trace events, which the client asks for on the requests of a retried
read: a connection opened for the request shows a `connection.connect_*` event (`new`); a connection taken from the pool
shows the request being sent and no connect (`reused`); no event at all (a pool timeout, or a transport that is not
httpcore, such as a test's mock) is `unknown`. How many requests a reused connection had served is the pool's request
count for it, which the client reads (`served`) when the request is sent, and only while one connection of the pool is
active, since with several the request's own cannot be told; otherwise it is not seen, and the line says so.

This module imports no HTTP library: the client hands it the exception types it retries on.
"""

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

log = logging.getLogger(__name__)

PLUMBING_ATTEMPTS = 3
PLUMBING_PAUSES_S = (2.0, 5.0)
# The least seconds a deadline must leave a retry's attempt for it to be made at all.
MIN_RETRY_S = 1.0

Trace = Callable[[str, Any], None]
Served = Callable[[], int | None]  # how many requests the connection that carries the request now sent had served


class ReadRetry:
    """How many times a timed-out read is tried, the pauses between, the clock and the sleep, and the count of retries.

    The default is the harness's plumbing policy: three attempts, pauses of 2 s and 5 s. A policy is the owner's, and
    its count is the owner's: a provisioner builds one with its own clock and sleep, so a test's ticker moves with the
    pauses, and reports `retries` as its "reads retried". `ONE_ATTEMPT` never retries, so it never counts."""

    def __init__(
        self,
        attempts: int = PLUMBING_ATTEMPTS,
        pauses_s: Sequence[float] = PLUMBING_PAUSES_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("a read is tried at least once")
        if len(pauses_s) != attempts - 1 or any(p < 0 for p in pauses_s):
            raise ValueError("a retried read has one pause, of zero or more seconds, between each two attempts")
        self.attempts = attempts
        self.pauses_s = tuple(float(p) for p in pauses_s)
        self.clock, self.sleep = clock, sleep
        self._retries = 0
        self._lock = threading.Lock()

    @property
    def retries(self) -> int:
        """How many retries have been made under this policy (a read retried twice counts two)."""
        with self._lock:
            return self._retries

    def count(self) -> None:
        """Count one retry; the loop does, as it commits to it."""
        with self._lock:
            self._retries += 1

    def __repr__(self) -> str:
        return f"ReadRetry(attempts={self.attempts}, pauses_s={self.pauses_s}, retries={self.retries})"


ONE_ATTEMPT = ReadRetry(1, ())  # the choice of whatever measures the platform: one attempt, a timeout stays a timeout


class Exhausted(Exception):
    """A read timed out on every attempt it was given, more than one: `error` is the last timeout and `attempts` how
    many were made. A read given one attempt raises its own timeout, as it always did."""

    def __init__(self, error: BaseException, attempts: int) -> None:
        super().__init__(type(error).__name__)
        self.error = error
        self.attempts = attempts


def connection_state(events: Iterable[str]) -> str:
    """How an attempt's connection began, from httpcore's trace event names: `new` when a connection was opened for it,
    `reused` when a pooled one carried it, `unknown` when no event tells."""
    names = list(events)
    if any(name.startswith("connection.connect_") or name == "connection.start_tls.started" for name in names):
        return "new"
    if any(name.endswith(".send_request_headers.started") for name in names):
        return "reused"
    return "unknown"


class Attempt:
    """What one attempt shows of its connection: the names of httpcore's trace events for it, and, when the request was
    sent on a pooled connection, how many requests that connection had served before it (`served`, asked once)."""

    def __init__(self, served: Served | None = None) -> None:
        self.events: list[str] = []
        self.earlier: int | None = None
        self._served = served

    def trace(self, name: str, info: Any) -> None:
        """The `trace` extension's callback: keep the event's name, never its detail, which names a host."""
        self.events.append(name)
        if name.endswith(".send_request_headers.started") and self.earlier is None and self._served is not None:
            self.earlier = self._served()

    @property
    def connection(self) -> str:
        """How the attempt's connection began, as a log line says it: `new`, `unknown`, or `reused` with how many
        requests it had served before the attempt, when that was seen."""
        state = connection_state(self.events)
        if state != "reused":
            return state
        if self.earlier is None:
            return "reused, earlier requests not seen"
        return f"reused, {self.earlier} earlier request{'' if self.earlier == 1 else 's'} served"


def run_attempts[T](
    send: Callable[[float, Trace, bool], T],
    *,
    retry: ReadRetry,
    label: str,
    timeout_s: float,
    timeouts: tuple[type[BaseException], ...],
    deadline_s: float | None = None,
    served: Served | None = None,
) -> T:
    """Call `send(timeout, trace, fresh)` until it returns, retrying only when it raises one of `timeouts`, as `retry`
    allows.

    `send` makes one request with that timeout and hands each trace event to `trace`; `fresh` is True for a retry, which
    must not be given a connection from the pool. Any other exception propagates at once, after a line when it ended a
    retry. A timeout that ends the attempts is raised as it was, or as `Exhausted` when more than one attempt was made.
    See the module docstring for the rule, the bound and the log."""
    began = retry.clock()
    attempt, timeout = 0, timeout_s
    total = retry.attempts
    while True:
        attempt += 1
        probe = Attempt(served)
        started = retry.clock()
        try:
            answer = send(timeout, probe.trace, attempt > 1)
        except timeouts as error:
            took, connection = retry.clock() - started, probe.connection
            pause = retry.pauses_s[attempt - 1] if attempt < total else None
            room = None if deadline_s is None else deadline_s - (retry.clock() - began)
            if pause is None or (room is not None and room - pause < MIN_RETRY_S):
                log.warning(
                    "%s: a GET timed out after %.2f s on attempt %d of %d (connection %s); giving up",
                    label,
                    took,
                    attempt,
                    total,
                    connection,
                )
                if attempt == 1:
                    raise
                raise Exhausted(error, attempt) from None
            log.warning(
                "%s: a GET timed out after %.2f s on attempt %d of %d (connection %s); retrying in %g s",
                label,
                took,
                attempt,
                total,
                connection,
                pause,
            )
            retry.count()
            retry.sleep(pause)
            timeout = timeout_s if room is None else min(timeout_s, room - pause)
            continue
        except Exception as error:
            if attempt > 1:
                log.warning(
                    "%s: a GET failed (%s) on attempt %d of %d after %.2f s (connection %s)",
                    label,
                    type(error).__name__,
                    attempt,
                    total,
                    retry.clock() - started,
                    probe.connection,
                )
            raise
        if attempt > 1:
            log.info(
                "%s: a GET was answered with %s on attempt %d of %d after %.2f s (connection %s)",
                label,
                getattr(answer, "status_code", "no status"),
                attempt,
                total,
                retry.clock() - started,
                probe.connection,
            )
        return answer
