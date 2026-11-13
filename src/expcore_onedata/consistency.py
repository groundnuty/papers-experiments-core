# Provenance: new
#   merged: icsoc-2026-code-agent @ c1e76c2, harness/verifier.py:475-553 (convergence needs consecutive matching polls)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:218-243 (a transfer counts once it ended)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:314-323 (a new space root may not exist yet)
"""Waiting out the federation's eventual consistency, with bounds (O07).

Three things lag in a federation, and each wait here is bounded by a timeout and by a count:
- a **transfer** is counted only once it has ENDED: a job still running, or one the provider does not show yet, is
  pending, and a job still pending at the bound is reported as timed out, never as done (`wait_transfers`);
- a **new space root** may answer "no such file" for a while after support is granted (`space_root`): that answer, and
  only that one, is waited out;
- a **block map** that must show a placement is read until it matches on two consecutive polls, so one lucky read
  between two stale ones does not count (`converge`). A read that fails counts as a poll that did not match.

These waits belong to provisioning and placement, before or after a run. They are not one of the declared wait kinds
(`expcore.plugin.WAIT_KINDS`), which are the harness's own machinery, so they must never run inside the agent's time. A
call made on the thread of a running event loop, which is where the agent loop runs, is refused (`guard`); an
executor that must place data does so before it starts the loop, or after it ends.

Every wait takes an injectable `clock` and `sleep`, so a test never really waits. A request inside a poll is bounded
by the client's own timeout.

A wait whose reads propagate a failure (`wait_transfers`, `space_root`, `wait_visible`) takes the caller's `retry`, the
policy for a read that timed out (`readretry.py`): the harness's plumbing passes its own, whatever measures the platform
passes `ONE_ATTEMPT`, and the default is one attempt. A read inside a poll that counts a failure as a poll that did not
match (`converge`, `converge_each`) is never retried: the poll is the retry. A wait's own bound also bounds the retries
of its reads.
"""

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from expcore_onedata.client import OnezoneClient, PlatformError, answered_absent
from expcore_onedata.readretry import ONE_ATTEMPT, ReadRetry

# The states in which a transfer has ended: the earlier study's working code stopped polling at these four.
ENDED_STATES = ("completed", "failed", "cancelled", "skipped")
STATE_FIELDS = ("transferStatus", "replicationStatus", "evictionStatus")
# A file or a transfer that is not there (yet) is told by `client.answered_absent`, the one rule for it: a 404, or
# exactly Onedata 25.0's 400 posix enoent. Any other answer, another 400 among them, is never "not there yet".


# How long a provider may take to see a file written through another before the wait for it gives up, and how often
# it is asked meanwhile. On the Compose federation (Onedata 25.0) a second provider saw a new file after about 6 s;
# the bound leaves twenty times that.
VISIBLE_TIMEOUT_S = 120.0
VISIBLE_POLL_S = 0.5
# What the visibility reads ask for. On 25.0 `GET /data/{id}` resolves only the attributes asked for: `name` reads the
# entry's document strictly and answers 400 posix enoent once it is deleted, while `parent_id`, `file_id` and a
# directory's `size` read it including a deleted one, so each alone keeps answering from a tombstone (14 days by
# default; what a file's `size` alone does was not traced). A read that includes `name` is gone when the entry is
# (day0/2026-11-13-federation-facts-deleted-entries-by-id.md).
VISIBLE_ATTRIBUTES = ("name", "size", "parent_id")


class WaitTimeout(RuntimeError):
    """A wait reached its bound before the federation settled; the message names what was waited for."""


class NotVisible(WaitTimeout):
    """A provider did not see a file written through another, at its size, within `VISIBLE_TIMEOUT_S`."""


