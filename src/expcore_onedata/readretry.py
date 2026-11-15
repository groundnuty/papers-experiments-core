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
  down must look down) unless the policy declares `transport` (below), a write timeout, a PUT, POST, PATCH or DELETE
  (reading POSTs included), a range read and a streamed read. The client takes `retry` on `get` and on the reads that
  find a target's space (`space_name`, `file_space_id`), and `gate_retry` on `mutate`, `read_range` and
  `read_content`, which applies to their gate's reads alone, so no write, no range read and no streamed read can be
  handed a policy for itself.
- **The reset's per-file reads also retry a connection failure** (O46, O51). A reset reads tens of thousands of block
  maps and summaries, and one `GET` that failed to connect after 8,290 good ones failed a live reset closed, 1,238 s
  lost. Those reads are plumbing, not a measurement of the provider, so their policy (`transport=True`, made for each
  reading by `Provisioner.reading_retry`: four attempts, pauses of 2, 5 and 15 s) also tries again a connection that
  could not be made, was reset or was closed before its answer (the client names them: `httpx` connect, read and write
  errors and a remote protocol error), each retry on a fresh connection as a timeout's is. **An answer still decides
  at once:** a status, a POSIX absence, an error answer or a failed storage entry is never asked again, and a failure
  after an error status had arrived (a body cut short) is final too, so no retry turns an answer into a pass. A read
  still failing after its last attempt is unread, as before, and its caller fails closed; a provider that is really
  down is still seen down, after the retries. Every other site keeps the rule above.
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
- **Logged and counted:** each failed attempt a policy may retry is logged with the seconds it took and how its
  connection began, and for a reused connection how many requests it had served before, so that a per-connection limit
  can be sized; so is the outcome of the attempt after a retry. Each retry is counted by the `ReadRetry` it was made
  under, so the owner of a policy (a provisioner) reports "reads retried: N". A policy also tallies, per provider label,
  the reads made under it, the retries, each failed attempt's error by type and the reads that failed for good
  (`facts`), which a reading's record carries, so a failure shows whether it was load or the provider. A log line names
  the provider by its label (`zone`, `providers[i]`) and holds no path, id or token.

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
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

PLUMBING_ATTEMPTS = 3
PLUMBING_PAUSES_S = (2.0, 5.0)
# The reset's per-file reads (`Provisioner.reading_retry`): a connection failure is tried again too, longer apart.
TRANSPORT_ATTEMPTS = 4
TRANSPORT_PAUSES_S = (2.0, 5.0, 15.0)
# The attribute the client sets on a failure raised after an answer's status had arrived (`client._exchange`): such a
# failure is the answer's, and a `transport` policy never asks again for an error status.
ANSWERED = "expcore_answered_status"
# The least seconds a deadline must leave a retry's attempt for it to be made at all.
MIN_RETRY_S = 1.0

Trace = Callable[[str, Any], None]
Served = Callable[[], int | None]  # how many requests the connection that carries the request now sent had served


