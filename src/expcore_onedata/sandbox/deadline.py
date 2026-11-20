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
tried: every process shown gone is the clean kill it would have been, `killed`, unless the mount was aborted before the
raise (see below); anything else, a process left in whatever state or none that could be listed, is `survived_kill`,
and the stop is ordered. Unknown is never gone.

An outcome never gets less severe than a step already taken (`SEVERITY`): `killed` < `mount_aborted` <
`sandbox_stopped` < `survived_kill`. Once the mount was aborted, or the stop reached, the outcome is at least that
whatever a later step decides, a fallback that finds nothing among them: the mount is dead, so the cell is not the
agent's and no later cell may run on it. It is enforced where the outcome is recorded (`_Run.done`), so no path of
`enforce` can end lower than what it did.

Every step is recorded with its seconds since the cap (`t_s`), and the record with the wall-clock time of the cap.
Each outcome but `killed` makes the attempt infrastructure (`kinds.CELL_UNKILLABLE`), and the run is over: after an
abort the mount is dead for the rest of the run, so no later cell may read it as if it were an empty directory.

**One exception: a mount that is restored.** A backend that can bring its mount back (`Escalation.restorable`) and says
whose block it was (`Escalation.attribute`) may end the cell as the agent's. It reads each survivor at the `survived`
step (`Tracker.diagnose`, before anything is aborted) and asks `attribute` whether the survivors were all blocked on
what the study's own manipulation holds down, never on anything the harness cannot account for. Only a verdict `agent`,
with an abort that freed every process of the cell, leaves the record `mount_aborted` with `restore: "pending"`: the run
stands, the cell's kill stays the agent's time limit, and the caller restores the mount (`cells.CellRunner.restore`),
which makes the outcome `mount_restored`, the one escalation's ending that is not infrastructure. Every other freed
survivor (`infrastructure`, no verdict, a diagnosis that raised, an abort of a backend that cannot restore) stays
`mount_aborted`, escalated, as before. `ends_run` says which records end the run; it fails closed, so a record it does
not know ends it.

The tracker, the abort and the clock are given, so the escalation is tested with a fake process tracker and a fake
clock: a real process in uninterruptible sleep cannot be made in a test.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol

KILLED = "killed"  # every process of the cell ended within the grace of its kill
SURVIVED = "survived_kill"  # a process outlived its kill and the margin ran out before anything ended it
MOUNT_ABORTED = "mount_aborted"  # a process outlived its kill and ended once the mount's FUSE connection was aborted
SANDBOX_STOPPED = "sandbox_stopped"  # a process outlived its kill and the abort: the sandbox's stop is ordered
# the one ending of a deadline that is not infrastructure after a process outlived its kill: the mount was aborted, the
# survivors were the agent's (`attribute`), and the mount was brought back, so the run goes on (`CellRunner.restore`).
# `enforce` never decides it: it is what a pending restore becomes, so it is in `RECORDED`, not `OUTCOMES`, `SEVERITY`
MOUNT_RESTORED = "mount_restored"
OUTCOMES = (KILLED, SURVIVED, MOUNT_ABORTED, SANDBOX_STOPPED)
RECORDED = (*OUTCOMES, MOUNT_RESTORED)  # every outcome the record of a cell's deadline can hold
ESCALATED = (
    SURVIVED,
    MOUNT_ABORTED,
    SANDBOX_STOPPED,
)  # each makes the attempt infrastructure, unless `ends_run` says not
# the record's `restore`, set only on a `mount_aborted` that a restorable escalation freed with the verdict `agent`:
RESTORE_PENDING = "pending"  # the caller has not run the restore yet: the run stands, and nothing may exec
RESTORE_RUNNING = "running"  # the restore is under way
RESTORE_DONE = "restored"  # it succeeded: the outcome is then `mount_restored`
RESTORE_NOT_NEEDED = "not_needed"  # the run's body ended before anything read the mount again: no restore was run
RESTORE_FAILED = "failed"  # it did not succeed: the run is ended (`MOUNT_LOST`, by `RESTORE`)
RESTORE_STATES = (RESTORE_PENDING, RESTORE_RUNNING, RESTORE_DONE, RESTORE_NOT_NEEDED, RESTORE_FAILED)
# what leaves the run standing after a restorable abort; every other state, and one not named here, ends it
RESTORE_KEEPS_RUN = (RESTORE_PENDING, RESTORE_RUNNING, RESTORE_NOT_NEEDED)
MOUNT_LOST = "mount_lost"  # the `outcome` of the end `CellRunner.restore` publishes when the restore fails
RESTORE = "restore"  # the `by` of that end, as `SWEEP` is of the sweep's
AGENT = "agent"  # an attribution's verdict: every survivor was blocked on what the study's manipulation holds down
INFRASTRUCTURE = "infrastructure"  # any other verdict: the survivors are the harness's, so the run is set aside
VERDICTS = (AGENT, INFRASTRUCTURE)
UNKNOWN_BLOCK = "blocking_object_unknown"  # the `why` of a verdict the escalation could not get at all
DIAGNOSTIC_KEYS = ("survivors", "survivors_cut", "connection")  # what `Tracker.diagnose` may add to the record
# a diagnosis is the one step of the escalation that reads the survivors' own memory maps, which take the target's mmap
# lock: a process in D inside a FUSE page fault holds it, so a read may never return. It is bounded by its own seconds
# and run in a thread that is given up on, and it never takes the time the abort and the second grace need
DIAGNOSE_S = 5.0
ABORT_RESERVE_S = 5.0  # kept for the abort itself beyond the second grace, whatever the diagnosis takes
READING = "reading"  # the key of a diagnosis that holds every process it read, each with its `pid` and `start`
ATTRIBUTION_KEYS = ("verdict", "why", "target", "space_holder")  # what the record keeps of `attribute`'s answer
# from the least severe to the most: an outcome never falls below a step already taken. The two stops are in STOPS and
# order the same stop, so their order never changes what is done; `survived_kill`, where nothing shows the cell gone, is
# above them all
SEVERITY = (KILLED, MOUNT_ABORTED, SANDBOX_STOPPED, SURVIVED)
SWEEP = "end_of_cell_sweep"  # what ended a run that no deadline ended: the sweep after a cell found a survivor (O72)
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
    SIGKILLs these within `timeout_s` and answers with the `key_of` of each that the signal reached (a signal sent, or
    its process found gone); None says every one was reached. A process whose signal did not reach it (the privileged
    half signals a pid only while it is still the process listed, O72) is not marked done: it is listed and tried
    again, never counted as signalled."""

    def processes(self, timeout_s: float) -> list[dict[str, Any]] | None: ...

    def kill(self, procs: list[dict[str, Any]], timeout_s: float) -> Collection[tuple[Any, ...]] | None: ...


class Diagnosing(Protocol):
    """A tracker that can also say what the survivors are blocked on: `diagnose(left, timeout_s)` reads each process
    of `left` (from `/proc` alone, never the file it waits on: that would block the reader too) within `timeout_s`, and
    answers with the record keys of `DIAGNOSTIC_KEYS` (`survivors`, `survivors_cut`, `connection`), or None when it
    could not. It is optional: the escalation reads it with `getattr`, so a tracker that has none is unchanged.

    The answer carries the reading the verdict is made on under `READING` (`"reading"`): one entry for EVERY process
    of `left` it read, uncapped, each entry holding the `pid` and the `start` of the process exactly as `left` lists
    them, beside what was read of it (`comm`, `state`, `tasks`; `sandbox/attribution.py` names the rest). These two
    keys are required of every entry, and `reading` is never recorded (the record keeps the capped `survivors`). The
    escalation asks for a verdict only when the entries cover every `(pid, start)` of `left`; anything less, or no
    `reading`, is `infrastructure` and `attribute` is never called. A diagnosis is given at most `Escalation.diagnose_s`
    seconds, in a thread of its own that is given up on when they run out."""

    def diagnose(self, left: list[dict[str, Any]], timeout_s: float) -> dict[str, Any] | None: ...


@dataclasses.dataclass(frozen=True)
class Escalation:
    """What the deadline does past the kill: its grace period, the mount's abort (given the seconds it may take; it
    returns a record with `ok`: True, False, or None when it could not be attempted), and its clock.

    `restorable` says the backend can bring the mount back after an abort; with `attribute` it is what lets a freed
    survivor be the agent's (the module's docstring). `attribute(diagnostics)` is a pure function of what `diagnose`
    read at the `survived` step (None when it read nothing): it answers `{"verdict": "agent" | "infrastructure", "why":
    ..., "target": ..., "space_holder": ...}`, and reads what the study holds down *when it is called*, at that step,
    never a value taken when the sandbox started. Both default to nothing: a `restorable` escalation with no
    `attribute`, an answer that is not a verdict, or one that raises, is `infrastructure`: it fails closed."""

    grace_s: float
    abort: Callable[[float], dict[str, Any]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_s: float = POLL_S
    restorable: bool = False
    attribute: Callable[[dict[str, Any] | None], Mapping[str, Any]] | None = None
    diagnose_s: float = (
        DIAGNOSE_S  # the most a diagnosis may take; less when the margin left is short (`_Run.diagnose`)
    )

    @property
    def margin_s(self) -> float:
        return margin_s(self.grace_s)


def ends_run(record: Mapping[str, Any]) -> bool:
    """Whether a cell's deadline record ends the run: every escalation does (`ESCALATED`) but a `mount_aborted` whose
    restore is pending, under way or not needed, and `mount_restored`; `killed` is the cell's own time limit and ends
    nothing. It fails closed: an outcome or a restore state it does not know ends the run, so a record from a newer
    writer never leaves a dead mount in use."""
    outcome = record.get("outcome")
    if outcome in (KILLED, MOUNT_RESTORED):
        return False
    return not (outcome == MOUNT_ABORTED and record.get("restore") in RESTORE_KEEPS_RUN)


def key_of(proc: dict[str, Any]) -> tuple[Any, Any, Any]:
    """What identifies a listed process: its pid, its start tick and, where the listing reads it, the digest of its
    command line. A process that execs between two listings has another key, so it is signalled again as what it is
    now (O72)."""
    return proc["pid"], proc.get("start"), proc.get("cmd_sha256")


class _Run:
    """One deadline's processes and record."""

    def __init__(self, tracker: Tracker, esc: Escalation) -> None:
        self.tracker, self.esc = tracker, esc
        self.seen: set[tuple[Any, Any, Any]] = set()  # the processes a signal reached
        self.unreached: set[tuple[Any, Any]] = set()  # the (pid, start) of what was asked for and is not reached yet
        self.doing: str | None = None  # the command under way, named when it raises
        self.reached = KILLED  # the most severe step taken: no outcome is less (`reach`, `done`)
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
        """SIGKILL what was not reached yet, within the time left until `until`; how many were reached. A process the
        tracker says it did not reach is kept in `unreached`, not marked done, and asked for again by the next call."""
        fresh = [p for p in procs if key_of(p) not in self.seen]
        budget = self.left(until)
        if fresh and budget > 0:
            answer = self.command("kill", self.tracker.kill, fresh, budget)
            asked = {key_of(p) for p in fresh}
            done = asked if answer is None else asked & set(answer)
            self.seen.update(done)
            # by pid and start, not by key: a process declined as it execed and reached under its new command line is
            # one process, and is no longer unreached
            self.unreached = (self.unreached | {key[:2] for key in asked}) - {key[:2] for key in self.seen}
            return len(done)
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
        self.step(
            "kill",
            signalled=signalled if self.seen or procs is not None else None,
            **({"unreached": len(self.unreached)} if self.unreached else {}),
        )
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

    def diagnose(self, left: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """What the survivors are blocked on, read by the tracker (`Diagnosing`) at the `survived` step, before the
        mount is aborted; its keys are merged into the record (`DIAGNOSTIC_KEYS`, nothing else). It is bounded: at most
        `Escalation.diagnose_s`, and never so long that the abort and the second grace lack their time
        (`ABORT_RESERVE_S` beyond the grace). It runs in a daemon thread that is joined for that long and given up on
        after, so a read that never returns (a survivor in D holds the mmap lock `/proc/<pid>/mem` and `maps` take)
        never holds the timer's thread: the step `diagnosis_timed_out` is recorded, and the answer is None. None too
        when the tracker cannot diagnose, nothing is left to read, and when the reading raised (a `raised` step).
        Never raises: the abort that follows is what frees the cell."""
        diagnose = getattr(self.tracker, "diagnose", None)
        if diagnose is None or not left:
            return None
        budget = min(self.esc.diagnose_s, self.left() - self.esc.grace_s - ABORT_RESERVE_S)
        if budget <= 0:
            self.step("diagnosis_timed_out", budget_s=0.0, why="no time was left for it")
            return None
        box: dict[str, Any] = {}
        listed = [dict(p) for p in left]

        def read() -> None:
            try:
                box["found"] = diagnose(listed, budget)
            except Exception as error:
                box["error"] = error

        reader = threading.Thread(target=read, name="cell-diagnosis", daemon=True)
        reader.start()
        reader.join(budget)
        if reader.is_alive():
            self.step("diagnosis_timed_out", budget_s=round(budget, 3))
            return None
        if "error" in box:
            self.step("raised", during="diagnosis", error=f"the diagnosis raised {type(box['error']).__name__}")
            return None
        found = box.get("found")
        if not isinstance(found, Mapping):
            return None
        self.record.update({key: found[key] for key in DIAGNOSTIC_KEYS if key in found})
        return dict(found)

    @staticmethod
    def covers(left: list[dict[str, Any]], diagnostics: Mapping[str, Any] | None) -> bool:
        """Whether a diagnosis read every survivor listed: its `reading` holds an entry with a `pid` and a `start` for
        each `(pid, start)` of `left`, and every entry holds both. Nothing less is a verdict (`attribution`)."""
        reading = diagnostics.get(READING) if isinstance(diagnostics, Mapping) else None
        if not isinstance(reading, list) or not reading:
            return False
        if not all(isinstance(e, Mapping) and "pid" in e and "start" in e for e in reading):
            return False
        read = {(e["pid"], e["start"]) for e in reading}
        return all((p.get("pid"), p.get("start")) in read for p in left)

    def attribution(self, left: list[dict[str, Any]] | None, diagnostics: dict[str, Any] | None) -> str | None:
        """Whose the survivors are, asked of `Escalation.attribute` at the `survived` step and recorded as the record's
        `attribution`; the verdict. None when the escalation has no `attribute` (nothing is recorded: a backend that
        does not attribute keeps its record as it was). Fails closed: survivors that could not be listed (`left` None:
        unknown is never gone, so never the agent's), no diagnosis, one that does not cover every survivor listed
        (`covers`: `attribute` is never called on a reading of some of them), an answer that raises, is no mapping or
        names no verdict is `infrastructure` with the reason `UNKNOWN_BLOCK`, so a restore is never made pending by a
        guess."""
        attribute = self.esc.attribute
        if attribute is None:
            return None
        answer: Any = None
        if left is not None and self.covers(left, diagnostics):
            try:
                answer = attribute(diagnostics)
            except Exception as error:
                self.step("raised", during="attribution", error=f"the attribution raised {type(error).__name__}")
        if not isinstance(answer, Mapping) or answer.get("verdict") not in VERDICTS:
            answer = {"verdict": INFRASTRUCTURE, "why": UNKNOWN_BLOCK}
        self.record["attribution"] = {key: answer[key] for key in ATTRIBUTION_KEYS if key in answer}
        return str(answer["verdict"])

    def reach(self, outcome: str) -> None:
        """A step is taken that no later outcome may fall below: the mount aborted, or the sandbox's stop reached.
        Called before whatever can raise next, so that the fallback run for a raise still knows of it."""
        self.reached = max(self.reached, outcome, key=SEVERITY.index)

    def done(self, outcome: str, *, restore: bool = False) -> dict[str, Any]:
        """Record the outcome, the one place that does: never less severe than what was reached. One that orders the
        sandbox's stop has that step recorded, if it is not yet (the caller runs the stop once this is recorded).
        `restore` makes a `mount_aborted` outcome one whose restore is pending, the one place that does: it is asked
        only by the path where the abort worked, every process was shown gone, the escalation can restore and its
        attribution is the agent's (`_escalate`); any outcome of more severity is never made restorable."""
        outcome = max(outcome, self.reached, key=SEVERITY.index)
        if outcome in STOPS and not any(s["step"] == "stop" for s in self.record["steps"]):
            self.step("stop")
        self.record["outcome"] = outcome
        if restore and outcome == MOUNT_ABORTED:
            self.record["restore"] = RESTORE_PENDING
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
    returns what is left (None when unknown); what it finds is never less severe than the mount's abort or the stop,
    when one was reached. Never raises for what the escalation's commands or the abort do."""
    run = _Run(tracker, esc)
    try:
        return _escalate(run, esc)
    except Exception as error:
        what = run.doing or "escalation"
        run.step("raised", during=what, error=f"the {what} raised {type(error).__name__}")
        return _fall_back(run, fallback)


def _fall_back(run: _Run, fallback: Callable[[], list[dict[str, Any]] | None] | None) -> dict[str, Any]:
    """After a step that raised: what `fallback` leaves of the cell. Every process shown gone is `killed`, unless the
    mount was aborted or the stop reached before (`_Run.reached`): then that, and never a clean kill; a process left, in
    whatever state, or none that could be listed, is `survived_kill`, with the stop ordered (`_Run.done` records it)."""
    try:
        left = fallback() if fallback is not None else None
    except Exception as error:
        left = None
        run.step("fallback", error=f"the fallback raised {type(error).__name__}")
    if left == []:
        run.step("ended")
        return run.done(KILLED)
    run.survivors(left)
    return run.done(SURVIVED)


def _escalate(run: _Run, esc: Escalation) -> dict[str, Any]:
    left = run.wait(run.kill())
    if left == []:
        run.step("ended")
        return run.done(KILLED)
    run.survivors(left)
    # read what the survivors wait on, and whose it is, before anything is aborted: the abort ends their wait. Asked
    # once, here: the later `survivors` steps are of a mount already dead
    verdict = run.attribution(left, run.diagnose(left))
    budget = run.left()
    abort = _attempt(lambda: esc.abort(budget), "abort") if budget > 0 else {"ok": False, "why": "no time was left"}
    run.step("abort", **abort)
    # from here the mount is dead (an abort that worked) or the stop is the only step left (one that did not): a raise
    # in the wait below, and the fallback it ends in, may not make of this a clean kill
    run.reach(MOUNT_ABORTED if abort.get("ok") is True else SANDBOX_STOPPED)
    if abort.get("ok") is True and run.left() > 0:
        left = run.wait(left)
        if left == []:
            run.step("ended")
            return run.done(MOUNT_ABORTED, restore=esc.restorable and verdict == AGENT)
        run.survivors(left)
    over = run.left() <= 0
    if over:  # waited no longer: whatever runs still is the stop's
        run.step("margin", margin_s=esc.margin_s)
    return run.done(SURVIVED if over else SANDBOX_STOPPED)  # `done` orders the stop: the caller runs it once recorded


__all__ = [
    "ABORT_RESERVE_S",
    "AGENT",
    "ATTRIBUTION_KEYS",
    "COMMANDS_S",
    "DIAGNOSE_S",
    "DIAGNOSTIC_KEYS",
    "ESCALATED",
    "INFRASTRUCTURE",
    "KILLED",
    "MOUNT_ABORTED",
    "MOUNT_LOST",
    "MOUNT_RESTORED",
    "OUTCOMES",
    "READING",
    "RECORDED",
    "RESTORE",
    "RESTORE_DONE",
    "RESTORE_FAILED",
    "RESTORE_KEEPS_RUN",
    "RESTORE_NOT_NEEDED",
    "RESTORE_PENDING",
    "RESTORE_RUNNING",
    "RESTORE_STATES",
    "SANDBOX_STOPPED",
    "SEVERITY",
    "STOPS",
    "SURVIVED",
    "SWEEP",
    "UNKNOWN_BLOCK",
    "VERDICTS",
    "Diagnosing",
    "Escalation",
    "Tracker",
    "ends_run",
    "enforce",
    "margin_s",
]