def guard() -> None:
    """Refuse to wait on the thread of a running event loop: a run's loop is agent time, and this is not a wait kind."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("a consistency wait must not run inside a run's event loop; place data before or after the run")


@dataclass(frozen=True)
class Convergence:
    """How a block-map convergence went: whether it did, the last value read and every poll taken."""

    converged: bool
    value: Any
    checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def converge[T](
    read: Callable[[], T],
    matches: Callable[[T], bool],
    *,
    timeout_s: float,
    poll_s: float,
    consecutive: int = 2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Convergence:
    """Poll `read` until `matches` holds on `consecutive` polls in a row, or `timeout_s` passes.

    A poll that raises `PlatformError` is recorded as not ok and does not match; the count of matching polls starts
    over on any poll that does not match, so the requirement is consecutive and not merely a total. Any other
    exception is a defect and propagates. Nothing is guessed: with no matching run the result is `converged=False`.
    """
    guard()
    if consecutive < 1 or timeout_s < 0 or poll_s <= 0:
        raise ValueError("converge needs consecutive >= 1, a non-negative timeout and a positive poll interval")
    began, run, last = clock(), 0, None
    checks: list[dict[str, Any]] = []
    while True:
        elapsed = clock() - began
        try:
            value = read()
            ok = True
        except PlatformError:
            value, ok = None, False
        hit = ok and matches(value)
        checks.append({"t_rel_s": round(elapsed, 3), "probe_ok": ok, "matches": bool(hit)})
        if ok:
            last = value
        run = run + 1 if hit else 0
        if run >= consecutive:
            return Convergence(True, value, tuple(checks))
        if elapsed >= timeout_s:
            return Convergence(False, last, tuple(checks))
        sleep(poll_s)


def bounded_map[K, V](call: Callable[[K], V], keys: Sequence[K], workers: int) -> list[V]:
    """Call `call` on every key, at most `workers` at once, and return the results in the keys' order.

    One worker calls them in turn on this thread. The first exception any call raises propagates once every call has
    ended, so nothing is left running behind a failure."""
    if workers < 1:
        raise ValueError("a bounded map needs at least one worker")
    if workers == 1 or len(keys) < 2:
        return [call(key) for key in keys]
    with ThreadPoolExecutor(max_workers=min(workers, len(keys))) as pool:
        futures = [pool.submit(call, key) for key in keys]
        wait(futures)
    return [future.result() for future in futures]


@dataclass(frozen=True)
class EachConvergence:
    """How a convergence over many items went: whether every item settled, the items that did not, the last value
    read of each, and how many polls were taken."""

    converged: bool
    pending: tuple[Any, ...]
    values: dict[Any, Any]
    polls: int


def converge_each[K, T](
    keys: Iterable[K],
    read: Callable[[K], T],
    matches: Callable[[T], bool],
    *,
    timeout_s: float,
    poll_s: float,
    consecutive: int = 2,
    workers: int = 1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EachConvergence:
    """Poll many items together until each matches on `consecutive` polls in a row, or `timeout_s` passes (O07).

    Each poll reads every item not settled yet, at most `workers` at once; an item that matched on `consecutive`
    polls in a row is settled and not read again. A read that raises `PlatformError` does not match and starts that
    item's count over; any other exception is a defect and propagates. With items left at the bound the result is
    `converged=False` with them pending: nothing is guessed."""
    guard()
    if consecutive < 1 or timeout_s < 0 or poll_s <= 0:
        raise ValueError("converge_each needs consecutive >= 1, a non-negative timeout and a positive poll interval")
    runs = dict.fromkeys(dict.fromkeys(keys), 0)
    values: dict[Any, Any] = {}
    began, polls = clock(), 0

    def poll(key: K) -> tuple[bool, Any]:
        try:
            value = read(key)
        except PlatformError:
            return False, None
        return matches(value), value

    while True:
        pending = [key for key, run in runs.items() if run < consecutive]
        if not pending:
            return EachConvergence(True, (), values, polls)
        polls += 1
        for key, (hit, value) in zip(pending, bounded_map(poll, pending, workers), strict=True):
            runs[key] = runs[key] + 1 if hit else 0
            if value is not None:
                values[key] = value
        if all(runs[key] >= consecutive for key in pending):
            return EachConvergence(True, (), values, polls)
        if clock() - began >= timeout_s:
            left = tuple(key for key, run in runs.items() if run < consecutive)
            return EachConvergence(False, left, values, polls)
        sleep(poll_s)


def transfer_state(record: Any) -> str:
    """Return the state a transfer record reports, or "unknown" when it names none of the known fields."""
    if isinstance(record, dict):
        for name in STATE_FIELDS:
            state = record.get(name)
            if isinstance(state, str) and state:
                return state
    return "unknown"


@dataclass(frozen=True)
class TransferReport:
    """What the transfers did: the final record of each that ended, and the ids still pending at the bound."""

    records: dict[str, dict[str, Any]]
    timed_out: tuple[str, ...]
    seconds: float

    def states(self) -> dict[str, int]:
        """Count the ended transfers by their state."""
        counts: dict[str, int] = {}
        for record in self.records.values():
            counts[transfer_state(record)] = counts.get(transfer_state(record), 0) + 1
        return counts

    def total(self, key: str) -> int:
        """Sum one counter (for example `bytesReplicated`) over the ended transfers; a missing one counts as zero."""
        return sum(int(r.get(key) or 0) for r in self.records.values())


def wait_transfers(
    client: OnezoneClient,
    provider: str,
    transfer_ids: Iterable[str],
    *,
    timeout_s: float,
    poll_s: float,
    workers: int = 1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retry: ReadRetry = ONE_ATTEMPT,
) -> TransferReport:
    """Poll each transfer until it has ended, or `timeout_s`. An ended transfer is a report, not evidence of placement.

    A transfer the provider does not show yet (`answered_absent`: a 404 or 400 posix enoent) is pending, like a running
    one. Any other failure, another 400 included, propagates at once. Each poll reads the pending transfers at most
    `workers` at once.
    """
    guard()
    pending = list(dict.fromkeys(transfer_ids))
    ended: dict[str, dict[str, Any]] = {}
    began = clock()

    def read(tid: str) -> Any:
        try:
            return client.get(
                f"/transfers/{tid}", provider=provider, retry=retry, deadline_s=timeout_s - (clock() - began)
            )
        except PlatformError as error:
            if not answered_absent(error):
                raise
            return None

    while pending:
        for tid, record in zip(list(pending), bounded_map(read, list(pending), workers), strict=True):
            if record is not None and transfer_state(record) in ENDED_STATES:
                ended[tid] = record
                pending.remove(tid)
        if not pending or clock() - began >= timeout_s:
            break
        sleep(poll_s)
    return TransferReport(ended, tuple(pending), round(clock() - began, 3))


def require_ended(report: TransferReport, what: str) -> TransferReport:
    """Raise WaitTimeout when any transfer had not ended at the bound: it may still change a provider's replica."""
    if report.timed_out:
        raise WaitTimeout(
            f"{what}: {len(report.timed_out)} transfer(s) had not ended at the bound; they may still change a "
            "replica, so nothing may follow until they are checked"
        )
    return report