class ReadRetry:
    """How many times a failed read is tried, the pauses between, whether a connection failure is tried again as well as
    a timeout (`transport`), the clock and the sleep, the count of retries, and the tally of what the reads met.

    The default is the harness's plumbing policy: three attempts, pauses of 2 s and 5 s, timeouts only. A policy is the
    owner's, and its count is the owner's: a provisioner builds one with its own clock and sleep, so a test's ticker
    moves with the pauses, and reports `retries` as its "reads retried". A policy made for one reading names its owner
    (`owner`), whose count its retries join, and its own tally is that reading's record (`facts`). `ONE_ATTEMPT` never
    retries, so it never counts."""

    def __init__(
        self,
        attempts: int = PLUMBING_ATTEMPTS,
        pauses_s: Sequence[float] = PLUMBING_PAUSES_S,
        *,
        transport: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        owner: "ReadRetry | None" = None,
        cap: int | None = None,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("a read is tried at least once")
        if len(pauses_s) != attempts - 1 or any(p < 0 for p in pauses_s):
            raise ValueError("a retried read has one pause, of zero or more seconds, between each two attempts")
        if not isinstance(transport, bool):
            raise ValueError("whether a connection failure is tried again is True or False")
        self.attempts = attempts
        self.pauses_s = tuple(float(p) for p in pauses_s)
        self.transport = transport
        self.cap = cap  # the bound on the reads in flight at one provider the reads were made under, for the record
        self.clock, self.sleep = clock, sleep
        self._owner = owner
        self._retries = 0
        self._lock = threading.Lock()
        self._per: dict[str, dict[str, Any]] = {}  # provider label -> reads, retries, errors, failed, answers, waits

    @property
    def retries(self) -> int:
        """How many retries have been made under this policy (a read retried twice counts two)."""
        with self._lock:
            return self._retries

    def _at(self, label: str) -> dict[str, Any]:
        return self._per.setdefault(
            label, {"reads": 0, "retries": 0, "errors": {}, "failed": 0, "answers": {}, "slot_wait_s": 0.0}
        )

    def count(self, label: str | None = None) -> None:
        """Count one retry, at the provider `label` when given; the loop does, as it commits to it. The owner, if any,
        counts it too."""
        with self._lock:
            self._retries += 1
            if label is not None:
                self._at(label)["retries"] += 1
        if self._owner is not None:
            self._owner.count()

    def began(self, label: str) -> None:
        """Count one read begun under this policy at `label`."""
        with self._lock:
            self._at(label)["reads"] += 1

    def met(self, label: str, error: BaseException) -> None:
        """Count one failed attempt at `label`, by its error's type."""
        with self._lock:
            errors = self._at(label)["errors"]
            errors[type(error).__name__] = errors.get(type(error).__name__, 0) + 1

    def failed(self, label: str) -> None:
        """Count one read that failed for good at `label`: its last attempt, or one never to be tried again, failed."""
        with self._lock:
            self._at(label)["failed"] += 1

    def answered(self, label: str, kind: str) -> None:
        """Count one answer at `label` that was no plain success, by its kind: an error status (`"404"`, `"500"`), which
        the caller decides on (a 404 may be an absence), or `"unreadable"`, an answer not in its shape. An answer is
        never tried again, so it is no failed attempt: it is tallied apart (`answers`)."""
        with self._lock:
            answers = self._at(label)["answers"]
            answers[kind] = answers.get(kind, 0) + 1

    def waited(self, label: str, seconds: float) -> None:
        """Add the seconds a read waited for its provider's slot (`Provisioner.reading_slot`)."""
        with self._lock:
            entry = self._at(label)
            entry["slot_wait_s"] = round(entry["slot_wait_s"] + max(0.0, seconds), 3)

    def facts(self) -> dict[str, Any]:
        """The tally, for a record: the policy (with the per-provider bound it was made under, if any), the reads, the
        retries, the failed attempts by error type, the reads that failed for good on them, the answers that were no
        plain success by kind (`answers`), and each of those per provider label with the seconds its reads waited for
        a slot."""
        with self._lock:
            per = {
                label: {**entry, "errors": dict(entry["errors"]), "answers": dict(entry["answers"])}
                for label, entry in sorted(self._per.items())
            }

        def total(key: str) -> dict[str, int]:
            summed: dict[str, int] = {}
            for entry in per.values():
                for name, n in entry[key].items():
                    summed[name] = summed.get(name, 0) + n
            return dict(sorted(summed.items()))

        policy = {"attempts": self.attempts, "pauses_s": list(self.pauses_s), "transport": self.transport}
        return {
            "policy": policy if self.cap is None else {**policy, "provider_concurrency": self.cap},
            "reads": sum(entry["reads"] for entry in per.values()),
            "retries": sum(entry["retries"] for entry in per.values()),
            "errors": total("errors"),
            "failed": sum(entry["failed"] for entry in per.values()),
            "answers": total("answers"),
            "providers": per,
        }

    def __repr__(self) -> str:
        return (
            f"ReadRetry(attempts={self.attempts}, pauses_s={self.pauses_s}, transport={self.transport}, "
            f"retries={self.retries})"
        )


ONE_ATTEMPT = ReadRetry(1, ())  # the choice of whatever measures the platform: one attempt, a timeout stays a timeout


@contextmanager
def recorded(policy: ReadRetry, key: str = "read_retry") -> Iterator[ReadRetry]:
    """Run one reading under `policy`; when it fails, its tally so far rides on the exception as `facts[key]`, which a
    failed reset stage records (`resetspaces._Space.fail`), so the record of a failure says what the reads met. The
    exception is given the attribute on purpose: it is the failure's record."""
    try:
        yield policy
    except Exception as error:
        error.facts = {**getattr(error, "facts", {}), key: policy.facts()}  # type: ignore[attr-defined]
        raise


class Exhausted(Exception):
    """A read failed on every attempt it was given, more than one: `error` is the last failure (a timeout, or a
    connection failure under a `transport` policy) and `attempts` how many were made. A read given one attempt raises
    its own error, as it always did."""

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
    transport: tuple[type[BaseException], ...] = (),
    deadline_s: float | None = None,
    served: Served | None = None,
) -> T:
    """Call `send(timeout, trace, fresh)` until it returns, retrying only when it raises one of `timeouts`, or of
    `transport` (the connection failures) when `retry` declares `transport`, as `retry` allows.

    `send` makes one request with that timeout and hands each trace event to `trace`; `fresh` is True for a retry, which
    must not be given a connection from the pool. Any other exception propagates at once, after a line when it ended a
    retry. Under a `transport` policy a failure raised after an error status had arrived (`ANSWERED`) is the answer's
    and propagates at once too. A failure that ends the attempts is raised as it was, or as `Exhausted` when more than
    one attempt was made. Each read, retry and failed attempt is tallied by `retry`, at `label`. See the module
    docstring for the rule, the bound and the log."""
    retried = timeouts + (transport if retry.transport else ())
    retry.began(label)
    began = retry.clock()
    attempt, timeout = 0, timeout_s
    total = retry.attempts
    while True:
        attempt += 1
        probe = Attempt(served)
        started = retry.clock()
        try:
            answer = send(timeout, probe.trace, attempt > 1)
        except retried as error:
            retry.met(label, error)
            took, connection = retry.clock() - started, probe.connection
            what = "timed out" if isinstance(error, timeouts) else f"failed ({type(error).__name__})"
            status = getattr(error, ANSWERED, None)
            if retry.transport and status is not None and status >= 300:  # an answer, never asked again
                log.warning(
                    "%s: a GET %s after %.2f s on attempt %d of %d, its answer's status %s already in (connection %s);"
                    " an answer is never asked again",
                    label,
                    what,
                    took,
                    attempt,
                    total,
                    status,
                    connection,
                )
                retry.failed(label)
                raise
            pause = retry.pauses_s[attempt - 1] if attempt < total else None
            room = None if deadline_s is None else deadline_s - (retry.clock() - began)
            if pause is None or (room is not None and room - pause < MIN_RETRY_S):
                log.warning(
                    "%s: a GET %s after %.2f s on attempt %d of %d (connection %s); giving up",
                    label,
                    what,
                    took,
                    attempt,
                    total,
                    connection,
                )
                retry.failed(label)
                if attempt == 1:
                    raise
                raise Exhausted(error, attempt) from None
            log.warning(
                "%s: a GET %s after %.2f s on attempt %d of %d (connection %s); retrying in %g s",
                label,
                what,
                took,
                attempt,
                total,
                connection,
                pause,
            )
            retry.count(label)
            retry.sleep(pause)
            timeout = timeout_s if room is None else min(timeout_s, room - pause)
            continue
        except Exception as error:
            retry.met(label, error)
            retry.failed(label)
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
