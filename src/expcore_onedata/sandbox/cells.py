# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:807-1085 (exec, kill facts per cell, quiesce)
#   changes: the cell runner is its own class over HostOps; the earlier study's tracer and cell tags are not carried
"""Running the agent's code in the sandbox, and saying why a cell was killed (O17).

One cause per cell, whatever the exit code: the cgroup's counters and the container's state are read after every cell,
because a killed worker may leave a parent that exits 1, or 0, and a cell may exit 137 or 143 by itself; the journal of
the host's out-of-memory killer likewise. An exit code alone never makes a kill. A cell that printed
bytes that are not UTF-8 is the agent's own outcome: they are replaced, never an exception. The only time limit is the
`timeout_s` the caller sets, so no hidden per-cell limit can creep back.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from expcore_onedata.sandbox import earlyoom as EO
from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox import teardown as TD
from expcore_onedata.sandbox.api import ExecResult
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
    never has a fired timer, and one this timer ended always has. No time limit is inferred from how long a cell ran."""

    def __init__(self, seconds: float, kill: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._ended = self._fired = False
        self._kill = kill
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._ended:
                return
            self._fired = True
        self._kill()

    def end(self) -> bool:
        """The cell has ended: from now on the timer does nothing. Whether it fired, after its kill has finished, so
        that what is read next is read after it."""
        with self._lock:
            self._ended = True
            fired = self._fired
        self._timer.cancel()
        if fired and self._timer.is_alive():
            self._timer.join()
        return fired


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
    ) -> None:
        self._host, self._obs, self._names, self._s = host, observer, names, settings
        self._meta, self._registry, self.roles, self._started = meta, registry, roles, started_epoch
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

    # ------------------------------------------------------------------------------------------------ processes
    def agent_processes(self) -> list[dict] | None:
        """Every process of the agent's container except its init and the init's `sleep`; [] when it has stopped,
        None when that cannot be read."""
        info = TD._inspect(self._host, self._names.agent, TD.WAITS)
        if info is None:
            return []
        if info.get("unknown"):
            return None
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

    def _kill_at_the_cap(self) -> None:
        """In the timer's thread, when a cell's time runs out: every process of the agent's except its init and `sleep`,
        the cell's own and any worker it detached. It never raises."""
        try:
            self.quiesce("time cap")
        except Exception as e:
            log.error("killing the processes of a cell at its cap failed: %s", describe_error(e))

    # ------------------------------------------------------------------------------------------------ the cell
    def run(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult:
        """Run Python source in the container as the unprivileged user (cwd /work); the only time limit is `timeout_s`.

        Its clock starts here, not when Docker gets round to starting the process. When it runs out, the harness's own
        timer records that it fired and kills every process of the agent's container, a detached worker included; the
        container and the run's evidence stay. `timeout` inside the container is only a backstop, `CAP_MARGIN_S` later,
        should that kill not land. A time limit is that record and nothing else: never the elapsed time, nor 137 (O17).
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
        t0, wall0 = time.monotonic(), time.time()
        timer = CellTimer(max(0.0, remaining), self._kill_at_the_cap)
        gave_up = fired = False
        try:
            if remaining < 1:  # used up before it could start: the harness's clock ran out, so its timer ended it
                rc, out, err = 124, "", "the cell was not started: its time ran out during preparation"
                gave_up = fired = True
            else:
                if record:  # the probes kill nothing of the agent's
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
        entry: dict[str, Any] = {"cell": len(self.cells) + 1, "exit_code": rc, "duration_s": res.duration_s}
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
        if res.kill:  # from what caused it, not from an exit code that a memory kill shares
            res.timed_out = entry["timed_out"] = gave_up or res.kill["reason"] == "time_limit"
        entry["timed_out"] = res.timed_out
        if res.timed_out:  # now, and not only from the timer: a worker that closed the pipes must not run on
            self.quiesce("time cap")
        self.cells.append(entry)

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
