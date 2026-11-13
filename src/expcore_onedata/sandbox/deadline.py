# Provenance: new
"""A cell's hard deadline (O34): the kill at its cap, and what follows when a process of the cell outlives it.

At the cap every process of the cell is SIGKILLed: every process of the agent's container but its init and the init's
`sleep`, read from the container's cgroup (`teardown._readers`), so a worker in a process group or session of its own
goes too. A process waiting in the kernel takes a kill only when its wait returns, and a read on a FUSE mount whose
provider is down may not return for minutes (measured live on 12 Nov: still waiting 10 s after its kill). So:

1. kill: SIGKILL to every process of the cell, in rounds until a listing shows none new; then up to `grace_s`, polled,
   for them to go. All gone: the outcome is `killed`, the agent's own time limit as before.
2. survived: any process left (or none that can be listed: unknown is never gone) is recorded with its state, and the
   mount is aborted (`abort`): the FUSE connection, which ends every request waiting on it with an errno, so the pending
   kill takes. Then up to `grace_s` again. All gone: `mount_aborted`.
3. stop: an abort that failed, could not be made (no FUSE mount), or freed nothing, orders the sandbox's stop:
   `sandbox_stopped`. The stop itself is the caller's, once this outcome is recorded (`cells.py`): its ordered teardown,
   which has its own last resorts and may take minutes, never holds up the run's classification.

The whole escalation, from the cap to the order of the stop, is bounded by one margin, `margin_s(grace_s)`: the two
graces and `COMMANDS_S` for the escalation's own commands (the listings, the kills and the abort). Every grace is
counted by the clock, never in polls, and every command is given only the time left: of its grace while one runs, of
the margin otherwise. When the margin runs out before the cell is shown ended, the escalation stops waiting, the stop is
ordered all the same, and the outcome is `survived_kill`.

A listing or a kill that raises (its command failed, not the cell) is recorded as the step `raised`, with what was
under way and the error's class, and the caller's own kill of what is left (`fallback`, the sandbox's quiesce) is
tried: every process shown gone is the clean kill it would have been, `killed`; anything else, a process left in
whatever state or none that could be listed, is `survived_kill`, and the stop is ordered. Unknown is never gone.

Every step is recorded with its seconds since the cap (`t_s`), and the record with the wall-clock time of the cap.
Each outcome but `killed` makes the attempt infrastructure (`kinds.CELL_UNKILLABLE`), and the run is over: after an
abort the mount is dead for the rest of the run, so no later cell may read it as if it were an empty directory.

The tracker, the abort and the clock are given, so the escalation is tested with a fake process tracker and a fake
clock: a real process in uninterruptible sleep cannot be made in a test.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections.abc import Callable
from typing import Any, Protocol

KILLED = "killed"  # every process of the cell ended within the grace of its kill
SURVIVED = "survived_kill"  # a process outlived its kill and the margin ran out before anything ended it
MOUNT_ABORTED = "mount_aborted"  # a process outlived its kill and ended once the mount's FUSE connection was aborted
SANDBOX_STOPPED = "sandbox_stopped"  # a process outlived its kill and the abort: the sandbox's stop is ordered
OUTCOMES = (KILLED, SURVIVED, MOUNT_ABORTED, SANDBOX_STOPPED)
ESCALATED = (SURVIVED, MOUNT_ABORTED, SANDBOX_STOPPED)  # each makes the attempt infrastructure
STOPS = (SURVIVED, SANDBOX_STOPPED)  # each orders the sandbox's stop: the caller runs it once the outcome is recorded
KILL_ROUNDS = 5  # a forking worker can start a child between a listing and the kill: listed again until none is new
POLL_S = 0.25
STATES_KEPT = 16
# what the escalation's own commands may take beyond its two graces: the listings and kills before the first grace,
# and the abort's `docker exec` in the sidecar; the margin a cell's escalation may last past its cap is `margin_s`
COMMANDS_S = 20.0


def margin_s(grace_s: float) -> float:
    """The most a cell's escalation lasts past its cap, from the kill to the order of the sandbox's stop: two graces
    (after the kill, after the abort) and `COMMANDS_S`. 30 s with the default grace of 5 s, 140 s at its maximum."""
    return 2 * grace_s + COMMANDS_S


class Tracker(Protocol):
    """The cell's processes: `processes` lists them now (None when they cannot be listed in `timeout_s`), `kill`
    SIGKILLs these within `timeout_s`."""

    def processes(self, timeout_s: float) -> list[dict[str, Any]] | None: ...

    def kill(self, procs: list[dict[str, Any]], timeout_s: float) -> None: ...


@dataclasses.dataclass(frozen=True)
class Escalation:
    """What the deadline does past the kill: its grace period, the mount's abort (given the seconds it may take; it
    returns a record with `ok`: True, False, or None when it could not be attempted), and its clock."""

    grace_s: float
    abort: Callable[[float], dict[str, Any]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_s: float = POLL_S

    @property
    def margin_s(self) -> float:
        return margin_s(self.grace_s)


def _key(proc: dict[str, Any]) -> tuple[Any, Any]:
    return proc["pid"], proc.get("start")


class _Run:
    """One deadline's processes and record."""

    def __init__(self, tracker: Tracker, esc: Escalation) -> None:
        self.tracker, self.esc = tracker, esc
        self.seen: set[tuple[Any, Any]] = set()
        self.doing: str | None = None  # the command under way, named when it raises
        self.t0 = esc.clock()
        self.end = self.t0 + esc.margin_s  # the whole escalation's bound
        utc = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
        self.record: dict[str, Any] = {
            "utc": utc,
            "grace_s": esc.grace_s,
            "margin_s": esc.margin_s,
            "outcome": None,
            "steps": [],
        }

    def left(self, until: float | None = None) -> float:
        """Seconds left until `until`, the margin's end by default."""
        return (self.end if until is None else until) - self.esc.clock()

    def step(self, name: str, **facts: Any) -> None:
        self.record["steps"].append({"step": name, "t_s": round(self.esc.clock() - self.t0, 3), **facts})

    def listing(self, until: float) -> list[dict[str, Any]] | None:
        """The cell's processes, the listing given the time left until `until`; None (unknown) when none is left."""
        budget = self.left(until)
        return self.command("listing", self.tracker.processes, budget) if budget > 0 else None

    def command(self, what: str, call: Callable[..., Any], *args: Any) -> Any:
        """One command of the escalation, what is under way kept while it runs, so that one that raises is named."""
        self.doing = what
        result = call(*args)
        self.doing = None
        return result

    def kill_new(self, procs: list[dict[str, Any]], until: float) -> int:
        """SIGKILL what was not signalled yet, within the time left until `until`; how many that was."""
        fresh = [p for p in procs if _key(p) not in self.seen]
        budget = self.left(until)
        if fresh and budget > 0:
            self.command("kill", self.tracker.kill, fresh, budget)
            self.seen.update(_key(p) for p in fresh)
            return len(fresh)
        return 0

    def kill(self) -> list[dict[str, Any]] | None:
        """Every process of the cell, in rounds until a listing shows none new; what is left, None when unknown."""
        signalled, procs = 0, None
        for _ in range(KILL_ROUNDS):
            procs = self.listing(self.end)
            if procs is None:
                break
            new = self.kill_new(procs, self.end)
            signalled += new
            if not new:
                break
        else:
            procs = self.listing(self.end)
        self.step("kill", signalled=signalled if self.seen or procs is not None else None)
        return procs

    def wait(self, left: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Up to the grace period by the clock, never past the margin, for the cell's processes to go, killing any new
        one; each listing and kill is given only what is left of the grace, so a slow daemon cannot stretch it. What is
        left, None when unknown."""
        until = min(self.esc.clock() + self.esc.grace_s, self.end)
        while left != []:
            now = self.esc.clock()
            if now >= until:
                break
            self.esc.sleep(min(self.esc.poll_s, until - now))
            if self.left(until) <= 0:
                break
            left = self.listing(until)
            if left:
                self.kill_new(left, until)
        return left

    def survivors(self, left: list[dict[str, Any]] | None) -> None:
        states = None if left is None else [p.get("state") for p in left[:STATES_KEPT]]
        self.step("survived", left=None if left is None else len(left), states=states)

    def done(self, outcome: str) -> dict[str, Any]:
        self.record["outcome"] = outcome
        return self.record


def _attempt(action: Callable[[], dict[str, Any]], what: str) -> dict[str, Any]:
    """An abort's own record; one that raises is a failure with its class, never raised here."""
    try:
        return dict(action())
    except Exception as error:
        return {"ok": False, "error": f"the {what} raised {type(error).__name__}"}


def enforce(
    tracker: Tracker, esc: Escalation, fallback: Callable[[], list[dict[str, Any]] | None] | None = None
) -> dict[str, Any]:
    """Kill the cell at its deadline and escalate while any of its processes outlives that (module docstring), within
    `esc.margin_s`; the record of every step. An outcome in `STOPS` leaves the sandbox's stop to the caller, once the
    outcome is recorded. A step that raises ends in `fallback`, the caller's own kill of the cell's processes, which
    returns what is left (None when unknown). Never raises for what the escalation's commands or the abort do."""
    run = _Run(tracker, esc)
    try:
        return _escalate(run, esc)
    except Exception as error:
        what = run.doing or "escalation"
        run.step("raised", during=what, error=f"the {what} raised {type(error).__name__}")
        return _fall_back(run, fallback)


def _fall_back(run: _Run, fallback: Callable[[], list[dict[str, Any]] | None] | None) -> dict[str, Any]:
    """After a step that raised: what `fallback` leaves of the cell. Every process shown gone is `killed`; a process
    left, in whatever state, or none that could be listed, is `survived_kill`, with the stop ordered."""
    try:
        left = fallback() if fallback is not None else None
    except Exception as error:
        left = None
        run.step("fallback", error=f"the fallback raised {type(error).__name__}")
    if left == []:
        run.step("ended")
        return run.done(KILLED)
    run.survivors(left)
    run.step("stop")  # ordered: the caller stops the sandbox once this outcome is recorded
    return run.done(SURVIVED)


def _escalate(run: _Run, esc: Escalation) -> dict[str, Any]:
    left = run.wait(run.kill())
    if left == []:
        run.step("ended")
        return run.done(KILLED)
    run.survivors(left)
    budget = run.left()
    abort = _attempt(lambda: esc.abort(budget), "abort") if budget > 0 else {"ok": False, "why": "no time was left"}
    run.step("abort", **abort)
    if abort.get("ok") is True and run.left() > 0:
        left = run.wait(left)
        if left == []:
            run.step("ended")
            return run.done(MOUNT_ABORTED)
        run.survivors(left)
    over = run.left() <= 0
    if over:  # waited no longer: whatever runs still is the stop's
        run.step("margin", margin_s=esc.margin_s)
    run.step("stop")  # ordered: the caller stops the sandbox once this outcome is recorded
    return run.done(SURVIVED if over else SANDBOX_STOPPED)


__all__ = [
    "COMMANDS_S",
    "ESCALATED",
    "KILLED",
    "MOUNT_ABORTED",
    "OUTCOMES",
    "SANDBOX_STOPPED",
    "STOPS",
    "SURVIVED",
    "Escalation",
    "Tracker",
    "enforce",
    "margin_s",
]
