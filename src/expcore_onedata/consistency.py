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
"""

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from expcore_onedata.client import OnezoneClient, PlatformError

# The states in which a transfer has ended: the earlier study's working code stopped polling at these four.
ENDED_STATES = ("completed", "failed", "cancelled", "skipped")
STATE_FIELDS = ("transferStatus", "replicationStatus", "evictionStatus")
NOT_FOUND = 404  # the provider's answer for a file or a transfer that is not there (yet)


class WaitTimeout(RuntimeError):
    """A wait reached its bound before the federation settled; the message names what was waited for."""


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
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TransferReport:
    """Poll each transfer until it has ended, or `timeout_s`. An ended transfer is a report, not evidence of placement.

    A transfer the provider does not show yet (a 404) is pending, like a running one. Any other failure propagates.
    """
    guard()
    pending = list(dict.fromkeys(transfer_ids))
    ended: dict[str, dict[str, Any]] = {}
    began = clock()
    while pending:
        for tid in list(pending):
            try:
                record = client.get(f"/transfers/{tid}", provider=provider)
            except PlatformError as error:
                if error.status != NOT_FOUND:
                    raise
                continue
            if transfer_state(record) in ENDED_STATES:
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
) -> str:
    """Return the id of the space's root directory on a provider, waiting out "no such file" on a fresh space.

    Only a 404 is waited out, and at most `tries` times; any other answer, and the last 404, propagate. This is a
    consistency wait (O07), not a probe: a probe records its failure instead (O08, `probes.py`).
    """
    guard()
    for attempt in range(1, tries + 1):
        try:
            answer = client.get(f"/spaces/{space_id}", provider=provider)
        except PlatformError as error:
            if error.status != NOT_FOUND or attempt == tries:
                raise
            sleep(wait_s)
            continue
        root = answer.get("fileId") if isinstance(answer, dict) else None
        if not isinstance(root, str) or not root:
            raise PlatformError(f"{client.provider_label(provider)}: the space's answer holds no root file id")
        return root
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises
