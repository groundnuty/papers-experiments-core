# Provenance: new
"""What `/proc` hides from the run's watch, seen from outside it: the cgroup's memory, and a child no entry shows (O56).

A process of the driver's uid that makes itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`) has no `/proc` entry under
`hidepid=invisible`, so the watch's sum over the run's processes neither counts nor kills it. Its memory is still
charged to the cgroup of the process that started its cell, the driver's own (`NodeOps.own_cgroup`), whose
`memory.stat` counts every process in it and below. So at every poll the check reads that cgroup's `anon` before and
after the watch's listing of `/proc`, and takes the smaller of the two less the `RssAnon` of every process of the uid
that the listing shows in the cgroup: the driver, the gate, the collector, the mount's watcher and oneclient,
Apptainer's own `squashfuse_ll` and the cells' visible processes. What is left is anonymous memory that nothing visible
explains.

Some of it is no agent's: Apptainer's setuid starter and the cell's `appinit` are hidden by the same rule, and the
kernel's counters are kept in per-CPU batches. So a cell is judged by how much the unexplained memory grew, from the
last reading taken with no cell's namespace in the cgroup (never below nothing) to each poll during the cell, against
`margin_bytes` for the running cell and for each other run's cell in the cgroup (a namespace of the run's own never
adds one). The start's own cells (the egress canary and the read-only probe, the
harness's code) calibrate it: one that grew past the margin means this node's own share is too large to tell an agent's
from, and the run's memory reading is then unknown (`margin_exceeded_at_start`), never a kill.

A poll whose growth passes the margin lists `/proc` again after the cgroup's second reading and takes the larger of
the two listings' totals, so a process that freed memory just as it was read and took it again before the cgroup was
must do so around both readings to look hidden; each total is one listing's own, so two processes that hold memory in
turn are never counted twice. A growth that passes so for `HIDDEN_CONFIRM`
polls in a row is the running cell's when no other cell's namespace is in the cgroup. Memory the watch
cannot see is taken as past the limit: the watch kills the run's processes as `memory_limit`, of `memory_hidden`
(`watch.Watch`), once per cell. With another run's cell in the cgroup it cannot be told whose it is: the cell cannot
be isolated, so the watch ends the run (`scope: run`, `attributed: false`, O72), and the reading is unknown
(`unattributed`), never `over`, since the memory may be the other run's; the run is not counted as hiding.

The second signal, `hidden_process`: a visible process of the run's cell whose `children` files list a pid that `/proc`
does not show as the uid's while a process holds it (`NodeOps.hidden_children`). It is recorded, never a kill, and it
makes the reading unknown. A process that forks twice under the cell's hidden `appinit` is a child of no visible
process, so this signal misses it: it supplements the cgroup's check and never replaces it.

What neither sees: a hidden process's shared memory (a memfd, a shared anonymous mapping), which the cgroup counts as
`shmem` with the run's tmpfs files and an unlinked file a cell holds open, so it is not compared; hidden memory under
the margin; and a spike shorter than two polls. The step's own limit bounds them, and the end of the cell's PID
namespace when its exec returns ends every hidden process (`facts.py`).

The run's reading is `over` when a kill of the watch's for memory reached a process, `unknown` with every reason that
held (a kill that reached none among them), or `within`. A cell killed for hidden memory counts as one whatever its
kill reached.
Not thread-safe: the watch calls it under its lock, and makes the node's reads outside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from expcore_onedata.sandbox.apptainer import procs as P

if TYPE_CHECKING:  # the settings import this module's default, and the node's module imports far more
    from expcore_onedata.sandbox.apptainer.node import NodeOps

HIDDEN_CONFIRM = 2  # polls in a row a growth past the margin must last before it counts
DEFAULT_MARGIN_BYTES = 128 << 20  # the live probe's whole overhead was some 43 MiB of the step's memory.current
VERSION = 1  # of the reading `visibility` gives (`expcore.visibility`)
WITHIN, OVER, UNKNOWN = "within", "over", "unknown"
CGROUP_UNREADABLE = "cgroup_unreadable"  # no v2 cgroup, or its memory.stat could not be read during a cell
PROC_UNREADABLE = "proc_unreadable"  # a listing of /proc during a cell failed
HIDDEN_PROCESS = "hidden_process"
CHILDREN_UNREADABLE = "children_unreadable"
UNATTRIBUTED = "unattributed"
MARGIN_AT_START = "margin_exceeded_at_start"
KILL_REACHED_NONE = "kill_reached_none"  # the watch's kill for hidden memory signalled no process


@dataclass(frozen=True)
class Hidden:
    """A growth past the margin that is the running cell's: what the watch kills it for."""

    grown_bytes: int
    allowance_bytes: int
    cgroup_anon_bytes: int
    visible_anon_bytes: int
    attributed: bool = True  # False: another run's cell is in the cgroup, so it may be that run's (it ends the run)


