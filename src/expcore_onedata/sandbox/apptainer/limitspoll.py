# Provenance: new
"""What the run's watch does at every poll for the limits of `limits.py` (O70): reads the node, kills past a limit, and
keeps the readings in the run's `LimitsCheck`.

The watch makes the node's reads outside its lock and gives this class its lock for the check's state, so the check
(which is not thread-safe) is only touched under it. A kill of a cell's processes is the watch's own (`kill`: it records
it against the running cell); the gate's is kept here, apart from the cells' (`gate_kills`), for the collection to read
as the gate's death and never as a cell's cause.
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.apptainer.limits import LimitsCheck, Probe
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.procs import Proc

log = logging.getLogger(__name__)


class LimitsPoll:
    """The limits' part of one run's watch (the module's docstring)."""

    def __init__(
        self,
        check: LimitsCheck,
        node: NodeOps,
        lock: threading.RLock,
        kill: Callable[..., int],
    ) -> None:
        self.check, self._node, self._lock, self._kill = check, node, lock, kill
        self.gate_kills: list[dict[str, Any]] = []

    def unreadable(self, cell_running: bool) -> None:
        """A poll that could not list `/proc` (the watch's lock is held)."""
        self.check.unreadable_poll(cell_running)

    def over_pids(self, members: Sequence[Proc]) -> bool:
        """The tasks of the run's processes against `pids`: over it, the running cell's processes are killed as
        `pids_limit`. Whether the poll killed (one that did goes no further: its processes are gone)."""
        with self._lock:
            tasks, limit = self.check.observe_tasks(members), self.check.pids
        if tasks is None or tasks <= limit:
            return False
        self._kill(list(members), KC.PIDS_LIMIT, "pids", tasks, limit)
        return True

    def check_affinity(self, members: Sequence[Proc]) -> None:
        """The cores of every task of every process of the run against the effective set: recorded, never a kill."""
        with self._lock:
            if self.check.reference() is None:  # no pin and no canary: nothing to judge a process against
                return
        for proc in members:
            found = self._node.task_affinity(proc.pid, proc.start)
            with self._lock:
                self.check.note_affinity(proc.pid, proc.start, found)

    def check_gate(self, found: Sequence[Proc]) -> None:
        """The gate helper's resident memory against `gate_memory_bytes`: over it, the gate is SIGKILLed through its
        pidfd, and the kill is recorded as the gate's own."""
        gate = self.check.gate
        if gate is None:
            return
        with self._lock:
            if self.check.gate_ended:
                return
        row = next((p for p in found if p.pid == gate.pid and p.start == gate.start), None)
        ended = None if row is not None and row.rss is not None else self._node.alive(gate.pid, gate.start)
        with self._lock:
            over = self.check.note_gate(row, ended)
        if over is None:
            return
        sent = self._node.signal(gate.pid, gate.start, signal.SIGKILL) is True
        with self._lock:
            self.gate_kills.append({
                "reason": "gate_memory", "what": "gate", "amount": over, "limit": self.check.gate_limit,
                "signalled": sent, "utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            })  # fmt: skip
        log.info("the run's watch killed the gate: its memory was over its limit")

    def gate_kill(self) -> dict[str, Any] | None:
        """The watch's kill of the gate that signalled it; None when there was none (the gate's death is then no doing
        of the watch's)."""
        with self._lock:
            return next((dict(k) for k in self.gate_kills if k["signalled"] is True), None)

    def note_probe(self, probe: Probe | None) -> None:
        with self._lock:
            self.check.note_probe(probe)

    def record(self, kills: Sequence[Mapping[str, Any]], memory: Mapping[str, Any]) -> dict[str, Any]:
        """The run's `infrastructure_facts.limits` (`LimitsCheck.record`) from the watch's kills and memory reading."""
        with self._lock:
            return self.check.record(kills=kills, gate_kills=self.gate_kills, memory=memory)


__all__ = ["LimitsPoll"]
