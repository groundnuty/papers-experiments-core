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

At every poll it also keeps what a cgroup would (`limits.py`, `limitspoll.py`, O70): the tasks of the run's processes
(`Threads:`) against `pids`, over which it kills the running cell's processes as `pids_limit`, the agent's own (`scope:
cell`); the cores of every task of every process of the run against the canary's effective set, a wider one recorded as
`widened` and never killed; and the gate helper's resident memory against `gate_memory_bytes`, over which it SIGKILLs
the gate through its pidfd (a kill of its own, in `gate_kills`, never a cell's: the collection reads it as a death).

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

from expcore_onedata.sandbox import killcause as KC
from expcore_onedata.sandbox.apptainer.hidden import DEFAULT_MARGIN_BYTES, Hidden, HiddenCheck
from expcore_onedata.sandbox.apptainer.limits import LimitsCheck, Probe
from expcore_onedata.sandbox.apptainer.limitspoll import LimitsPoll
from expcore_onedata.sandbox.apptainer.node import NodeOps
from expcore_onedata.sandbox.apptainer.procs import Proc

log = logging.getLogger(__name__)

UNATTRIBUTED_WHY = (
    "hidden memory past the margin cannot be told this run's cell's: another run's cell is charged to the same cgroup"
)
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
        limits: LimitsCheck | None = None,
    ) -> None:
        self._node, self._tag, self._own = node, run_tag, own_pidns
        self._limit, self._spaces = memory_bytes, spaces
        self._period, self._disk_period, self._clock = memory_poll_s, disk_poll_s, clock
        self._hidden = HiddenCheck(node, own_pidns=own_pidns, margin_bytes=hidden_margin_bytes)
        self._lock = threading.RLock()
        # pids, cpus and the gate's memory (O70); None: the watch keeps memory and disks alone
        self._limits = None if limits is None else LimitsPoll(limits, node, self._lock, self._kill)
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
        self.ended: dict[str, Any] | None = None  # the kill that ended the run (`scope: run`), once one did
        self.on_run_end: Callable[[dict[str, Any]], None] = lambda event: None

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
            killed = _killed_for_memory(self._cell_kills)
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
                self._unreadable_limits()
                return
            total = sum(p.rss or 0 for p in members)
            self.peak_bytes = max(self.peak_bytes, total)
            if self._cell is not None:
                self._cell_peak = max(self._cell_peak, total)
        tasks = None
        if self._limits is not None:  # read before any kill: a poll that kills still shows what it saw (O70)
            self._limits.check_gate(found)
            tasks = self._limits.sample(members)
            self._limits.check_affinity(members)
        over_pids = tasks is not None and self._limits is not None and tasks > self._limits.check.pids
        if members and total > self._limit:
            beside = (KC.PIDS_LIMIT, "pids", tasks, self._limits.check.pids) if over_pids else None
            sent = self._kill(members, "memory_limit", "memory", total, self._limit, beside=beside)
            if sent == 0:
                with self._lock:  # nothing was signalled: never a reading of `over`
                    self._hidden.unreached()
            return
        if over_pids:  # this poll returns before the hidden check: the cell's memory was not read at it
            self._kill(members, KC.PIDS_LIMIT, "pids", tasks, self._limits.check.pids, unchecked=True)
            return
        self._check_hidden(found, members, before, after)
        if self._clock() >= self._next_walk:
            self._measure_spaces(members)

    def _check_hidden(self, found: list[Proc], members: list[Proc], before: int | None, after: int | None) -> None:
        """The cgroup's memory against the visible processes', and the cell's children against `/proc`: a growth past
        the margin that is the running cell's kills it (`memory_hidden`); the rest goes into the memory reading. A poll
        whose own listing leaves a growth past the margin lists `/proc` again, after the cgroup's second reading, and
        is judged on both (`hidden.HiddenCheck.suspect`)."""
        with self._lock:
            namespaces = set(self._ns)
            suspect = self._hidden.suspect(found, before, after, namespaces)
        listings: list[list[Proc]] | None = [found]
        if suspect:
            again = self._node.processes(())
            listings = None if again is None else [found, again]
        with self._lock:
            if listings is None:
                self._hidden.unreadable_poll()
                hidden = None
            else:
                hidden = self._hidden.assess(listings, before, after, namespaces)
            running = self._hidden.running
        if hidden is not None and not hidden.attributed:
            self._end_run(members, hidden)
        elif hidden is not None:
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

    # ------------------------------------------------------------------------------------------------ limits (O70)
    def _unreadable_limits(self) -> None:
        if self._limits is not None:
            self._limits.unreadable(self._cell is not None)

    def gate_kill(self) -> dict[str, Any] | None:
        """The watch's kill of the gate that signalled it, None when there was none (`limitspoll`)."""
        return None if self._limits is None else self._limits.gate_kill()

    def note_probe(self, probe: Probe | None) -> None:
        """What the start's canary read of the limits inside a cell (`limits.parse_probe`)."""
        if self._limits is not None:
            self._limits.note_probe(probe)

    def limits_record(self) -> dict[str, Any] | None:
        """The run's `infrastructure_facts.limits` (`limits.LimitsCheck.record`); None without a check."""
        if self._limits is None:
            return None
        with self._lock:
            memory = self._hidden.reading(_killed_for_memory(self.kills))
            return self._limits.record(self.kills, memory)

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

    def _end_run(self, members: list[Proc], hidden: Hidden) -> None:
        """Hidden memory past the margin that cannot be told the running cell's (another run's cell is in the cgroup):
        the cell cannot be isolated, so its processes are killed and the run ended (`on_run_end`, O72), its reading
        left unknown (`unattributed`), never `over`."""
        if self.ended is None:
            self._kill(members, "memory_limit", "memory_hidden", hidden.grown_bytes, hidden.allowance_bytes,
                       figures=_figures(hidden), scope="run", why=UNATTRIBUTED_WHY, attributed=False)  # fmt: skip
            with self._lock:
                self.ended = dict(self.kills[-1])
            self.on_run_end(dict(self.ended))
        with self._lock:
            self._hidden.ended()

    def _kill(
        self, members: list[Proc], reason: str, what: str, amount: int, limit: int, *,
        figures: Mapping[str, int] | None = None, scope: str = "cell", why: str | None = None, attributed: bool = True,
        beside: tuple[str, str, int, int] | None = None, unchecked: bool = False,
    ) -> int:  # fmt: skip
        """SIGKILL every process of the run through its pidfd, and record it against the running cell; how many were
        signalled. `beside`: another reason (`reason`, `what`, `amount`, `limit`) that the same signal ended the
        processes for, recorded in the same step, against the same cell. `unchecked`: the poll ends at this kill,
        before the hidden-memory check, so the cell's memory reading is marked unknown in the same step, against the
        same cell (`hidden.HiddenCheck.unchecked`)."""
        sent = sum(1 for p in members if self._node.signal(p.pid, p.start, signal.SIGKILL) is True)
        with self._lock:
            event = {
                "cell": self._cell,
                "reason": reason,
                "what": what,
                "amount": amount,
                "limit": limit,
                # nothing of a cell outlives its exec, so the run's processes are the running cell's: its own are
                # killed, the run goes on, as a container's OOM kill is the cell's on Docker; `run` when the cell
                # could not be isolated and the run was ended
                "scope": scope,
                **({"why": why} if why else {}),
                **({} if attributed else {"attributed": False}),
                "processes": len(members),
                "signalled": sent,
                "utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
                **({"figures": dict(figures)} if figures else {}),
            }
            events = [event]
            if beside is not None:
                reason2, what2, amount2, limit2 = beside
                events.append({**event, "reason": reason2, "what": what2, "amount": amount2, "limit": limit2})
            for each in events:
                self.kills.append(each)
                if self._cell is not None:
                    self._cell_kills.append(each)
            if unchecked:
                self._hidden.unchecked()
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
                        self._unreadable_limits()
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
                "gate_kills": [] if self._limits is None else list(self._limits.gate_kills),
                "polls": self.polls,
                "unreadable_polls": self.unreadable_polls,
                "disk": {k: dict(v) for k, v in self.disk.items()},
                "memory": self.reading(),
            }

    def reading(self) -> dict[str, Any]:
        """The run's memory reading with its figures (`hidden.HiddenCheck.reading`): `over` once a kill of the watch's
        for memory reached a process, `unknown` with its reasons, never `within` without the check having held."""
        with self._lock:
            return self._hidden.reading(_killed_for_memory(self.kills))

    def visibility(self) -> dict[str, Any]:
        """The reading the core's `process_visibility` instrument takes (`expcore.visibility`)."""
        with self._lock:
            return self._hidden.visibility(_killed_for_memory(self.kills))


def _killed_for_memory(kills: list[dict[str, Any]]) -> bool:
    """Whether a kill of the watch's for memory reached a process: one that signalled none bounded nothing, and one
    for memory that may be another run's (`attributed: false`) never shows this run's over its limit."""
    return any(k["reason"] == "memory_limit" and k["signalled"] > 0 and k.get("attributed", True) for k in kills)


def _figures(hidden: Hidden) -> dict[str, int]:
    return {"cgroup_anon_bytes": hidden.cgroup_anon_bytes, "visible_anon_bytes": hidden.visible_anon_bytes}


def spaces_of(run_dir: Path, limits: Mapping[str, int]) -> tuple[Space, ...]:
    """The run's three writable directories with their sizes: `work`, `tmp` and `shm` under the run's directory."""
    return tuple(Space(name, run_dir / name, limits[name]) for name in ("work", "tmp", "shm"))


__all__ = ["BYTES_PER_INODE", "Space", "Watch", "spaces_of", "usage"]
