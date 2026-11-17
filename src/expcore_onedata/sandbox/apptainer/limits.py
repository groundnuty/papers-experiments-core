# Provenance: new
"""`cpus`, `pids`, `nofile` and the gate's memory limit on an HPC batch node, kept as close to Docker as an
unprivileged user can, and what each run's record says of them (O70).

A batch node gives no cgroup per container, so each limit is kept by other means, and the record says which held:

* **nofile**: every cell command is prefixed by `prlimit --nofile=N:N --`, so `apptainer` and the cell under it start
  with a hard and soft `RLIMIT_NOFILE` of `limits.nofile`, which the cell (no `CAP_SYS_RESOURCE`) cannot raise. The
  start's canary reads `resource.getrlimit` inside a cell, and a hard limit that is not the declared one fails the
  start.
* **cpus**: the prefix also holds `taskset -c <cores>`, the first `ceil(cpus)` cores of the driver's own affinity (the
  step's cores). The canary reads `os.sched_getaffinity(0)` inside a cell: that is the run's effective set. At every
  poll the watch reads the cores of every task of every process of the run (`NodeOps.task_affinity`) and records, never
  kills, a process whose cores are not within the effective set (`widened`). Docker's `--cpus` is a CFS quota, not a
  set.
* **pids**: at every poll the watch counts the tasks of the run's processes (`Threads:`; Docker's pids.max counts
  tasks) and over `pids` kills the running cell's processes as `pids_limit`, the agent's own (`scope: cell`). Docker
  refuses the fork with EAGAIN instead.
* **gate_memory_bytes**: at every poll the watch reads the gate's `RssAnon` and `RssShmem` (a helper process of the
  driver's own PID namespace, named by its pid and start time) and over the limit SIGKILLs it through its pidfd; the
  collection's `deaths()` then names the watch and a `component_defect`, the cause of a gate past its own memory limit
  on Docker.

A reading is `pinned`, `within` or `set` only when it was shown: a poll that could not list `/proc`, a process whose
tasks or cores could not be read, a process `/proc` hides (the run's memory reading holds `hidden_process`, or a cell
was killed for hidden memory), a canary that could not be read, and a step whose cores could not be read each leave it
`unknown` with that reason, never a value. A kill (`over`) or a widened process (`widened`) that was shown outweighs
them. Not thread-safe: the watch calls it under its lock, and makes the node's reads outside it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.apptainer import procs as P

if TYPE_CHECKING:
    from expcore_onedata.sandbox.apptainer.node import Spawned

VERSION = 1  # of the record `LimitsCheck.record` gives
PINNED, WIDENED = "pinned", "widened"
WITHIN, OVER, UNKNOWN, NOT_APPLICABLE = "within", "over", "unknown", "not_applicable"
SET = "set"
AFFINITY_UNREADABLE = "affinity_unreadable"  # a task of the run's whose cores could not be read
STEP_SET_UNREADABLE = "step_set_unreadable"  # the driver's own affinity could not be read: no pin was made
PROC_UNREADABLE = "proc_unreadable"  # a listing of /proc failed, or a process's status had no task count
HIDDEN_PROCESS = "hidden_process"  # /proc hides a process of the run (hidden.py), so its tasks and cores are unseen
PROBE_UNREADABLE = "probe_unreadable"  # the canary's own reading of its limits did not come back whole
GATE_UNREADABLE = "gate_unreadable"  # the gate's resident memory could not be read
KILL_REACHED_NONE = "kill_reached_none"  # the watch's kill signalled no process (a reason added to the fixed list)
WRAPPERS = ("prlimit", "taskset")  # the tools the prefix runs: a failure of theirs names them, never the connect

# The egress canary (`expcore.egress`), which also reads what the cell was given: its open-file limit and its cores.
CANARY = (
    "import os, resource, socket, time\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', {port}), 10).close()\n"
    "    print('canary open')\n"
    "except OSError as e:\n"
    "    print('canary ' + type(e).__name__)\n"
    "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
    "print('limits', soft, hard, ','.join(str(c) for c in sorted(os.sched_getaffinity(0))))\n"
    "time.sleep({hold:.3f})\n"
)
LIMITS_LINE = "limits"


@dataclass(frozen=True)
class Probe:
    """What the canary read inside a cell: its soft and hard open-file limit, and its cores (None when not a list)."""

    nofile_soft: int
    nofile_hard: int
    cpus: frozenset[int] | None


def pin_for(step_set: Sequence[int] | None, cpus: float) -> list[int] | None:
    """The cores a cell is pinned to: the first `ceil(cpus)` of the step's, sorted, all of them when it has fewer;
    None when the step's set is unknown or empty."""
    if not step_set:
        return None
    return sorted(step_set)[: max(math.ceil(cpus), 1)]


