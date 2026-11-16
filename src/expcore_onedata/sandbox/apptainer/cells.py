# Provenance: new
"""Running the agent's code as one `apptainer exec` per cell, and saying why a cell was killed (O56, O17, O34, O55).

The runner is the Docker sandbox's `CellRunner` with the node in place of docker: it keeps that runner's deadline,
its publication under the metadata's lock, `settle` and `wait_decided`, and replaces how a cell is run, how its
processes are found and killed, and what says it was killed.

* **The exec** (`argv.exec_argv`, `argv.cell_command`): `timeout -s KILL` in the cell as the backstop, then the
  completion bootstrap (`completion.py`, O55), with the program on the exec's stdin. Its record lines are taken out
  of both streams as they are read and the streams capped (`hostops.run_bounded`), and `Completion.check` reads the
  result: an exec not known to have completed that no timeout and no kill explains raises `ExecIncomplete`. One that
  Apptainer itself could not start (`argv.apptainer_failed`) names that cause: its program did not run.
* **The processes** of the run are the watch's (`watch.Watch.members`): every process in a PID namespace of the
  run's. At the cap the harness's timer kills them through their pidfds (`deadline.enforce`), and one that outlives
  the kill past the grace orders the sandbox's stop (there is no mount to abort). After every exec, whatever of the
  cell is left is killed and shown gone: a cell's processes end with it (`facts.py`, a departure from Docker, where a
  detached worker lives on until the next cap or the collection). One that cannot be shown gone ends the run as
  `survived_kill`.
* **The cause** of a kill, one per cell, from the harness's own evidence only (O17): the watch's kill
  (`memory_limit`, `disk_limit`), an OOM kill the step's memory cgroup counted during the cell (`host_oom`, since that
  cgroup is the harness's and the gate's as well as the cell's), the harness's timer (`time_limit`), a call given up
  on (`unknown`), a process that outlived its kill (`kill_survived`). An exit code alone never makes a kill.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Sequence
from typing import Any

from expcore_onedata.sandbox import bounds as BD
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.api import RAN, ExecResult
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.watch import Watch
from expcore_onedata.sandbox.cells import CAP_MARGIN_S, HOST_CALL_MARGIN_S, QUIESCE_ROUNDS, CellRunner, CellTimer
from expcore_onedata.sandbox.cells import _output_cut as output_cut
from expcore_onedata.sandbox.completion import Completion, ExecIncomplete
from expcore_onedata.sandbox.hostops import TIMED_OUT, describe_error
from expcore_onedata.sandbox.registry import Registry

log = logging.getLogger(__name__)

GONE_WAIT_S = 10.0  # how long the processes of a cell are waited for once they were sent SIGKILL
GONE_POLL_S = 0.2
APPTAINER_FAILED = (
    "apptainer could not run the cell's container (exit {rc}, its own FATAL line on stderr): its program did not run"
)


class _RunProcesses:
    """The deadline's tracker (`deadline.Tracker`): the run's processes, from the watch, killed through their pidfds."""

    def __init__(self, runner: AppCellRunner) -> None:
        self._runner = runner

    def processes(self, timeout_s: float) -> list[dict[str, Any]] | None:
        found = self._runner.watch.members()
        return None if found is None else [p.brief() for p in found]

    def kill(self, procs: list[dict[str, Any]], timeout_s: float) -> None:
        self._runner.kill(procs)

    def fallback(self) -> list[dict[str, Any]] | None:
        state = self._runner.quiesce("time cap")
        return None if state["killed"] is None else state["left"]


class AppCellRunner(CellRunner):
    """Runs cells as `apptainer exec`s on the node and keeps what is known about their kills."""

    def __init__(
        self,
        node: NodeOps,
        watch: Watch,
        argv_of: Callable[[Sequence[str]], list[str]],
        *,
        python: str,
        settings: Any,
        meta: dict[str, Any],
        registry: Registry,
        started_epoch: float,
        escalation: DL.Escalation,
        stop: Callable[[], dict[str, Any]],
        meta_lock: Any,
        output_cap_bytes: int,
        before_exec: Callable[[], None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            node, None, None, settings, meta, registry, {}, started_epoch, escalation, stop=stop, meta_lock=meta_lock,
            output_cap_bytes=output_cap_bytes,
        )  # type: ignore[arg-type]  # fmt: skip
        self.node, self.watch, self._argv_of, self._python = node, watch, argv_of, python
        self._before_exec, self._sleep, self._clock = before_exec, sleep, clock
        self.last_watch: dict[str, Any] = {}  # what the watch saw of the last exec: `seen`, `peak_bytes`, `kills`

    # ------------------------------------------------------------------------------------------------ no earlyoom here
    def sample_pids(self) -> None:
        """No host killer's journal to trace a kill to on this node: nothing to sample."""

    def earlyoom_scan(self) -> list[dict]:
        return []

    def step_earlyoom(self) -> list[dict] | None:
        return []

    # ------------------------------------------------------------------------------------------------ processes
    def agent_processes(self, timeout_s: float | None = None) -> list[dict] | None:
        found = self.watch.members()
        return None if found is None else [p.brief() for p in found]

    def kill(self, procs: Sequence[dict[str, Any]]) -> int:
        """SIGKILL each process through its pidfd, only if it is still the one listed; how many were signalled."""
        return sum(1 for p in procs if self.node.signal(int(p["pid"]), str(p["start"]), signal.SIGKILL) is True)

    def _wait_gone(self, procs: Sequence[dict[str, Any]], wait_s: float) -> list[dict[str, Any]] | None:
        """Those of `procs` still alive after at most `wait_s`; None when that cannot be told."""
        until = self._clock() + wait_s
        while True:
            states = [self.node.alive(int(p["pid"]), str(p["start"])) for p in procs]
            if any(s is None for s in states):
                return None
            left = [p for p, s in zip(procs, states, strict=True) if s]
            if not left or self._clock() >= until:
                return left
            self._sleep(GONE_POLL_S)

    def quiesce(self, reason: str = "end of run") -> dict[str, Any]:
        """Kill every process of the run, in rounds until none new is listed, and show them gone; the state is kept
        under `quiesced`. `killed` None means they could not be listed or shown gone: nothing is known. It never
        raises."""
        state = self._kill_all(reason)
        with self.meta_lock:
            self._meta.setdefault("quiesced", []).append(state)
        return state

    def _kill_all(self, reason: str) -> dict[str, Any]:
        """`quiesce` without its record."""
        state: dict[str, Any] = {"reason": reason, "killed": None, "left": None}
        try:
            killed, seen, left = 0, set(), []
            for _ in range(QUIESCE_ROUNDS):
                procs = self.agent_processes()
                if procs is None:
                    killed = None
                    break
                fresh = [p for p in procs if (p["pid"], p["start"]) not in seen]
                left = procs
                if not fresh:
                    break
                killed += self.kill(fresh)
                seen.update((p["pid"], p["start"]) for p in fresh)
                left = self._wait_gone(fresh, GONE_WAIT_S)
                if left is None:
                    killed = None
                    break
            if killed is not None:
                state.update(killed=killed, left=[{k: p[k] for k in ("pid", "comm", "state")} for p in left[:16]])
        except Exception as error:
            state.update(killed=None, left=None, error=describe_error(error))
        return state

    def _kill_at_the_cap(self, cell: int) -> Callable[[], None] | None:
        """In the timer's thread, at the cell's cap: its deadline (`deadline.enforce`) over the run's processes."""
        procs = _RunProcesses(self)
        try:
            record = DL.enforce(procs, self._escalation, fallback=procs.fallback)
        except Exception as error:
            log.error("the deadline of a cell failed: %s", describe_error(error))
            record = self._unknown(f"the deadline raised {type(error).__name__}")
        return self._publish(cell, record)

    def _sweep(self, cell: int) -> int | None:
        """After the exec: kill what is left of the run's processes and show it gone; forget the namespaces that are.
        How many were left; None when that is unknown. A process that cannot be shown gone ends the run (O34)."""
        before = self.watch.namespaces()
        state = self._kill_all("end of cell")
        if state["killed"] != 0:  # something was left, or nothing is known: on the record
            with self.meta_lock:
                self._meta.setdefault("quiesced", []).append({"cell": cell, **state})
        if not self.quiet(state):
            log.error("a process of a cell outlived its exec and its kill: the run is ended as %s", DL.SURVIVED)
            with self.meta_lock:
                self.ended = self.ended or self._unknown("a process of the cell outlived its exec and its kill")
            return None
        self.watch.forget(before)
        return state["killed"]

    # ------------------------------------------------------------------------------------------------ the cell
    def run(self, code: str, timeout_s: float, *, record: bool = True) -> ExecResult:
        """Run Python source as one `apptainer exec`; the only time limit is `timeout_s` (`cells.CellRunner.run`)."""
        if not timeout_s >= 1:
            raise ValueError("timeout_s must be at least 1 second (GNU timeout treats 0 as no limit)")
        entered = time.monotonic()
        completion = Completion.for_source(code)
        self._before_exec()  # the image is still the one pinned (O13)
        cgroup_before = self.node.cgroup_memory() if record else None
        remaining = timeout_s - (time.monotonic() - entered)
        limit_s = max(remaining, 1.0) + CAP_MARGIN_S
        if remaining < 1:
            return self._not_started(record)
        argv = self._argv_of(AV.cell_command(limit_s, completion.argv(self._python)))
        cell = len(self.cells) + 1
        with self.meta_lock:
            self._deadline = None
        timer = CellTimer(remaining, lambda: self._kill_at_the_cap(cell))
        self.watch.begin_cell(cell, completion.nonce)
        fired = timer_ended = watch_ended = False
        try:
            if record:
                with self.meta_lock:
                    self._running = timer
                timer.start()
            t0 = time.monotonic()
            done = self.node.apptainer(
                argv, remaining + HOST_CALL_MARGIN_S, input=code, output_cap=self._output_cap, marker=completion.marker
            )
            duration = time.monotonic() - t0
            (fired, deadline), timer_ended = self._end(timer, cell), True
            left = self._sweep(cell)
            watched, watch_ended = self.watch.end_cell(), True
            self.last_watch = watched
            rc, out, err = done.returncode, done.stdout, done.stderr
            gave_up = rc == TIMED_OUT and err.startswith("timed out after")
            if gave_up:
                err = "apptainer exec did not return"
            check = completion.check(done)
            res = ExecResult(out, err, rc, duration, timed_out=gave_up or fired, output_cut=output_cut(done, gave_up),
                             exec_check=check.facts)  # fmt: skip
            bound = BD.named(fired=fired, gave_up=gave_up, exit_code=rc, ran_s=duration, limit_s=limit_s)
            res.kill = self._cause(fired, gave_up, watched, cgroup_before, timeout_s)
            incomplete = None
            if check.cause is not None and not gave_up and not fired:
                said = check.cause.replace("docker exec", "apptainer exec")  # the shared check's words, this exec's
                incomplete = APPTAINER_FAILED.format(rc=rc) if AV.apptainer_failed(rc, err) else said
            if record:
                self._record_cell(res, cell, deadline, watched, left, incomplete, gave_up)
                res.bound = bound if res.timed_out else None
            else:  # no timer ran: only the container's own bound, or the host's giving up, can have ended it (O39)
                res.timed_out, res.bound = bound is not None, bound
            if incomplete is not None and not res.timed_out and res.kill is None:
                log.error("a cell's exec did not complete: %s", incomplete)
                raise ExecIncomplete(incomplete, res)
        finally:
            if not timer_ended:
                self._end(timer, cell)
            if not watch_ended:
                self.watch.end_cell()
            with self.meta_lock:
                if self._running is timer:
                    self._running = None
        return res

    def _cause(
        self, fired: bool, gave_up: bool, watched: dict[str, Any], before: dict[str, Any] | None, timeout_s: float
    ) -> dict[str, Any] | None:
        """The one cause of a cell's kill from the harness's own evidence (the module's docstring), None for none."""
        kills = watched["kills"]
        after = self.node.cgroup_memory() if before is not None else None
        if _oom_rose(before, after):  # infrastructure comes first (killcause.PRECEDENCE), whatever the watch did
            basis = "the step's memory cgroup, which the harness, the gate and the cell share, counted an OOM kill"
            return {"reason": "host_oom", "category": KC.CATEGORY["host_oom"], "basis": basis, "timer_fired": fired,
                    "watch_kills": kills}  # fmt: skip
        if kills:
            first = kills[0]
            reason = first["reason"]
            basis = (
                f"the harness's watch killed the run's processes: {first['what']} at {first['amount']}, over its "
                f"limit of {first['limit']}"
            )
            return {"reason": reason, "category": KC.CATEGORY[reason], "basis": basis, "watch_kills": kills,
                    "timer_fired": fired}  # fmt: skip
        if fired:
            basis = f"the harness's timer fired at the cell's limit of {timeout_s:g} s"
            return {"reason": "time_limit", "category": KC.CATEGORY["time_limit"], "basis": basis, "timer_fired": True}
        if gave_up:
            basis = "the apptainer call did not return within the cell's limit and its margin"
            return {"reason": "unknown", "category": KC.CATEGORY["unknown"], "basis": basis, "timer_fired": False}
        return None

    def _record_cell(
        self,
        res: ExecResult,
        cell: int,
        deadline: dict[str, Any] | None,
        watched: dict[str, Any],
        left: int | None,
        incomplete: str | None,
        gave_up: bool,
    ) -> None:
        entry: dict[str, Any] = {
            "cell": cell,
            "status": RAN,
            "exit_code": res.exit_code,
            "duration_s": res.duration_s,
            "exec_check": res.exec_check,
            "watched": {"seen": watched["seen"], "peak_bytes": watched["peak_bytes"]},
            "left_after_exec": left,
        }
        if res.kill:
            entry["kill"] = res.kill
            with self.meta_lock:
                self._meta.setdefault("cell_kills", []).append({"cell": cell, **res.kill})
        self._with_deadline(res, entry, deadline)  # a process that outlived its kill: `kill_survived` (O34)
        if res.kill:  # from what caused it, as on Docker: never from an exit code a kill shares
            res.timed_out = gave_up or res.kill["reason"] in ("time_limit", KC.KILL_SURVIVED)
        entry["timed_out"] = res.timed_out
        if res.output_cut:
            entry["output_cut"] = res.output_cut
            with self.meta_lock:
                self._meta.setdefault("cell_output_cuts", []).append({"cell": cell, **res.output_cut})
        if not watched["seen"]:
            with self.meta_lock:
                self._meta.setdefault("unwatched_cells", []).append(cell)
        if incomplete is not None and not res.timed_out and not res.kill:
            entry["incomplete"] = incomplete
            with self.meta_lock:
                self._meta.setdefault("exec_incomplete", []).append({"cell": cell, "cause": incomplete})
        self.cells.append(entry)


def _oom_rose(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    """Whether the cgroup's `oom_kill` counter rose between the two readings; False when either is unknown."""
    old = (before or {}).get("events") or {}
    new = (after or {}).get("events") or {}
    return bool(old) and bool(new) and new.get("oom_kill", 0) > old.get("oom_kill", 0)


__all__ = ["AppCellRunner"]
