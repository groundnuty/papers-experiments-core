# Provenance: new
"""The run's memory and disk watch: the bounds a container's cgroup and disks give Docker, kept by the harness (O56).

A batch node gives an unprivileged user no cgroup per container, so the agent's memory limit is kept here. Every
`memory_poll_s` the watch lists this uid's processes (`NodeOps.processes`), finds the run's PID namespaces (each cell's
`apptainer exec --containall` has its own: a process that carries the run's tag in its environment, or the running
cell's completion nonce in its command line, names its namespace, and from then on every process in that namespace is
the cell's, whatever it calls itself or clears), and sums their resident anonymous and shared-memory bytes. Over
`memory_bytes`, every process of the run is SIGKILLed through its pidfd: a `memory_limit` kill, the agent's own outcome,
as a container's OOM kill is on Docker (where the kernel kills the largest process alone, and the cell may live on).

Every `disk_poll_s` (stretched to ten times a walk's own duration) it walks the run's `/work`, `/tmp` and `/dev/shm`,
which are directories on the node's tmpfs, charged to the step's memory with the dispatcher's and the gate's: each over
its size (`work_disk_bytes`, `tmp_bytes`, `shm_bytes`), holding more entries than a disk of that size has inodes
(one per 16 KiB, as `mkfs.ext4` makes them), or holding a directory the walk may not read (whose size is unknown),
kills the run's processes as `disk_limit`, also the agent's own; on
Docker a write past the size is refused with ENOSPC instead.

A process `/proc` hides (a non-dumpable one, under `hidepid`) is checked from outside at every poll (`hidden.py`): the
anonymous memory the driver's cgroup charges against what its visible processes hold, and a child of a cell process
that `/proc` does not show. Memory nothing visible explains that grew past the margin during a cell is taken as past
the limit and kills the run's processes as `memory_limit` (`memory_hidden`); what cannot be told the cell's, a hidden
child and a reading that failed make the run's memory reading unknown, never within (`summary()["memory"]`).

What it cannot see is recorded, never taken for nothing (`facts.py`): a spike shorter than a period, a file the agent
writes elsewhere, a hidden process's shared memory. A listing of `/proc` that fails is counted (`unreadable_polls`),
and a cell whose namespace was never found is `seen: false` in its record.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.apptainer.hidden import DEFAULT_MARGIN_BYTES, Hidden, HiddenCheck
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.procs import Proc

log = logging.getLogger(__name__)

BYTES_PER_INODE = 16384  # mkfs.ext4's default ratio: a disk of N bytes has N // 16384 inodes
WALK_STRETCH = 10.0  # the disk walk waits at least this many times its own last duration


@dataclass(frozen=True)
class Space:
    """A directory of the run's that a cell writes, with the size it may hold."""

    name: str  # work, tmp or shm
    path: Path
    cap_bytes: int

    @property
    def cap_entries(self) -> int:
        return max(self.cap_bytes // BYTES_PER_INODE, 1)


def usage(path: Path, max_entries: int) -> tuple[int, int, bool]:
    """The bytes allocated under `path`, its entries, and whether the walk saw all of it; never following a link. The
    walk stops past `max_entries` entries, so a tree of millions of files costs no more than the cap. A file gone while
    it is walked is skipped; one the walk may not read or list (a directory its owner made 0000, say) makes the walk
    incomplete, since what is under it is unknown, never nothing."""
    total, entries, stack, whole = 0, 0, [path], True
    while stack and entries <= max_entries:
        try:
            with os.scandir(stack.pop()) as listing:
                for entry in listing:
                    entries += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        whole = False
                        continue
                    total += info.st_blocks * 512
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    if entries > max_entries:
                        break
        except FileNotFoundError:
            continue
        except OSError:
            whole = False
    return total, entries, whole


class Watch:
    """The memory and disk watch of one run (the module's docstring). `poll` takes one sample (a test calls it); `start`
    runs it in a thread until `stop`."""

    def __init__(
        self,
        node: NodeOps,
        *,
        run_tag: str,
        own_pidns: str | None,
        memory_bytes: int,
        spaces: tuple[Space, ...],
        memory_poll_s: float,
        disk_poll_s: float,
        hidden_margin_bytes: int = DEFAULT_MARGIN_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._node, self._tag, self._own = node, run_tag, own_pidns
        self._limit, self._spaces = memory_bytes, spaces
        self._period, self._disk_period, self._clock = memory_poll_s, disk_poll_s, clock
        self._hidden = HiddenCheck(node, own_pidns=own_pidns, margin_bytes=hidden_margin_bytes)
        self._lock = threading.RLock()
        self._ns: dict[str, int | None] = {}  # the run's PID namespaces, each with the cell it was found in
        self._cell: int | None = None
        self._nonce: str | None = None
        self._seen = False
        self._cell_peak = 0
        self._cell_kills: list[dict[str, Any]] = []
        self.kills: list[dict[str, Any]] = []
        self.peak_bytes = 0
        self.polls = self.unreadable_polls = 0
        self.disk: dict[str, dict[str, int]] = {}
        self._next_walk = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------------------------------------ cells
    def begin_cell(self, cell: int, nonce: str, *, calibration: bool = False) -> None:
        """A cell is about to start: its completion nonce names its first processes until its namespace is found. A
        `calibration` cell runs the harness's own code (the start's): what `/proc` hides of it is the node's own share,
        never killed for (`hidden.py`)."""
        with self._lock:
            self._cell, self._nonce, self._seen, self._cell_peak, self._cell_kills = cell, nonce, False, 0, []
            self._hidden.begin_cell(cell, calibration)

    def end_cell(self) -> dict[str, Any]:
        """The cell has ended: whether its namespace was found, its peak, the watch's kills during it, and its memory
        reading (`hidden.HiddenCheck.end_cell`)."""
        with self._lock:
            killed = any(k["reason"] == "memory_limit" for k in self._cell_kills)
            found = {"seen": self._seen, "peak_bytes": self._cell_peak, "kills": list(self._cell_kills),
                     "memory": self._hidden.end_cell(killed)}  # fmt: skip
            self._cell = self._nonce = None
            return found

    def forget(self, namespaces: set[str]) -> None:
        """Namespaces whose processes are all shown gone: their inode may be the kernel's to give again."""
        with self._lock:
            for name in namespaces:
                self._ns.pop(name, None)

    def namespaces(self) -> set[str]:
        with self._lock:
            return set(self._ns)

    # ------------------------------------------------------------------------------------------------ sampling
    def members(self) -> list[Proc] | None:
        """The run's processes now: every process in a namespace of the run's, a namespace found from any process
        that carries the run's tag or the running cell's nonce. None when `/proc` could not be listed."""
        return self._listing()[1]

    def _listing(self) -> tuple[list[Proc] | None, list[Proc] | None]:
        """Every process of the uid `/proc` shows, and the run's among them (`members`); both None when it could not
        be listed."""
        with self._lock:
            tags = (self._tag, *([self._nonce] if self._nonce else []))
            cell = self._cell
        found = self._node.processes(tags)
        if found is None:
            return None, None
        with self._lock:
            for proc in found:
                if proc.tags and proc.pidns is not None and proc.pidns != self._own and proc.pidns not in self._ns:
                    self._ns[proc.pidns] = cell
            members = [p for p in found if p.pidns is not None and p.pidns in self._ns]
            if cell is not None and any(self._ns.get(p.pidns) == cell for p in members):
                self._seen = True
            return found, members

    def poll(self) -> None:
        """One sample: the run's memory against its limit, what `/proc` hides of it (`hidden.py`), and every so often
        its directories against their sizes."""
        before = self._hidden.anon()
        found, members = self._listing()
        after = self._hidden.anon()
        with self._lock:
            self.polls += 1
            if found is None or members is None:
                self.unreadable_polls += 1
                self._hidden.unreadable_poll()
                return
            total = sum(p.rss or 0 for p in members)
            self.peak_bytes = max(self.peak_bytes, total)
            if self._cell is not None:
                self._cell_peak = max(self._cell_peak, total)
        if members and total > self._limit:
            self._kill(members, "memory_limit", "memory", total, self._limit)
            return
        self._check_hidden(found, members, before, after)
        if self._clock() >= self._next_walk:
            self._measure_spaces(members)

    def _check_hidden(self, found: list[Proc], members: list[Proc], before: int | None, after: int | None) -> None:
        """The cgroup's memory against the visible processes', and the cell's children against `/proc`: a growth past
        the margin that is the running cell's kills it (`memory_hidden`); the rest goes into the memory reading."""
        with self._lock:
            hidden = self._hidden.assess(found, before, after, set(self._ns))
            running = self._hidden.running
        if hidden is not None:
            sent = self._kill(members, "memory_limit", "memory_hidden", hidden.grown_bytes, hidden.allowance_bytes,
                              figures=_figures(hidden)) if members else 0  # fmt: skip
            with self._lock:
                self._hidden.killed(sent)
        if not running:
            return
        seen, unreadable = set(), False
        for proc in members:
            children = self._node.hidden_children(proc.pid, proc.start)
            if children is None:
                unreadable = True
            else:
                seen.update(children)
        with self._lock:
            self._hidden.note_children(seen, unreadable)

    def _measure_spaces(self, members: list[Proc]) -> None:
        began = self._clock()
        over: tuple[str, int, int] | None = None
        for space in self._spaces:
            used, entries, whole = usage(space.path, space.cap_entries)
            with self._lock:
                self.disk[space.name] = {"bytes": used, "entries": entries, "whole": whole}
            if over is None and not whole:  # what it could not see is unknown: taken as past its size
                over = (f"{space.name}_unwalkable", used, space.cap_bytes)
            elif over is None and used > space.cap_bytes:
                over = (space.name, used, space.cap_bytes)
            elif over is None and entries > space.cap_entries:
                over = (f"{space.name}_entries", entries, space.cap_entries)
        self._next_walk = self._clock() + max(self._disk_period, WALK_STRETCH * (self._clock() - began))
        if over is not None and members:
            self._kill(members, "disk_limit", *over)

    def _kill(
        self, members: list[Proc], reason: str, what: str, amount: int, limit: int, *,
        figures: Mapping[str, int] | None = None,
    ) -> int:  # fmt: skip
        """SIGKILL every process of the run through its pidfd, and record it against the running cell; how many were
        signalled."""
        sent = sum(1 for p in members if self._node.signal(p.pid, p.start, signal.SIGKILL) is True)
        with self._lock:
            event = {
                "cell": self._cell,
                "reason": reason,
                "what": what,
                "amount": amount,
                "limit": limit,
                "processes": len(members),
                "signalled": sent,
                "utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
                **({"figures": dict(figures)} if figures else {}),
            }
            self.kills.append(event)
            if self._cell is not None:
                self._cell_kills.append(event)
        log.info("the run's watch killed its processes: %s of %s over its limit", reason, what)
        return sent

    # ------------------------------------------------------------------------------------------------ the thread
    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll()
                except Exception as error:  # a sample that failed is unknown, counted, never the end of the watch
                    with self._lock:
                        self.unreadable_polls += 1
                        self._hidden.unreadable_poll()
                    log.error("the run's watch failed a sample: %s", type(error).__name__)
                self._stop.wait(self._period)

        self._thread = threading.Thread(target=loop, daemon=True, name="apptainer-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def summary(self) -> dict[str, Any]:
        """The watch's record for the run's metadata: its limits, its kills, its peak and what it could not read."""
        with self._lock:
            return {
                "memory_bytes": self._limit,
                "memory_poll_s": self._period,
                "disk_poll_s": self._disk_period,
                "spaces": {s.name: {"cap_bytes": s.cap_bytes, "cap_entries": s.cap_entries} for s in self._spaces},
                "peak_bytes": self.peak_bytes,
                "kills": list(self.kills),
                "polls": self.polls,
                "unreadable_polls": self.unreadable_polls,
                "disk": {k: dict(v) for k, v in self.disk.items()},
                "memory": self.reading(),
            }

    def _killed_for_memory(self) -> bool:
        return any(k["reason"] == "memory_limit" for k in self.kills)

    def reading(self) -> dict[str, Any]:
        """The run's memory reading with its figures (`hidden.HiddenCheck.reading`): `over` once the watch killed it
        for memory, `unknown` with its reasons, never `within` without the check having held."""
        with self._lock:
            return self._hidden.reading(self._killed_for_memory())

    def visibility(self) -> dict[str, Any]:
        """The reading the core's `process_visibility` instrument takes (`expcore.visibility`)."""
        with self._lock:
            return self._hidden.visibility(self._killed_for_memory())


def _figures(hidden: Hidden) -> dict[str, int]:
    return {"cgroup_anon_bytes": hidden.cgroup_anon_bytes, "visible_anon_bytes": hidden.visible_anon_bytes}


def spaces_of(run_dir: Path, limits: Mapping[str, int]) -> tuple[Space, ...]:
    """The run's three writable directories with their sizes: `work`, `tmp` and `shm` under the run's directory."""
    return tuple(Space(name, run_dir / name, limits[name]) for name in ("work", "tmp", "shm"))


__all__ = ["BYTES_PER_INODE", "Space", "Watch", "spaces_of", "usage"]
