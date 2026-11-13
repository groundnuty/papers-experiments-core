# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:807-1085 (exec, kill facts per cell, quiesce)
#   changes: the cell runner is its own class over HostOps; the earlier study's tracer and cell tags are not carried
"""Running the agent's code in the sandbox, and saying why a cell was killed (O17).

One cause per cell, whatever the exit code: the cgroup's counters and the container's state are read after every cell,
because a killed worker may leave a parent that exits 1, or 0, and a cell may exit 137 or 143 by itself; the journal of
the host's out-of-memory killer likewise. An exit code alone never makes a kill. A cell that printed
bytes that are not UTF-8 is the agent's own outcome: they are replaced, never an exception. The only time limit is the
`timeout_s` the caller sets, so no hidden per-cell limit can creep back.

That limit is a hard deadline (deadline.py, O30): at the cap every process of the cell is killed, and one that outlives
the kill past the grace period has the mount aborted under it, or the sandbox stopped. Its record is the cell's
`deadline`, and a cell that outlived its kill is `kill_survived`, infrastructure, and ends the run (`ended`). The record
is published from the timer's thread under `meta_lock`, before the sandbox's stop, which runs after it.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import earlyoom as EO
from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox import teardown as TD
from expcore_onedata.sandbox.api import NOT_STARTED, RAN, ExecResult
from expcore_onedata.sandbox.containers import Names
from expcore_onedata.sandbox.hostops import TIMED_OUT, HostOps, describe_error
from expcore_onedata.sandbox.observe import Observer
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.settings import SandboxSettings

log = logging.getLogger(__name__)

SIGNAL_EXITS = (137, 143, -9, -15)  # a cell that ended by SIGKILL or SIGTERM, what earlyoom sends
CAP_MARGIN_S = 1.0  # `timeout` inside the container is the backstop: it fires this long after the harness's own timer
QUIESCE_ROUNDS = (
    5  # a forking worker can start a child between the listing and the kill: listed again until none is new
)
HOST_CALL_MARGIN_S = 30.0  # what the docker client may take beyond the cell's own limit before it is given up on
KILL_S = 20.0  # a `kill` of the agent's processes through the host's privilege
SETTLE_SLACK_S = 30.0  # beyond a deadline's margin, what its commands' own timeouts may overrun it by


def docker_failed(rc: int, err: str) -> bool:
    """`docker exec`, or the `timeout` under it, could not run the cell, as they say themselves on stderr. Not an exit
    code or words alone: a cell may exit 126, or print "the scheduler is not running" in its traceback, and either is
    its own outcome."""
    if rc == 0 or not any(m in err for m in ("Error response from daemon", "OCI runtime", "docker: ", "timeout: ")):
        return False
    return rc in (125, 126) or "is not running" in err or "No such container" in err


class CellTimer:
    """The harness's own timer of a cell, the one that decides `time_limit` (O17). At the cell's limit it records that
    it fired, then kills what runs; the record and the cell's end are taken under one lock, so a cell that ended first
    never has a fired timer, and one this timer ended always has. No time limit is inferred from how long a cell ran.

    The kill may hand back what is to run once its outcome is decided (the sandbox's stop a deadline ordered, O30): the
    timer runs it after it marks the outcome decided, so `end`, which waits for the outcome, never waits for that."""

    def __init__(self, seconds: float, kill: Callable[[], Callable[[], None] | None]) -> None:
        self._lock = threading.Lock()
        self._ended = self._fired = False
        self._decided = threading.Event()  # the kill has recorded its outcome
        self._kill = kill
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def _claim(self) -> bool:
        """Mark the timer fired, unless the cell has ended or it already has; whether this call did."""
        with self._lock:
            if self._ended or self._fired:
                return False
            self._fired = True
            return True

    def _fire(self) -> None:
        if self._claim():
            self._run_kill()

    def _run_kill(self) -> None:
        then = None
        try:
            then = self._kill()
        finally:
            self._decided.set()
        if then is not None:
            then()

    def fire_now(self, timeout_s: float) -> bool | None:
        """The run's body is over while the cell still runs (the loop cut it at the agent's cap, which is the cell's):
        fire now, in a thread of the timer's own, unless the cell has ended or the timer has fired; then wait for the
        outcome, at most `timeout_s`. Whether the timer fired; None when its outcome was not decided in time."""
        if self._claim():
            self._timer.cancel()
            threading.Thread(target=self._run_kill, name="cell-deadline", daemon=True).start()
        return self.wait_decided(timeout_s)

    def wait_decided(self, timeout_s: float) -> bool | None:
        """Whether the timer fired, once its kill's outcome is decided, waited for at most `timeout_s`; never fires it.
        None when it fired and the outcome was not decided in time."""
        with self._lock:
            fired = self._fired
        if fired and not self._decided.wait(timeout_s):
            return None
        return fired

    def end(self) -> bool:
        """The cell has ended: from now on the timer does nothing. Whether it fired, after its kill's outcome is
        decided, so that what is read next is read after it."""
        with self._lock:
            self._ended = True
            fired = self._fired
        self._timer.cancel()
        if fired:
            self._decided.wait()
        return fired


class _CellProcesses:
    """The deadline's tracker (deadline.Tracker): the cell's processes are every process of the agent's container but
    its init and the init's `sleep`, from its cgroup, so a worker in a session of its own is one; SIGKILLed through the
    host's privilege, as the quiesce does. Each command is given at most the time the deadline has left for it."""

    def __init__(self, runner: CellRunner) -> None:
        self._runner = runner

    def processes(self, timeout_s: float) -> list[dict] | None:
        try:
            return self._runner.agent_processes(timeout_s)
        except Exception as e:  # unknown, never none
            log.error("the processes of a cell could not be listed at its deadline: %s", describe_error(e))
            return None

    def kill(self, procs: list[dict], timeout_s: float) -> None:
        pids = [str(p["pid"]) for p in procs]
        TD._sudo(self._runner._host, "kill", "-s", "KILL", *pids, timeout_s=min(KILL_S, timeout_s))


class CellRunner:
    """Runs cells in one agent container and keeps what is known about their kills."""

    def __init__(
        self,
        host: HostOps,
        observer: Observer,
        names: Names,
        settings: SandboxSettings,
        meta: dict[str, Any],
        registry: Registry,
        roles: dict[str, str],
        started_epoch: float,
        escalation: DL.Escalation | None = None,
        stop: Callable[[], dict[str, Any]] | None = None,
        meta_lock: threading.RLock | None = None,
    ) -> None:
        self._host, self._obs, self._names, self._s = host, observer, names, settings
        self._meta, self._registry, self.roles, self._started = meta, registry, roles, started_epoch
        # past the kill at a cell's deadline: by default the grace alone, with nothing to abort and no stop
        self._escalation = escalation or DL.Escalation(settings.kill_grace_s, lambda _: {"ok": None, "why": "no abort"})
        self._stop = stop or (lambda: {"ok": None, "why": "no stop"})  # the sandbox's, after a deadline's outcome
        # what the deadline's thread writes into the metadata, and whoever reads it whole (`DockerSandbox.metadata`)
        self.meta_lock = meta_lock or threading.RLock()
        # the agent's init's `sleep`, by pid and start time from the container's start (`teardown.init_sleep`): the one
        # process besides the init that a kill of the agent's processes spares; a process merely named `sleep` is killed
        self.init_sleep: dict[str, Any] | None = None
        self._deadline: dict[str, Any] | None = None  # the record of the running cell's deadline, once it fired
        self.ended: dict[str, Any] | None = None  # the deadline that ended the run: a process outlived its kill
        self._running: CellTimer | None = None  # the timer of the cell running now, while its `run` lasts
        self.cells: list[dict[str, Any]] = []
        self._events_seen: dict[str, int] | None = None
        self._earlyoom_seen = 0
        self._journal_kills: list[EO.Kill] = []
        self._seen_pids: dict[str, dict[int, list[list[float]]]] = {}
        self._pid_lock = threading.Lock()

    # ------------------------------------------------------------------------------------------------ sampling
    def sample_pids(self) -> None:
        """Host PIDs in each of this run's containers, kept for the whole run: what the host's killer names is gone from
        /proc by the time anyone reads it, so this is how a kill is traced to its run."""
        now = time.time()
        with self._pid_lock:
            for role, container in self.roles.items():
                text = self._obs.procs(container)
                if text is None:
                    continue  # the container has gone; what was seen stays
                EO.note_pids(self._seen_pids.setdefault(role, {}), text, now)
                self._registry.note_pids(set(self._seen_pids[role]))

    def earlyoom_scan(self) -> list[dict]:
        """The earlyoom kills of this run so far, kept in the metadata. An unreadable journal is unknown, never none."""
        self.sample_pids()  # what a cell that has just ended left behind
        kills = self._obs.earlyoom_kills(self._started)
        self._meta["earlyoom_log_readable"] = kills is not None
        prev = self._meta.setdefault("earlyoom_kills", [])
        if kills is None:  # not known: what is already on record stays
            return prev
        self._journal_kills = kills
        found = prev + [k for k in EO.attribute(kills, self._seen_pids) if k not in prev]
        self._meta["earlyoom_kills"] = found
        return found

    def step_earlyoom(self) -> list[dict] | None:
        """After a harness step (O24): the host killer's kills of this run since the last cell or step, which are the
        step's, moved past so that the agent's next cell never takes them. None when its journal could not be read."""
        try:
            found = self.earlyoom_scan()
        except Exception as e:
            self._meta["earlyoom_log_readable"] = False
            self._meta["earlyoom_scan_error"] = describe_error(e)
            return None
        if self._meta.get("earlyoom_log_readable") is not True:
            return None
        new, self._earlyoom_seen = found[self._earlyoom_seen :], len(found)
        return new

    # ------------------------------------------------------------------------------------------------ processes
    def agent_processes(self, timeout_s: float | None = None) -> list[dict] | None:
        """Every process of the agent's container except its init and the init's `sleep` (`init_sleep`, O30); [] when it
        has stopped, None when that cannot be read (within `timeout_s`, when given)."""
        waits = TD.WAITS
        if timeout_s is not None:
            waits = dataclasses.replace(waits, command_s=min(waits.command_s, timeout_s))
        info = TD._inspect(self._host, self._names.agent, waits)
        if info is None:
            return []
        if info.get("unknown"):
            return None
        if self.init_sleep is not None:
            info = {**info, "sleep": self.init_sleep}
        return TD._readers(self._host, info) if info["running"] else []

    def quiesce(self, reason: str = "end of run") -> dict[str, Any]:
        """Kill everything the agent left running, before any evidence is collected: a worker started in a new session
        is outside `timeout`'s group, and could go on changing outputs after the cap. The container stays up, since its
        cgroup's counters classify the kills and a mount has its own teardown; only its init and `sleep` remain.
        `killed` None means the processes could not be listed, so nothing is known. It never raises."""
        state: dict[str, Any] = {"reason": reason, "killed": None, "left": None}
        try:
            killed, left, seen = 0, [], set()
            for _ in range(QUIESCE_ROUNDS):
                procs = self.agent_processes()
                if procs is None:
                    killed = None
                    break
                left = procs
                fresh = [p for p in procs if (p["pid"], p["start"]) not in seen]
                if not fresh:
                    break
                TD._sudo(self._host, "kill", "-s", "KILL", *[str(p["pid"]) for p in fresh], timeout_s=20.0)
                seen.update((p["pid"], p["start"]) for p in fresh)
                killed += len(fresh)
                left = TD._wait_gone(self._host, fresh, 10.0, 0.2)
            else:
                left = self.agent_processes()
                killed = None if left is None else killed
            if killed is not None:
                state.update(killed=killed, left=[{k: p[k] for k in ("pid", "comm", "state")} for p in left[:16]])
        except Exception as e:
            state.update(killed=None, left=None, error=describe_error(e))
        self._meta.setdefault("quiesced", []).append(state)
        return state

    @staticmethod
    def quiet(state: dict[str, Any]) -> bool:
        """Nothing of the agent's can run any more. A process in D (a read of a slow mount) or a zombie has
        SIGKILL pending or is dead: it returns to no user code, so it changes nothing."""
        return state["killed"] is not None and not [p for p in state["left"] if p["state"] not in ("D", "Z")]

    def _kill_at_the_cap(self, cell: int) -> Callable[[], None] | None:
        """In the timer's thread, when a cell's time runs out: its deadline (deadline.py). Every process of the agent's
        except its init and `sleep`, the cell's own and any worker it detached, is killed, and one that outlives the
        kill past the grace has the mount aborted or the sandbox's stop ordered. The record is published under the
        metadata's lock, under `cell_deadlines` and, when a process outlived the kill, as `ended`: the run is classified
        before anything else happens. The stop is handed back to the timer, which runs it only once the outcome is
        decided, so its teardown neither holds up the classification nor counts in the cell's time. It never raises."""
        try:
            record = DL.enforce(_CellProcesses(self), self._escalation)
        except Exception as e:
            log.error("the deadline of a cell failed: %s", describe_error(e))
            self.quiesce("time cap")
            return None
        outcome = record["outcome"]
        with self.meta_lock:
            self._deadline = record
            self._meta.setdefault("cell_deadlines", []).append({"cell": cell, **record})  # shares the record's steps
            if outcome in DL.ESCALATED:  # the run is over from now, whatever the cell's record makes of it after
                self.ended = record
        if outcome in DL.ESCALATED:
            log.error("a process of a cell outlived its kill at the cap: the deadline ended %s", outcome)
        return (lambda: self._stop_after(record)) if outcome in DL.STOPS else None

    def settle(self) -> dict[str, Any] | None:
        """The run's body is over (`launch` leaves it, or `collect` begins): a cell still running is past its cap, the
        agent's own, so its deadline is enforced now if its timer has not fired, and one under way is waited for, at
        most its margin and `SETTLE_SLACK_S`. Only then may anyone decide how the run ended (O30). The deadline that
        ended the run, or None; a deadline whose outcome was not decided in that time ends it too, as `survived_kill`:
        unknown is never a clean kill. It never raises."""
        with self.meta_lock:
            timer = self._running
        if timer is not None and timer.fire_now(self._escalation.margin_s + SETTLE_SLACK_S) is None:
            log.error("a cell's deadline was not decided within its margin: the run is ended as %s", DL.SURVIVED)
            with self.meta_lock:
                self.ended = self.ended or {"outcome": DL.SURVIVED, "why": "not decided within its margin", "steps": []}
        with self.meta_lock:
            return self.ended

    def wait_decided(self) -> None:
        """Wait for the outcome of a cell's deadline already under way, at most its margin and `SETTLE_SLACK_S`; never
        fire one. A stop calls this first, so that its teardown never races an escalation (O30)."""
        with self.meta_lock:
            timer = self._running
        if timer is not None:
            timer.wait_decided(self._escalation.margin_s + SETTLE_SLACK_S)

    def _stop_after(self, record: dict[str, Any]) -> None:
        """The sandbox's stop a deadline ordered, once its outcome is recorded; how it went is the record's last step,
        `stopped`. It never raises."""
        began = time.monotonic()
        try:
            note = dict(self._stop())
        except Exception as e:
            note = {"ok": False, "error": f"the stop raised {type(e).__name__}"}
        with self.meta_lock:
            record["steps"].append({"step": "stopped", "teardown_s": round(time.monotonic() - began, 3), **note})

    # ------------------------------------------------------------------------------------------------ the cell
    def run(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult:
        """Run Python source in the container as the unprivileged user (cwd /work); the only time limit is `timeout_s`.

        Its clock starts here, not when Docker gets round to starting the process. When it runs out, the harness's own
        timer records that it fired and kills every process of the agent's container, a detached worker included; the
        container and the run's evidence stay. `timeout` inside the container is only a backstop, `CAP_MARGIN_S` later,
        should that kill not land. A time limit is that record and nothing else: never the elapsed time, nor 137 (O17).
        A cell left less than a second once it is prepared is not started at all (`NOT_STARTED`): no timer ran, so none
        fired, and it is no timeout; the loop's own time rule says what the run's time running out means.
        """
        if not timeout_s >= 1:
            raise ValueError("timeout_s must be at least 1 second (GNU timeout treats 0 as no limit)")
        entered = time.monotonic()
        agent, uid = self._names.agent, self._s.agent_uid
        before = (self._events_seen or self._obs.memory_facts(agent)["events"]) if record else None
        up0 = self._obs.uptime() if record else None
        remaining = timeout_s - (time.monotonic() - entered)
        limit = f"{max(remaining, 1.0) + CAP_MARGIN_S:.3f}"  # the backstop; the harness's timer below comes first
        argv = [
            "exec",
            "-i",
            "-u",
            f"{uid}:{uid}",
            "-w",
            "/work",
            agent,
            "timeout",
            "-s",
            "KILL",
            limit,
            self._s.python,
            "-",
        ]
        if remaining < 1:  # too little left to start it: nothing runs, no timer was started, so none fired
            return self._not_started(record)
        t0, wall0 = time.monotonic(), time.time()
        with self.meta_lock:
            self._deadline = None  # this cell's, once its timer fires
        cell = len(self.cells) + 1
        timer = CellTimer(remaining, lambda: self._kill_at_the_cap(cell))
        gave_up = fired = False
        try:
            if record:  # the probes kill nothing of the agent's
                with self.meta_lock:
                    self._running = timer  # what `settle` enforces when the run's body ends while it runs
                timer.start()
            done = self._host.docker(*argv, input=code, timeout=remaining + HOST_CALL_MARGIN_S, check=False)
            fired = timer.end()
            rc, out, err = done.returncode, done.stdout, done.stderr
            if rc == TIMED_OUT and err.startswith("timed out after"):
                gave_up, err = True, "docker exec did not return"
            used = time.monotonic() - entered
            res = ExecResult(out, err, rc, time.monotonic() - t0, timed_out=gave_up or fired)
            if record:
                self._record_cell(code, res, before, up0, used, timeout_s, gave_up, fired, wall0)
        finally:  # whatever was raised: a timer that has not fired must not kill the next cell's processes
            timer.end()
            with self.meta_lock:
                if self._running is timer:
                    self._running = None
        return res

    def _not_started(self, record: bool) -> ExecResult:
        """A cell its time ran out for while it was being prepared: `NOT_STARTED`, with no exit, no timeout and no kill.
        Only the `CellTimer` firing makes a time limit; a timer that never ran fired nothing (O17)."""
        res = ExecResult("", "the cell was not started: too little of its time was left", -1, 0.0, status=NOT_STARTED)
        if record:
            self.cells.append(
                {
                    "cell": len(self.cells) + 1,
                    "status": NOT_STARTED,
                    "exit_code": None,
                    "duration_s": 0.0,
                    "timed_out": False,
                }
            )
        return res

    def _record_cell(
        self,
        code: str,
        res: ExecResult,
        before: dict[str, int] | None,
        up0: float | None,
        used: float,
        timeout_s: float,
        gave_up: bool,
        fired: bool,
        wall0: float,
    ) -> None:
        rc = res.exit_code
        entry: dict[str, Any] = {
            "cell": len(self.cells) + 1,
            "status": RAN,
            "exit_code": rc,
            "duration_s": res.duration_s,
        }
        events = self._obs.memory_facts(self._names.agent)["events"]
        if events is None:  # not known whether anything of this cell was killed
            self._meta.setdefault("oom_counters_unreadable_cells", []).append(entry["cell"])
        killed = (events or {}).get("oom_kill", 0) > (before or {}).get("oom_kill", 0)
        running = self._obs.running(self._names.agent)  # the container's own state, after every cell, whatever its code
        if running is None:  # not known whether the container died under this cell
            self._meta.setdefault("container_state_unreadable_cells", []).append(entry["cell"])
        # a kill only on the kernel's or the container's evidence, or the harness's own: an OOM kill counted, a
        # container that stopped, the harness's timer or its giving up, docker saying it could not run the cell. Never
        # the exit code alone: `sys.exit(137)` with the counters unchanged and the container running is the agent's
        # own exit (O17)
        if gave_up or fired or killed or running is False or docker_failed(rc, res.stderr):
            # when the harness gave up, the cell had all the time it could have: `used` may be less
            kill = self._obs.kill_facts(
                self._names.agent, before, timeout_s if gave_up else used, timeout_s, up0, timer_fired=fired
            )
            entry["kill"] = res.kill = kill
            self._meta.setdefault("cell_kills", []).append({"cell": entry["cell"], **kill})
        self._events_seen = events
        res.kill = self._with_earlyoom(res, entry, before, up0, used, timeout_s, gave_up, fired, wall0)
        escalated = self._with_deadline(res, entry, fired)
        if res.kill:  # from what caused it, not from an exit code that a memory kill shares
            res.timed_out = entry["timed_out"] = gave_up or res.kill["reason"] in ("time_limit", KC.KILL_SURVIVED)
        entry["timed_out"] = res.timed_out
        if res.timed_out and not escalated:  # now, and not only from the timer: a worker that closed the pipes must
            self.quiesce("time cap")  # not run on (after an escalation the deadline has done all a kill can)
        self.cells.append(entry)

    def _with_deadline(self, res: ExecResult, entry: dict[str, Any], fired: bool) -> bool:
        """The record of the cell's deadline, when its timer fired (the timer's thread has put it under `cell_deadlines`
        already); and when a process of the cell outlived its kill, its kill becomes `kill_survived`, whatever else the
        counters said (after an abort or a stop they describe the harness's doing); the timer's thread has already ended
        the run (`ended`). Whether that happened."""
        with self.meta_lock:
            record = self._deadline if fired else None
            if record is None:
                return False
            entry["deadline"] = record
            if record["outcome"] not in DL.ESCALATED:
                return False
            kill = {**(entry.get("kill") or {}), **KC.survived_cause(record)}
            entry["kill"] = res.kill = kill
            cell_kills = self._meta.setdefault("cell_kills", [])
            if cell_kills and cell_kills[-1]["cell"] == entry["cell"]:
                cell_kills[-1] = {"cell": entry["cell"], **kill}
            else:
                cell_kills.append({"cell": entry["cell"], **kill})
            return True

    def _with_earlyoom(self, res, entry, before, up0, used, timeout_s, gave_up, fired, wall0) -> dict[str, Any] | None:
        """Whatever the exit code, the host's killer's victim may be another container of the run, and the cell may
        survive that; when it does hit this cell its cause is infrastructure."""
        try:
            found = self.earlyoom_scan()
        except Exception as e:
            found = []
            self._meta["earlyoom_log_readable"] = False
            self._meta["earlyoom_scan_error"] = describe_error(e)
        new = found[self._earlyoom_seen :]
        if not new and res.exit_code in SIGNAL_EXITS:
            sampled = [
                k for k in EO.unsampled_kills(self._journal_kills, found, wall0, time.time(), self._s.agent_uid)
                if not self._registry.has_seen(k["pid"])
            ]  # fmt: skip
            if sampled:
                self._meta["earlyoom_kills"] = found = [*found, *sampled]
                new = sampled
        if not new:
            return res.kill
        self._earlyoom_seen = len(found)
        facts = entry.get("kill") or self._obs.kill_facts(
            self._names.agent, before, timeout_s if gave_up else used, timeout_s, up0, timer_fired=fired
        )
        kill = {**facts, **KC.earlyoom_cause(new), "oom_killed": True}
        cell_kills = self._meta.setdefault("cell_kills", [])
        if cell_kills and cell_kills[-1]["cell"] == entry["cell"]:
            cell_kills[-1] = {"cell": entry["cell"], **kill}
        else:
            cell_kills.append({"cell": entry["cell"], **kill})
        entry["kill"] = kill
        self._meta["oom_killed"] = True
        return kill