def space_root(
    client: OnezoneClient,
    provider: str,
    space_id: str,
    *,
    tries: int = 30,
    wait_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    retry: ReadRetry = ONE_ATTEMPT,
) -> str:
    """Return the id of the space's root directory on a provider, waiting out "no such file" on a fresh space.

    Only "not there yet" (`answered_absent`: a 404, or 25.0's 400 posix enoent) is waited out, and at most `tries`
    times; any other answer, another 400 included, and the last absence, propagate. This is a consistency wait (O07),
    not a probe: a probe records its failure instead (O08, `probes.py`).
    """
    guard()
    for attempt in range(1, tries + 1):
        try:
            answer = client.get(f"/spaces/{space_id}", provider=provider, retry=retry)
        except PlatformError as error:
            if not answered_absent(error) or attempt == tries:
                raise
            sleep(wait_s)
            continue
        root = answer.get("fileId") if isinstance(answer, dict) else None
        if not isinstance(root, str) or not root:
            raise PlatformError(f"{client.provider_label(provider)}: the space's answer holds no root file id")
        return root
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def wait_visible(
    client: OnezoneClient,
    provider: str,
    file_id: str,
    *,
    size: int,
    timeout_s: float = VISIBLE_TIMEOUT_S,
    poll_s: float = VISIBLE_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retry: ReadRetry = ONE_ATTEMPT,
) -> float:
    """Wait until a provider sees a file written through another, at its size; return the seconds it took.

    The file's `name`, `size` and `parent_id` are read through the provider together, and the size is checked from that
    answer. A read of `parent_id` alone is answered from a deleted entry's tombstone, 14 days by default on 25.0, and a
    file's `size` alone may be (a directory's is; a file's was not traced), so either alone could report a file deleted
    during the wait as visible; `name` is read strictly and refuses a deleted entry
    (`VISIBLE_ATTRIBUTES`). "Not visible yet" is `answered_absent` (a 404, or 25.0's 400 posix enoent), and a size other
    than `size` is a file the provider has not caught up with; both are asked again every `poll_s`. Any other failure,
    another 400 included, propagates at once (fail closed). Past `timeout_s` it raises `NotVisible`, naming the provider
    by its position in platform.toml. A consistency wait (O07), never a probe's read (O08).
    """
    guard()
    began = clock()
    while True:
        try:
            answer = client.get(
                f"/data/{file_id}",
                provider=provider,
                params={"attribute": list(VISIBLE_ATTRIBUTES)},
                retry=retry,
                deadline_s=timeout_s - (clock() - began),
            )
            if isinstance(answer, dict) and answer.get("size") == size:
                return round(clock() - began, 3)
        except PlatformError as error:
            if not answered_absent(error):
                raise
        if clock() - began >= timeout_s:
            raise NotVisible(
                f"{client.provider_label(provider)}: a file written through another provider was not visible there at "
                f"its size within {timeout_s:.0f} s"
            )
        sleep(poll_s)
