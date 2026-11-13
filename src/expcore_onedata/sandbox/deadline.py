# Provenance: new
"""A cell's hard deadline (O30): the kill at its cap, and what follows when a process of the cell outlives it.

At the cap every process of the cell is SIGKILLed: every process of the agent's container but its init and the init's
`sleep`, read from the container's cgroup (`teardown._readers`), so a worker in a process group or session of its own
goes too. A process waiting in the kernel takes a kill only when its wait returns, and a read on a FUSE mount whose
provider is down may not return for minutes (measured live on 12 Nov: still waiting 10 s after its kill). So:

1. kill: SIGKILL to every process of the cell, in rounds until a listing shows none new; then up to `grace_s`, polled,
   for them to go. All gone: the outcome is `killed`, the agent's own time limit as before.
2. survived: any process left (or none that can be listed: unknown is never gone) is recorded with its state, and the
   mount is aborted (`abort`): the FUSE connection, which ends every request waiting on it with an errno, so the pending
   kill takes. Then up to `grace_s` again. All gone: `mount_aborted`.
3. stop: an abort that failed, could not be made (no FUSE mount), or freed nothing, stops the sandbox (`stop`), whose
   ordered teardown has its own last resorts. Stopped: `sandbox_stopped`; a stop that failed too: `survived_kill`.

Every step is recorded with its seconds since the cap (`t_s`), and the record with the wall-clock time of the cap.
Each outcome but `killed` makes the attempt infrastructure (`kinds.CELL_UNKILLABLE`), and the run is over: after an
abort the mount is dead for the rest of the run, so no later cell may read it as if it were an empty directory.

The tracker, the abort and the stop are given, so the escalation is tested with a fake process tracker: a real process
in uninterruptible sleep cannot be made in a test.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import time
from collections.abc import Callable
from typing import Any, Protocol

KILLED = "killed"  # every process of the cell ended within the grace of its kill
SURVIVED = "survived_kill"  # a process outlived its kill, and neither the abort nor the stop ended it
MOUNT_ABORTED = "mount_aborted"  # a process outlived its kill and ended once the mount's FUSE connection was aborted
SANDBOX_STOPPED = "sandbox_stopped"  # a process outlived its kill and the sandbox was stopped
OUTCOMES = (KILLED, SURVIVED, MOUNT_ABORTED, SANDBOX_STOPPED)
ESCALATED = (SURVIVED, MOUNT_ABORTED, SANDBOX_STOPPED)  # each makes the attempt infrastructure
KILL_ROUNDS = 5  # a forking worker can start a child between a listing and the kill: listed again until none is new
POLL_S = 0.25
STATES_KEPT = 16


class Tracker(Protocol):
    """The cell's processes: `processes` lists them now (None when they cannot be listed), `kill` SIGKILLs these."""

    def processes(self) -> list[dict[str, Any]] | None: ...

    def kill(self, procs: list[dict[str, Any]]) -> None: ...


@dataclasses.dataclass(frozen=True)
class Escalation:
    """What the deadline does past the kill: its grace period, the mount's abort and the sandbox's stop (each returns a
    record with `ok`: True, False, or None when it could not be attempted), and its clock."""

    grace_s: float
    abort: Callable[[], dict[str, Any]]
    stop: Callable[[], dict[str, Any]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_s: float = POLL_S


def _key(proc: dict[str, Any]) -> tuple[Any, Any]:
    return proc["pid"], proc.get("start")


class _Run:
    """One deadline's processes and record."""

    def __init__(self, tracker: Tracker, esc: Escalation) -> None:
        self.tracker, self.esc = tracker, esc
        self.seen: set[tuple[Any, Any]] = set()
        self.t0 = esc.clock()
        utc = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
        self.record: dict[str, Any] = {"utc": utc, "grace_s": esc.grace_s, "outcome": None, "steps": []}

    def step(self, name: str, **facts: Any) -> None:
        self.record["steps"].append({"step": name, "t_s": round(self.esc.clock() - self.t0, 3), **facts})

    def kill_new(self, procs: list[dict[str, Any]]) -> int:
        """SIGKILL what was not signalled yet; how many that was."""
        fresh = [p for p in procs if _key(p) not in self.seen]
        if fresh:
            self.tracker.kill(fresh)
            self.seen.update(_key(p) for p in fresh)
        return len(fresh)

    def kill(self) -> list[dict[str, Any]] | None:
        """Every process of the cell, in rounds until a listing shows none new; what is left, None when unknown."""
        signalled, procs = 0, None
        for _ in range(KILL_ROUNDS):
            procs = self.tracker.processes()
            if procs is None:
                break
            new = self.kill_new(procs)
            signalled += new
            if not new:
                break
        else:
            procs = self.tracker.processes()
        self.step("kill", signalled=signalled if self.seen or procs is not None else None)
        return procs

    def wait(self, left: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Up to the grace period for the cell's processes to go, killing any new one; what is left, None unknown."""
        for _ in range(max(1, math.ceil(self.esc.grace_s / self.esc.poll_s))):
            if left == []:
                return left
            self.esc.sleep(self.esc.poll_s)
            left = self.tracker.processes()
            if left:
                self.kill_new(left)
        return left

    def survivors(self, left: list[dict[str, Any]] | None) -> None:
        states = None if left is None else [p.get("state") for p in left[:STATES_KEPT]]
        self.step("survived", left=None if left is None else len(left), states=states)

    def done(self, outcome: str) -> dict[str, Any]:
        self.record["outcome"] = outcome
        return self.record


def _attempt(action: Callable[[], dict[str, Any]], what: str) -> dict[str, Any]:
    """An abort's or a stop's own record; one that raises is a failure with its class, never raised here."""
    try:
        return dict(action())
    except Exception as error:
        return {"ok": False, "error": f"the {what} raised {type(error).__name__}"}


def enforce(tracker: Tracker, esc: Escalation) -> dict[str, Any]:
    """Kill the cell at its deadline and escalate while any of its processes outlives that (module docstring); the
    record of every step. Never raises for what the abort or the stop does."""
    run = _Run(tracker, esc)
    left = run.wait(run.kill())
    if left == []:
        run.step("ended")
        return run.done(KILLED)
    run.survivors(left)
    abort = _attempt(esc.abort, "abort")
    run.step("abort", **abort)
    if abort.get("ok") is True:
        left = run.wait(tracker.processes())
        if left == []:
            run.step("ended")
            return run.done(MOUNT_ABORTED)
        run.survivors(left)
    stop = _attempt(esc.stop, "stop")
    run.step("stop", **stop)
    return run.done(SANDBOX_STOPPED if stop.get("ok") is True else SURVIVED)


__all__ = [
    "ESCALATED",
    "KILLED",
    "MOUNT_ABORTED",
    "OUTCOMES",
    "SANDBOX_STOPPED",
    "SURVIVED",
    "Escalation",
    "Tracker",
    "enforce",
]