def prefix(nofile: int, pin: Sequence[int] | None) -> list[str]:
    """The words before `apptainer` in a cell's command: the file limit, then the pin when there is one."""
    words = ["prlimit", f"--nofile={nofile}:{nofile}", "--"]
    return [*words, "taskset", "-c", ",".join(str(core) for core in pin)] if pin else words


def parse_probe(text: str) -> Probe | None:
    """The canary's `limits <soft> <hard> <cores>` line from its output; None when it is missing or its limits are not
    two whole numbers. Cores that are not a list leave `cpus` None."""
    for line in text.splitlines():
        parts = line.split()
        if parts[:1] == [LIMITS_LINE]:
            if len(parts) != 4 or not (parts[1].isdigit() and parts[2].isdigit()):
                return None
            return Probe(int(parts[1]), int(parts[2]), P.parse_cpu_list(parts[3]))
    return None


def wrapper_failed(returncode: int, stderr: str) -> str | None:
    """The tool of the prefix that could not run or refused (`prlimit: failed to set the NOFILE resource limit`, or one
    missing from the node's PATH), named; None when neither says so."""
    for line in stderr.splitlines():
        for tool in WRAPPERS:
            if line.startswith((f"{tool}:", f"{tool} could not be run")):
                return tool
    return None


class LimitsCheck:
    """The limits of one run: what was declared, what the canary showed, and what the watch saw (the module's
    docstring). `begin` gives it the step's cores and the gate once the run has them; until then every reading is
    unknown."""

    def __init__(self, *, cpus: float, pids: int, nofile: int, gate_memory_bytes: int, has_gate: bool = True) -> None:
        self.cpus, self.pids, self.nofile, self.gate_limit = cpus, pids, nofile, gate_memory_bytes
        self.has_gate = has_gate  # a REST data path: there is a gate to bound
        self.step_set: list[int] | None = None
        self.pin: list[int] | None = None
        self.gate: Spawned | None = None
        self.probe: Probe | None = None
        self.max_tasks: int | None = None
        self._widened: set[tuple[int, str]] = set()
        self._reasons: dict[str, set[str]] = {"cpus": set(), "pids": set(), "gate_memory": set()}
        self.gate_peak: int | None = None
        self.gate_ended = False

    def begin(self, step_set: Sequence[int] | None, gate: Spawned | None) -> None:
        """The step's own cores (None: they could not be read) and the gate helper, which a run with a gate has."""
        self.step_set = sorted(step_set) if step_set else None
        self.pin = pin_for(self.step_set, self.cpus)
        self.gate = gate

    def command_prefix(self) -> list[str]:
        return prefix(self.nofile, self.pin)

    def note_probe(self, probe: Probe | None) -> None:
        """The canary's reading of the limits inside a cell (None: it did not come back whole)."""
        self.probe = probe

    @property
    def effective_set(self) -> frozenset[int] | None:
        return None if self.probe is None else self.probe.cpus

    def reference(self) -> frozenset[int] | None:
        """The cores a process of the run may be on: the canary's effective set once it is known, the pin until then;
        None when neither is."""
        return self.effective_set or (None if self.pin is None else frozenset(self.pin))

    # ------------------------------------------------------------------------------------------------ polls
    def unreadable_poll(self, cell_running: bool) -> None:
        """A poll that could not list `/proc`: the gate's memory was not read, nor the running cell's tasks or cores."""
        if not self.gate_ended:
            self._reasons["gate_memory"].add(PROC_UNREADABLE)
        if cell_running:
            self._reasons["pids"].add(PROC_UNREADABLE)
            self._reasons["cpus"].add(PROC_UNREADABLE)

    def observe_tasks(self, members: Sequence[P.Proc]) -> int | None:
        """The tasks of the run's processes now, the largest kept; a process whose task count could not be read leaves
        the pids reading unknown, and the sum is of the others (a lower bound, enough for a kill). None without a
        process."""
        if not members:
            return None
        known = [p.threads for p in members if p.threads is not None]
        if len(known) < len(members):
            self._reasons["pids"].add(PROC_UNREADABLE)
        total = sum(known)
        if known:  # a poll that read no task count shows no maximum
            self.max_tasks = max(self.max_tasks or 0, total)
        return total

    def note_affinity(self, pid: int, start: str, found: frozenset[int] | None) -> None:
        """What the node read of the cores of every task of a process of the run: None when it could not (unknown),
        the empty set when the process is gone (nothing), otherwise recorded as wider than the reference or not."""
        if found is None:
            self._reasons["cpus"].add(AFFINITY_UNREADABLE)
            return
        reference = self.reference()
        if found and reference is not None and not found <= reference:
            self._widened.add((pid, start))

    def note_gate(self, found: P.Proc | None, ended: bool | None) -> int | None:
        """The gate's resident bytes at one poll: `found` is its row of a listing, `ended` what the node says of it when
        no row shows its memory (False: it is gone, which stops the watching). The bytes when over the limit."""
        if self.gate is None or self.gate_ended:
            return None
        if ended is False:  # gone, whatever the row of a zombie not yet reaped shows (it has no memory to read)
            self.gate_ended = True
            return None
        if found is None or found.rss is None:
            self._reasons["gate_memory"].add(GATE_UNREADABLE)
            return None
        self.gate_peak = max(self.gate_peak or 0, found.rss)
        return found.rss if found.rss > self.gate_limit else None

    # ------------------------------------------------------------------------------------------------ the record
    @property
    def widened(self) -> bool:
        return bool(self._widened) or self._probe_wider

    @property
    def _probe_wider(self) -> bool:
        """The canary's cores are not within the pin: the starter or the node gave the cell more than was asked."""
        return self.pin is not None and self.effective_set is not None and not self.effective_set <= set(self.pin)

    def record(self, *, kills: Sequence[Mapping[str, Any]], gate_kills: Sequence[Mapping[str, Any]],
               memory: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
        """The run's `infrastructure_facts.limits` (every key, always): `kills` the watch's, `gate_kills` its kills of
        the gate and `memory` its memory reading, whose `hidden_process` makes the tasks and cores unseen."""
        hidden = HIDDEN_PROCESS if memory.get("hidden_processes") or memory.get("hidden_memory_cells") else None
        return {
            "version": VERSION,
            "cpus": self._cpus(hidden),
            "pids": self._pids(kills, hidden),
            "nofile": self._nofile(),
            "gate_memory": self._gate(gate_kills),
        }

    def _cpus(self, hidden: str | None) -> dict[str, Any]:
        reasons = set(self._reasons["cpus"]) | ({hidden} if hidden else set())
        if self.step_set is None:
            reasons.add(STEP_SET_UNREADABLE)
        if self.effective_set is None:
            reasons.add(PROBE_UNREADABLE)
        effective = self.effective_set
        return {
            "declared": float(self.cpus),
            "reading": WIDENED if self.widened else UNKNOWN if reasons else PINNED,
            "unknown": sorted(reasons),
            "effective_set": None if effective is None else sorted(effective),
            "effective_count": None if effective is None else len(effective),
            "step_set": None if self.step_set is None else list(self.step_set),
            "widened": self.widened,
            "widened_processes": len(self._widened),
        }

    def _pids(self, kills: Sequence[Mapping[str, Any]], hidden: str | None) -> dict[str, Any]:
        mine = [k for k in kills if k["reason"] == KC.PIDS_LIMIT]
        reached = [k for k in mine if k["signalled"] > 0]
        reasons = set(self._reasons["pids"]) | ({hidden} if hidden else set())
        if self.max_tasks is None:
            reasons.add(PROC_UNREADABLE)
        if mine and not reached:
            reasons.add(KILL_REACHED_NONE)
        return {
            "declared": int(self.pids),
            "reading": OVER if reached else UNKNOWN if reasons else WITHIN,
            "unknown": sorted(reasons),
            "max_seen": self.max_tasks,
            "kills": len(reached),
        }

    def _nofile(self) -> dict[str, Any]:
        hard = None if self.probe is None else self.probe.nofile_hard
        shown = hard == self.nofile  # a start whose canary read another hard limit failed: never a record of `set`
        return {
            "declared": int(self.nofile),
            "reading": SET if shown else UNKNOWN,
            "unknown": [] if shown else [PROBE_UNREADABLE],
            "hard_limit": hard,
        }

    def _gate(self, gate_kills: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        declared = int(self.gate_limit)
        if not self.has_gate:
            return {"declared": declared, "reading": NOT_APPLICABLE, "unknown": [], "peak_bytes": None}
        reached = [k for k in gate_kills if k["signalled"] is True]
        reasons = set(self._reasons["gate_memory"])
        if self.gate_peak is None:
            reasons.add(GATE_UNREADABLE)
        if gate_kills and not reached:
            reasons.add(KILL_REACHED_NONE)
        return {
            "declared": declared,
            "reading": OVER if reached else UNKNOWN if reasons else WITHIN,
            "unknown": sorted(reasons),
            "peak_bytes": self.gate_peak,
        }


__all__ = [
    "CANARY",
    "LimitsCheck",
    "Probe",
    "parse_probe",
    "pin_for",
    "prefix",
    "wrapper_failed",
]