class HiddenCheck:
    """The check of one run's watch (the module's docstring)."""

    def __init__(self, node: NodeOps, *, own_pidns: str | None, margin_bytes: int) -> None:
        if margin_bytes < 1:
            raise ValueError("the margin is a positive number of bytes")
        self._node, self._own, self.margin = node, own_pidns, margin_bytes
        self.cgroup = node.own_cgroup()
        self._reasons: set[str] = set() if self.cgroup is not None else {CGROUP_UNREADABLE}
        self._idle: int | None = None  # the unexplained bytes at the last poll with no cell running
        self._peak: dict[str, int] | None = None  # the largest growth in a cell, with the readings it came from
        self._start_peak: int | None = None  # the largest growth in a calibrating cell
        self._hidden_pids: set[int] = set()
        self._hidden_memory_cells: set[int] = set()
        self.children_unreadable_polls = self.cgroup_unreadable_polls = 0
        self._uncalibrated = False
        self._cell: int | None = None
        self._cell_state(None, False)

    def _cell_state(self, cell: int | None, calibration: bool) -> None:
        self._cell, self._calibrating = cell, calibration
        self._baseline = max(self._idle or 0, 0)
        self._streak, self._cell_peak, self._passed, self._acted = 0, None, False, False
        self._cell_reasons: set[str] = set()
        self._cell_hidden: set[int] = set()

    # ------------------------------------------------------------------------------------------------ cells
    def begin_cell(self, cell: int, calibration: bool) -> None:
        self._cell_state(cell, calibration)

    def end_cell(self, killed_for_memory: bool) -> dict[str, Any]:
        """The cell's reading: `reading`, `unknown`, `hidden_processes` and `unexplained_peak_bytes`."""
        if self._calibrating:
            self._start_peak = max(self._start_peak or 0, self._cell_peak or 0)
            if self._passed:
                self._uncalibrated = True
                self._cell_reasons.add(MARGIN_AT_START)
        self._reasons |= self._cell_reasons
        found = {
            "reading": OVER if killed_for_memory else UNKNOWN if self._cell_reasons else WITHIN,
            "unknown": sorted(self._cell_reasons),
            "hidden_processes": len(self._cell_hidden),
            "unexplained_peak_bytes": self._cell_peak,
        }
        self._cell_state(None, False)
        return found

    # ------------------------------------------------------------------------------------------------ readings
    def anon(self) -> int | None:
        """The cgroup's anonymous bytes now; None without a cgroup or when they cannot be read."""
        return None if self.cgroup is None else self._node.cgroup_anon(self.cgroup)

    def unreadable_poll(self) -> None:
        """A poll that could not list `/proc`: nothing is known of the cell's memory at it."""
        self._streak = 0
        if self._cell is not None:
            self._cell_reasons.add(PROC_UNREADABLE)

    def _visible(self, listings: Sequence[Sequence[P.Proc]]) -> tuple[int, set[str]]:
        """The RssAnon the visible processes of the uid in the cgroup hold, at the larger of the listings' totals, and
        the PID namespaces of the cells among them. Each total is one listing's: a sum of each process's largest
        reading across listings would count memory two processes held in turn twice, and hide as much."""
        totals: list[int] = []
        cells: set[str] = set()
        for found in listings:
            inside = [proc for proc in found if P.in_cgroup(proc.cgroup, str(self.cgroup))]
            totals.append(sum(proc.anon or 0 for proc in inside))
            cells |= {proc.pidns for proc in inside if proc.pidns is not None and proc.pidns != self._own}
        return max(totals, default=0), cells

    def suspect(self, found: Sequence[P.Proc], before: int | None, after: int | None, run_namespaces: set[str]) -> bool:
        """Whether the poll's one listing leaves the running cell's growth past its allowance. The watch then lists
        `/proc` again, after the cgroup's second reading, for `assess`: a visible process that freed memory just as it
        was read and took it again before the cgroup was must have done so twice, around both readings, to count."""
        if self._cell is None or self.cgroup is None or before is None or after is None:
            return False
        visible, cells = self._visible([found])
        grown = min(before, after) - visible - self._baseline
        return grown > self.margin * (1 + len(cells - run_namespaces))

    def assess(
        self,
        listings: Sequence[Sequence[P.Proc]],
        before: int | None,
        after: int | None,
        run_namespaces: set[str],
    ) -> Hidden | None:
        """One poll's cgroup readings against what the listings show (`listings`: the poll's own, and the one taken
        after the cgroup's second reading when the first was `suspect`): the growth to kill the running cell for, or
        None."""
        if self.cgroup is None or before is None or after is None:
            self._streak = 0
            if self._cell is not None:
                self.cgroup_unreadable_polls += 1
                self._cell_reasons.add(CGROUP_UNREADABLE)
            return None
        charged, (visible, cells) = min(before, after), self._visible(listings)
        unexplained = charged - visible
        if self._cell is None:
            if not cells:  # another run's cell, hidden memory and all, is never this run's baseline
                self._idle = unexplained
            return None
        grown = unexplained - self._baseline
        foreign = cells - run_namespaces
        allowance = self.margin * (1 + len(foreign))  # the running cell's share, and each other run's cell's
        if self._cell_peak is None or grown > self._cell_peak:
            self._cell_peak = grown
            if self._peak is None or grown > self._peak["unexplained_bytes"]:
                self._peak = {"unexplained_bytes": grown, "cgroup_anon_bytes": charged, "visible_anon_bytes": visible,
                              "baseline_bytes": self._baseline, "allowance_bytes": allowance}  # fmt: skip
        if grown <= allowance:
            self._streak = 0
            return None
        self._streak += 1
        if self._streak < HIDDEN_CONFIRM:
            return None
        self._passed = True
        if self._calibrating or self._acted:
            return None
        if foreign:
            self._cell_reasons.add(UNATTRIBUTED)
            return Hidden(grown, allowance, charged, visible, attributed=False)
        if self._uncalibrated:
            self._cell_reasons.add(MARGIN_AT_START)
            return None
        return Hidden(grown, allowance, charged, visible)

    def killed(self, signalled: int) -> None:
        """The watch's kill for a growth `assess` gave: the cell held hidden memory whatever the kill reached, and a
        kill that reached no process leaves its memory unknown (it may be tried again at the next poll)."""
        if self._cell is None:
            return
        self._hidden_memory_cells.add(self._cell)
        if signalled > 0:
            self._acted = True
        else:
            self._cell_reasons.add(KILL_REACHED_NONE)

    def ended(self) -> None:
        """The watch ended the run for a growth it could not tell the cell's: nothing more is done for this cell, and
        the run is not counted as hiding (the memory may be another run's)."""
        self._acted = True

    def unreached(self) -> None:
        """A kill of the watch's for visible memory that signalled no process: the cell's memory is unknown."""
        if self._cell is not None:
            self._cell_reasons.add(KILL_REACHED_NONE)

    def note_children(self, hidden: set[int], unreadable: bool) -> None:
        """What the running cell's visible processes' children showed at one poll."""
        if self._cell is None:
            return
        if unreadable:
            self.children_unreadable_polls += 1
            self._cell_reasons.add(CHILDREN_UNREADABLE)
        if hidden:
            self._cell_hidden |= hidden
            self._hidden_pids |= hidden
            self._cell_reasons.add(HIDDEN_PROCESS)

    @property
    def running(self) -> bool:
        return self._cell is not None

    # ------------------------------------------------------------------------------------------------ the record
    def reading(self, killed_for_memory: bool) -> dict[str, Any]:
        """The run's memory reading with its figures, for its infrastructure facts: `reading` (`within`, `over` or
        `unknown`), `unknown` (every reason), the margin, the calibrating cells' largest growth, the largest growth
        with the readings it came from, and the counts behind the reasons. Never a path or a pid."""
        reasons = sorted(self._reasons | self._cell_reasons)
        return {
            "reading": OVER if killed_for_memory else UNKNOWN if reasons else WITHIN,
            "unknown": reasons,
            "margin_bytes": self.margin,
            "start_unexplained_bytes": self._start_peak,
            "peak": None if self._peak is None else dict(self._peak),
            "hidden_processes": len(self._hidden_pids),
            "hidden_memory_cells": len(self._hidden_memory_cells),
            "children_unreadable_polls": self.children_unreadable_polls,
            "cgroup_unreadable_polls": self.cgroup_unreadable_polls,
        }

    def visibility(self, killed_for_memory: bool) -> dict[str, Any]:
        """The reading as the core's `process_visibility` instrument takes it (`expcore.visibility`)."""
        found, peak = self.reading(killed_for_memory), self._peak or {}
        return {
            "version": VERSION,
            "memory": found["reading"],
            "unknown": found["unknown"],
            "hidden_process": found["hidden_processes"],
            "hidden_memory": found["hidden_memory_cells"],
            "figures": {
                "margin_bytes": self.margin,
                "start_unexplained_bytes": self._start_peak,
                "unexplained_peak_bytes": peak.get("unexplained_bytes"),
                "cgroup_anon_bytes": peak.get("cgroup_anon_bytes"),
                "visible_anon_bytes": peak.get("visible_anon_bytes"),
            },
        }


__all__ = ["DEFAULT_MARGIN_BYTES", "HIDDEN_CONFIRM", "Hidden", "HiddenCheck"]
