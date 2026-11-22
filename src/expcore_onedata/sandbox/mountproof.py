# Provenance: new
"""The proof, read from outside the agent's cells, that they cannot see oneclient's process or reach the files that
hold or record the mount's token (#179, milestone 2, O60).

On an HPC node a cell (setuid Apptainer) and oneclient run as one uid, so the kernel's permission checks do not keep
the agent from oneclient's `/proc` entries (its environment holds the token) or from a file the host wrote. What keeps
it away is that each cell runs in a PID namespace of its own (`--containall`), whose `/proc` shows only the cell's
processes, and that its mount namespace binds none of the host's private directories. This module checks both from the
host, through the host's `/proc`, which no cell can see or write. Everything is read; nothing is taken from a cell.

`CellPoll` lists the processes at each poll and looks at each of this uid's (the others about once a second):

- **A cell of this run** is a process in a PID namespace nested in the watcher's (more `NSpid` entries in
  `/proc/<pid>/status`, readable for one's own processes without ptrace) whose mount table holds the run's FUSE mount
  (its device, which only this run's mount has). Each such namespace is proved once (`/proc/<pid>/ns/pid` names it,
  as the backend's watch does; one that cannot be read is proved per process) with label `cell`:
  - oneclient is as deep as the watcher (`oneclient_not_host`, `oneclient_unread`): a process in a nested namespace
    cannot see one in the namespace above, so it cannot name oneclient's pid to read its `/proc` entries, trace it or
    read its memory;
  - no mount of the cell is on the device of a `proc` mount of the host's table (`host_proc_in_cell`), as a bind of
    the host's `/proc` would be;
  - no mount of the cell is on a protected directory's device with a root that is, holds or lies under it
    (`protected_reachable`; one that could not be resolved at the start is `protected_unresolved`).
  Apptainer may still be making a cell's mounts when its first process is seen, so every process of this uid is looked
  at again at every poll for as long as it lives: a cell's namespace found later, or a problem a later look finds, is
  written then (a failed proof). A process of another uid is read again every `OTHER_EVERY` polls, as a setuid
  starter's process may become one of this uid's (on a node with `hidepid=invisible` it is not listed until it is).
- **A process of the node's PID namespace** whose mount table holds the run's FUSE mount anywhere but at the host's
  own mount point is a cell started without a PID namespace of its own: a failed proof, label `host_process`,
  `same_pid_namespace`.
- **At the close** (`close`), a cell of this run still alive is a failed proof, label `close`, `cell_alive_at_close`:
  the window was closed while the agent could still act, so nothing after is watched.

A failed `cell` proof records what failed it, in `mountdetail`'s words (kinds of mount, never a path): each mount that
made `host_proc_in_cell` or `protected_reachable`, and the process's root, mount namespace and root mount.

A poll that cannot list `/proc`, or a nested process of this uid whose mount table cannot be read, counts as a failed
poll: it may have hidden a cell. What this cannot see: a cell that starts and ends between two polls is never proved;
the run's proof rests on the cells it caught (the backend's start holds its canary cell open for a second) and on every
cell being started by the same command line.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from expcore_onedata.sandbox import mountdetail as MD
from expcore_onedata.sandbox.hostops import MountTableError, parse_mountinfo

ONECLIENT_NOT_HOST = "oneclient_not_host"
ONECLIENT_UNREAD = "oneclient_unread"
HOST_PROC_IN_CELL = "host_proc_in_cell"
PROTECTED_REACHABLE = "protected_reachable"
PROTECTED_UNRESOLVED = "protected_unresolved"
SAME_PID_NAMESPACE = "same_pid_namespace"
CELL_ALIVE_AT_CLOSE = "cell_alive_at_close"
CELL, HOST_PROCESS, CLOSE = "cell", "host_process", "close"
OTHER_EVERY = 20  # a process of another uid is read again every this many polls (about 1 s at the watcher's 50 ms)


class ProcView(Protocol):
    """Reads of the host's `/proc` (a fake in tests). `read` and `readlink` raise `OSError`; `FileNotFoundError` and
    `ProcessLookupError` mean the process is gone."""

    def read(self, path: str) -> str: ...

    def readlink(self, path: str) -> str: ...

    def pids(self) -> Iterable[int]: ...


class HostProc:
    """The real `/proc` of the host, as the watcher sees it."""

    def read(self, path: str) -> str:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def pids(self) -> Iterable[int]:
        return [int(name) for name in os.listdir("/proc") if name.isdigit()]


@dataclass(frozen=True)
class Protected:
    """A protected directory as a cell's mount table would show it: the device (`major:minor`) of the file system it
    is on and its path inside that file system; both None when it could not be resolved."""

    dev: str | None
    fs_path: str | None


@dataclass(frozen=True)
class Host:
    """What the watcher learns of the host at its start: its own depth, uid and mount table's `proc` devices, the run's
    FUSE mount (its device and the host's mount point of it), the protected directories and oneclient; and, for what a
    failed proof records, the device and root of the host's `/` mount (`root_mount`), the watcher's mount namespace
    (`mnt_ns`) and the host's mounts as (device, root, point) (`entries`); None or empty when unread."""

    depth: int
    uid: int
    proc_devs: frozenset[str]
    fuse_dev: str | None
    mountpoint: str
    protected: tuple[Protected, ...]
    oneclient: int
    root_mount: tuple[str, str] | None = None
    mnt_ns: str | None = None
    entries: frozenset[tuple[str, str, str]] = frozenset()


def status_field(text: str, name: str) -> str | None:
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key == name:
            return value.strip()
    return None


def nspid(text: str) -> tuple[int, ...] | None:
    """A process's pid in each PID namespace it is in, outermost first (`NSpid` of its status), or None."""
    field = status_field(text, "NSpid")
    try:
        values = tuple(int(v) for v in (field or "").split())
    except ValueError:
        return None
    return values or None


def real_uid(text: str) -> int | None:
    field = (status_field(text, "Uid") or "").split()
    return int(field[0]) if field and field[0].isdigit() else None


def start_time(stat_text: str) -> int | None:
    """A process's start time in clock ticks after boot (`/proc/<pid>/stat`, field 22), or None. The command name may
    hold spaces and parentheses, so the fields are read after the last `)`."""
    fields = stat_text.rpartition(")")[2].split()
    return int(fields[19]) if len(fields) > 19 and fields[19].isdigit() else None


def protected_at(path: str, host_mountinfo: str) -> Protected:
    """A protected directory's device and its path inside its file system, from the host's mount table: the mount whose
    point is the longest prefix of the real path, held to the device `stat` gives. Unresolved on any doubt."""
    try:
        real = os.path.realpath(path)
        found = os.stat(real)
        mounts = parse_mountinfo(host_mountinfo)
    except (OSError, MountTableError):
        return Protected(None, None)
    dev = f"{os.major(found.st_dev)}:{os.minor(found.st_dev)}"
    holding = [m for m in mounts if _under(real, m["mountpoint"])]
    if not holding:
        return Protected(None, None)
    mount = max(holding, key=lambda m: len(m["mountpoint"]))
    if mount["dev"] != dev:
        return Protected(None, None)
    inside = posixpath.relpath(real, mount["mountpoint"])
    return Protected(dev, posixpath.normpath(posixpath.join(mount["root"], inside)))


def _under(path: str, base: str) -> bool:
    return base == "/" or path == base or path.startswith(base.rstrip("/") + "/")


def _nested(one: str, other: str) -> bool:
    """Whether one path is, holds or lies under the other."""
    return _under(one, other) or _under(other, one)


def fuse_dev(host_mountinfo: str, mountpoint: str) -> str | None:
    """The device of the FUSE mount at `mountpoint` in the host's table (the last one mounted there), or None."""
    try:
        mounts = parse_mountinfo(host_mountinfo)
    except MountTableError:
        return None
    found = [m["dev"] for m in mounts if m["mountpoint"] == mountpoint and m["fstype"].startswith("fuse")]
    return found[-1] if found else None


def proc_devs(host_mountinfo: str) -> frozenset[str]:
    """The devices of every `proc` mount of the host's table; `MountTableError` when it cannot be read whole."""
    return frozenset(m["dev"] for m in parse_mountinfo(host_mountinfo) if m["fstype"] == "proc")


def host_mounts(host_mountinfo: str) -> tuple[tuple[str, str] | None, frozenset[tuple[str, str, str]]]:
    """The device and root of the host's `/` mount (the last one) and every mount of its table as (device, root, point);
    None and empty when the table cannot be read whole."""
    try:
        mounts = parse_mountinfo(host_mountinfo)
    except MountTableError:
        return None, frozenset()
    roots = [(m["dev"], m["root"]) for m in mounts if m["mountpoint"] == "/"]
    return roots[-1], frozenset((m["dev"], m["root"], m["mountpoint"]) for m in mounts)


def _relation(root: str, protected: str) -> str:
    """How a mount's root lies to a protected directory's path in one file system: `is`, `holds` or `under` it."""
    if root == protected:
        return "is"
    return "under" if _under(root, protected) else "holds"


def offending(cell_mounts: Sequence[Mapping[str, str]], host: Host) -> list[MD.Offence]:
    """Each mount of a cell's table that makes a problem: one on a `proc` device of the host's, or one that is, holds or
    lies under a protected directory (with the directory's index and how the mount's root lies to it)."""
    found: list[MD.Offence] = [(HOST_PROC_IN_CELL, m, None, None) for m in cell_mounts if m["dev"] in host.proc_devs]
    for index, p in enumerate(host.protected):
        if p.dev is None or p.fs_path is None:
            continue
        found += [
            (PROTECTED_REACHABLE, m, index, _relation(m["root"], p.fs_path))
            for m in cell_mounts
            if m["dev"] == p.dev and _nested(m["root"], p.fs_path)
        ]
    return found


def mount_problems(cell_mounts: Sequence[Mapping[str, str]], host: Host) -> list[str]:
    """What a cell's mount table shows that it must not: the host's `/proc`, a protected directory."""
    named = {problem for problem, *_ in offending(cell_mounts, host)}
    found = []
    if HOST_PROC_IN_CELL in named:
        found.append(HOST_PROC_IN_CELL)
    if any(p.dev is None or p.fs_path is None for p in host.protected):
        found.append(PROTECTED_UNRESOLVED)
    if PROTECTED_REACHABLE in named:
        found.append(PROTECTED_REACHABLE)
    return found


def oneclient_problems(proc: ProcView, host: Host) -> list[str]:
    """Whether oneclient is in the watcher's own PID namespace, as deep as it."""
    try:
        values = nspid(proc.read(f"/proc/{host.oneclient}/status"))
    except OSError:
        values = None
    if values is None:
        return [ONECLIENT_UNREAD]
    return [] if len(values) == host.depth else [ONECLIENT_NOT_HOST]


Seen = tuple[int, int]  # a process by its pid and start time


class CellPoll:
    """The polls of one run (the module's docstring). `emit(label, ok, problems, cell, detail)` writes a proof line;
    `detail` is None for a passing one, else the keys the failed one gains (`mountdetail`)."""

    def __init__(self, proc: ProcView, host: Host, emit: Callable[..., None]):
        self._proc, self._host, self._emit = proc, host, emit
        self._later: dict[int, int] = {}  # processes of another uid by pid, with the poll that reads them again
        self._proved: dict[Any, set[str]] = {}  # each namespace proved, with the problems written for it
        self.cells: dict[Seen, int] = {}  # the processes found to be this run's cells, with their depth
        self.polls = 0
        self.failed = 0

    def poll(self) -> None:
        """Look at each process of this uid, and at each of another uid when it is due (the module's docstring)."""
        self.polls += 1
        try:
            pids = list(self._proc.pids())
        except OSError:
            self.failed += 1
            return
        listed = set(pids)
        self._later = {pid: due for pid, due in self._later.items() if pid in listed}
        broken = False
        for pid in pids:
            if self._later.get(pid, 0) > self.polls:
                continue
            broken = self._look(pid) is False or broken
        if broken:
            self.failed += 1

    def close(self) -> None:
        """The last poll, then a failed proof for each of the run's cells still alive."""
        self.poll()
        for (pid, started), depth in sorted(self.cells.items()):
            if self._started(pid) == started:
                cell = {"pid": pid, "start_time": started, "nspid_depth": depth}
                self._emit(CLOSE, False, [CELL_ALIVE_AT_CLOSE], cell, None)

    def _started(self, pid: int) -> int | None:
        try:
            return start_time(self._proc.read(f"/proc/{pid}/stat"))
        except OSError:
            return None

    def _look(self, pid: int) -> bool | None:
        """Look at one process: False when it is one of ours that could not be read whole, else None."""
        started = self._started(pid)
        if started is None:
            return None  # gone between the listing and the read
        try:
            status = self._proc.read(f"/proc/{pid}/status")
        except OSError:
            return None
        values = nspid(status)
        if real_uid(status) != self._host.uid or values is None:
            self._later[pid] = self.polls + OTHER_EVERY
            return None
        try:
            mounts = parse_mountinfo(self._proc.read(f"/proc/{pid}/mountinfo"))
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, MountTableError):
            # a nested process of ours we cannot read may be a cell: a failed poll, and looked at again next poll
            return False if len(values) > self._host.depth else None
        self._judge(pid, started, values, mounts)
        return None

    def _judge(self, pid: int, started: int, values: tuple[int, ...], mounts: list[dict[str, str]]) -> None:
        """A proof line for a cell of the run's the first time its namespace is seen, and a failed one whenever a later
        look finds a problem not written yet; a failed one for a process of the node's namespace holding the run's
        mount elsewhere."""
        ours = [m for m in mounts if self._host.fuse_dev is not None and m["dev"] == self._host.fuse_dev]
        if not ours:
            return
        cell = {"pid": pid, "start_time": started, "nspid_depth": len(values)}
        if len(values) <= self._host.depth:
            if any(m["mountpoint"] != self._host.mountpoint for m in ours) and (pid, started) not in self.cells:
                self.cells[(pid, started)] = len(values)
                self._emit(HOST_PROCESS, False, [SAME_PID_NAMESPACE], cell, {"process": self._view(pid, mounts)})
            return
        self.cells[(pid, started)] = len(values)
        key = self._namespace(pid) or (pid, started)
        problems = oneclient_problems(self._proc, self._host) + mount_problems(mounts, self._host)
        if key not in self._proved:
            self._proved[key] = set(problems)
            self._emit(CELL, not problems, problems, cell, self._detail(pid, mounts) if problems else None)
        elif set(problems) - self._proved[key]:
            self._proved[key] |= set(problems)
            self._emit(CELL, False, problems, cell, self._detail(pid, mounts))

    def _root_mount(self, mounts: Sequence[Mapping[str, str]]) -> str:
        """Whose a process's `/` mount is: the host's (the same device and root, so the pivot is not done), its own, or
        unknown (it has none, or the host's could not be read)."""
        roots = [(m["dev"], m["root"]) for m in mounts if m["mountpoint"] == "/"]
        if not roots or self._host.root_mount is None:
            return MD.UNKNOWN
        return MD.HOST if roots[-1] == self._host.root_mount else MD.OWN

    def _view(self, pid: int, mounts: Sequence[Mapping[str, str]]) -> dict[str, str]:
        return MD.process_view(pid, self._host.mnt_ns, self._proc.readlink, self._root_mount(mounts))

    def _detail(self, pid: int, mounts: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        """What failed a cell's proof (the module's docstring): the offending mounts and the process's view."""
        return {**MD.entries(offending(mounts, self._host), self._host.entries), "process": self._view(pid, mounts)}

    def _namespace(self, pid: int) -> str | None:
        try:
            link = self._proc.readlink(f"/proc/{pid}/ns/pid")
        except OSError:
            return None
        return link if link.startswith("pid:[") else None


__all__ = [
    "CellPoll",
    "Host",
    "HostProc",
    "ProcView",
    "Protected",
    "fuse_dev",
    "host_mounts",
    "proc_devs",
    "protected_at",
]
