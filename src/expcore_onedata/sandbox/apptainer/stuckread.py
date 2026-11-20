# Provenance: new
"""The diagnostics of a cell's survivors, read from `/proc` alone (O97): the reader behind `deadline.Diagnosing`.

At the `survived` step of a cell's deadline (`deadline.enforce`), before the mount is aborted, the tracker reads each
process that outlived its kill: its tasks (`task/<tid>/stat`, `wchan`, `syscall`) and what each task in uninterruptible
sleep waits on, placed on the cell's own mounts (`stuck`). The answer holds the record's keys, which `deadline` merges,
and `blocks`, the uncapped reading `Escalation.attribute` takes:

    {"survivors": [{"comm": ..., "state": "D", "tasks": [{"state": "D", "wchan": "<symbol>" | None,
                    "syscall": {"nr": 0, "fd": 3} | {"nr": 257} | "running" | "unreadable"}],
                    "waits_on": [{"fd": 3, "mount": "data", "fstype": "fuse.<name>", "space": ..., "path": ...,
                                  "pos": 0, "flags": "<octal>"}], "cwd_mount": "work"}],        # at most 8, 4, 4
     "survivors_cut": 0, "connection": {"fusectl": "writable"},
     "blocks": [{"state": "Z", "tasks": [{"state": "D", "objects": [{"mount": "data", "space": "<name>"}] | None}]
                 | None}]}                                                                    # every survivor

`objects` is None for a task whose blocking object is not known (an unreadable `syscall` or fd, a path that could not be
read) and for a task not in `D`; a survivor whose tasks could not be listed has `tasks` None. A zombie leader with a
live thread in `D` is reported by its thread: the leader's own entries are empty, so every read is under `task/<tid>/`.
`space` and `path` are given only for the data mount. **Nothing a process wrote is read: no file content, no command
line, no environment**, and no pid or node name is recorded.

**The blocking object**, by the task's `syscall` (`stuck.FD_CALLS`, `stuck.PATH_CALLS`): the descriptor a read-family
call names, placed by its link and its `fdinfo` mount; or the path a path call names, read from the task's memory at the
call's pointer argument (at most 4 KiB up to a NUL, the agent's own argument) and placed lexically on the task's mount
table; for any other call, or one blocked outside a syscall, **the holdings rule**: every FUSE object the process holds
(its descriptors, its mapped files, its working directory), unknown when it holds none or when any could not be read.
An unreadable `syscall` is never guessed from the holdings.

**It never touches the target.** Reads are `listdir`, `readlink` and `/proc` text through a `ProcReader`; no `stat`,
`open` or `read` of a file the process waits on. Every read runs in a daemon thread that is waited for at most the
margin left: the cheap files first (`stat`, `wchan`, `syscall`, `fd`, `fdinfo`, `mountinfo`), `maps` and the task's
memory last, and a read that blocks (they lock the process's address space) leaves what it did not reach unknown.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from expcore_onedata.sandbox.apptainer import procs as P
from expcore_onedata.sandbox.apptainer import stuck as S
from expcore_onedata.sandbox.apptainer.stuckrecord import (
    SURVIVORS_KEPT,
    Survivor,
    Task,
    Waits,
    block_record,
    connection_record,
    survivor_record,
)
from expcore_onedata.sandbox.hostops import describe_error

log = logging.getLogger(__name__)

FDS_READ = 2048  # the most descriptors of a process read for the holdings rule: more is unknown, never a partial set
DIAGNOSE_SHARE = 0.5  # of the margin left, at most, so that a read that blocks leaves the abort the rest of it
DIAGNOSE_MAX_S = 10.0
TEXT_LIMIT, MAPS_LIMIT = 1 << 18, 1 << 22
WCHAN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")


class ProcReader(Protocol):
    """The `/proc` of a node as the reader needs it, with names relative to it (`1234/task/1234/stat`). Each call
    answers None where the file or link cannot be read, and raises nothing."""

    def listdir(self, name: str) -> list[str] | None: ...

    def read(self, name: str, limit: int) -> bytes | None: ...

    def readlink(self, name: str) -> str | None: ...

    def read_at(self, name: str, offset: int, size: int) -> bytes | None: ...


class LocalProcReader:
    """The node's own `/proc`, as this uid reads it."""

    def __init__(self, root: str = "/proc") -> None:
        self._root = root

    def listdir(self, name: str) -> list[str] | None:
        try:
            return os.listdir(f"{self._root}/{name}")
        except OSError:
            return None

    def read(self, name: str, limit: int) -> bytes | None:
        try:
            with open(f"{self._root}/{name}", "rb") as stream:
                return stream.read(limit)
        except OSError:
            return None

    def readlink(self, name: str) -> str | None:
        try:
            return os.readlink(f"{self._root}/{name}")
        except OSError:
            return None

    def read_at(self, name: str, offset: int, size: int) -> bytes | None:
        try:
            descriptor = os.open(f"{self._root}/{name}", os.O_RDONLY)
        except OSError:
            return None
        try:
            return os.pread(descriptor, size, offset)
        except (OSError, OverflowError):
            return None
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class Diagnosis:
    """What a backend gives its tracker to read survivors with: the cell's fixed targets (`roles`, asked when the
    deadline fires: the run's connection is a new device after a restore) and, optionally, the run's connection record
    (`{"fusectl": "writable" | "absent" | "not_writable"}`, None when unknown)."""

    roles: Callable[[], S.Roles]
    connection: Callable[[], Mapping[str, Any] | None] | None = None


class _Reader:
    """The reads of one diagnosis: phase A (cheap files) over every survivor, then phase B (`maps`, memory)."""

    def __init__(self, view: ProcReader, roles: S.Roles, machine: str) -> None:
        self.view, self.roles = view, roles
        self.known = machine in S.X86_64
        self.pending: list[tuple[Survivor, Task]] = []  # D tasks whose object needs `maps` or the task's memory

    def text(self, name: str, limit: int = TEXT_LIMIT) -> str | None:
        data = self.view.read(name, limit)
        return None if data is None else data.decode("utf-8", "replace")

    # ------------------------------------------------------------------------------------------------ phase A
    def look(self, proc: Mapping[str, Any]) -> Survivor:
        """The process as far as the cheap files go; unknown (`tasks` None) when it is gone, another process by this pid
        or hidden. A D task whose object needs more is queued for `finish`."""
        pid = proc.get("pid")
        found = Survivor(pid if isinstance(pid, int) else -1, str(proc.get("comm")), str(proc.get("state")))
        stat = P.parse_stat(self.text(f"{pid}/stat") or "")
        if stat is None or stat[3] != proc.get("start"):
            return found
        found.comm, found.state = stat[0], stat[1]
        tids = self.view.listdir(f"{pid}/task")
        if tids is None:
            return found
        found.tasks = [task for tid in sorted(int(t) for t in tids if t.isdigit()) if (task := self._task(found, tid))]
        if found.tasks:
            self._shared(found, f"{pid}/task/{found.tasks[0].tid}")
        for task in found.tasks:
            if task.state == "D":
                self._object(found, task, f"{pid}/task/{task.tid}")
        return found

    def _task(self, proc: Survivor, tid: int) -> Task | None:
        base = f"{proc.pid}/task/{tid}"
        stat = P.parse_stat(self.text(f"{base}/stat") or "")
        if stat is None or stat[1] in ("Z", "X"):
            return None  # gone, or a zombie: not blocked on anything
        task = Task(tid, stat[1])
        wchan = (self.text(f"{base}/wchan", 128) or "").strip()
        task.wchan = wchan if WCHAN.fullmatch(wchan) else None  # `0` on some kernels, and unreadable, read as None
        reading = self.text(f"{base}/syscall", 512)
        task.syscall = S.UNREADABLE if reading is None else S.parse_syscall(reading)
        return task

    def _shared(self, proc: Survivor, base: str) -> None:
        """The process's mount table and working directory, through one live task's directory."""
        proc.base = base
        table = self.text(f"{base}/mountinfo")
        proc.mounts = None if table is None else S.parse_mountinfo(table)
        top = S.link_path(self.view.readlink(f"{base}/root"))
        proc.root = "" if top in (None, "/") else top
        proc.cwd = self._path(proc, self.view.readlink(f"{base}/cwd"))

    def _path(self, proc: Survivor, target: str | None) -> str | None:
        """The absolute path a link of the process names, as the process sees it (`stuck.within_root`)."""
        return S.within_root(S.link_path(target), proc.root)

    def _object(self, proc: Survivor, task: Task, base: str) -> None:
        call = task.syscall
        if isinstance(call, str):  # unreadable or running: never guessed from what the process holds
            return
        fd = S.fd_call(call) if self.known else None
        if fd is not None:
            found = self._descriptor(proc, base, fd[1])
            task.objects = None if found is None else [found]
        else:
            self.pending.append((proc, task))  # a path call's argument (task memory), or else the holdings rule

    def _descriptor(self, proc: Survivor, base: str, fd: int) -> Waits | None:
        """The file descriptor `fd` placed on the process's mounts; None when any read failed."""
        if fd < 0 or proc.mounts is None:
            return None
        target = self.view.readlink(f"{base}/fd/{fd}")
        info = S.parse_fdinfo(self.text(f"{base}/fdinfo/{fd}", 4096) or "")
        entry = None if info.mnt_id is None else S.mount_with_id(proc.mounts, info.mnt_id)
        if target is None or entry is None:
            return None
        return Waits(self.roles.place(entry, self._path(proc, target)), fd, info.pos, info.flags)

    def _fuse_fds(self, proc: Survivor) -> None:
        """Every FUSE descriptor of the process (once), and whether the list is whole."""
        if proc.fuse_fds is not None or not proc.base:
            return
        proc.fuse_fds = []
        names = self.view.listdir(f"{proc.base}/fd")
        if names is None or len(names) > FDS_READ:
            proc.fds_complete = False
            return
        for name in sorted(n for n in names if n.isdigit()):
            found = self._descriptor(proc, proc.base, int(name))
            if found is None:
                proc.fds_complete = False
            elif found.place.fuse:
                proc.fuse_fds.append(found)

    # ------------------------------------------------------------------------------------------------ phase B
    def finish(self, procs: Sequence[Survivor]) -> None:
        """The objects that need the task's memory or its `maps`, after every cheap read of every survivor; then, for a
        survivor with a task in `D` and no object found, its FUSE descriptors, for the record's `waits_on`."""
        for proc, task in list(self.pending):
            call = task.syscall
            path = S.path_call(call) if self.known and not isinstance(call, str) else None
            task.objects = self._holdings(proc) if path is None else self._path_object(proc, task, path)
        for proc in list(procs):
            tasks = proc.tasks or []
            if any(t.state == "D" for t in tasks) and not any(t.objects for t in tasks):
                self._fuse_fds(proc)

    def _path_object(self, proc: Survivor, task: Task, call: tuple[str, int | None, int]) -> list[Waits] | None:
        _, dirfd, pointer = call
        base = f"{proc.pid}/task/{task.tid}"
        path = S.cstring(self.view.read_at(f"{base}/mem", pointer, S.PATH_LIMIT))
        if path is None or proc.mounts is None:
            return None
        if path == "":  # an `AT_EMPTY_PATH` call names its directory descriptor itself; any other empty path is no wait
            named = None if dirfd is None or dirfd == S.AT_FDCWD else self._descriptor(proc, base, dirfd)
            return None if named is None else [named]
        if path.startswith("/"):
            absolute = S.normalize(path)
        else:
            start = proc.cwd if dirfd in (None, S.AT_FDCWD) else self._directory(proc, base, dirfd)
            if start is None:
                return None
            absolute = S.normalize(f"{start}/{path}")
        entry = S.mount_of_path(proc.mounts, absolute)
        return None if entry is None else [Waits(self.roles.place(entry, absolute))]

    def _directory(self, proc: Survivor, base: str, fd: int) -> str | None:
        return self._path(proc, self.view.readlink(f"{base}/fd/{fd}")) if fd >= 0 else None

    def _holdings(self, proc: Survivor) -> list[Waits] | None:
        """The holdings rule: the FUSE objects the process holds, None when it holds none or any read failed."""
        self._fuse_fds(proc)
        self._mapped(proc)
        if not (proc.fds_complete and proc.maps_complete) or proc.mounts is None:
            return None
        held = list(proc.fuse_fds or []) + list(proc.maps or [])
        if proc.cwd is not None and (entry := S.mount_of_path(proc.mounts, proc.cwd)) is not None:
            held.append(Waits(self.roles.place(entry, proc.cwd)))
        found = [w for w in held if w.place.fuse]
        return found or None

    def _mapped(self, proc: Survivor) -> None:
        """The FUSE files the process has mapped, from `maps` (once)."""
        if proc.maps is not None:
            return
        proc.maps = []
        data = self.view.read(f"{proc.base}/maps", MAPS_LIMIT) if proc.base else None
        if data is None or proc.mounts is None:
            proc.maps_complete = False
            return
        seen: set[str] = set()
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split(None, 5)
            path = self._path(proc, parts[5]) if len(parts) == 6 else None
            if path is None or path in seen:
                continue
            seen.add(path)
            entry = S.mount_of_path(proc.mounts, path)
            if entry is not None and entry.fuse:
                proc.maps.append(Waits(self.roles.place(entry, path)))


def diagnose(
    view: ProcReader,
    left: Sequence[Mapping[str, Any]],
    timeout_s: float,
    roles: S.Roles,
    *,
    connection: Callable[[], Mapping[str, Any] | None] | None = None,
    machine: str | None = None,
) -> dict[str, Any] | None:
    """What the survivors in `left` (each with its `pid`, `start`, `comm` and `state`) are blocked on, within
    `timeout_s` (the module's docstring); None when there is nothing to read or no time. A survivor the reads did not
    reach in time is unknown (its `tasks` None), never read as not blocked."""
    if not left or not timeout_s > 0:
        return None
    reader = _Reader(view, roles, machine or platform.machine())
    procs = [Survivor(-1, str(p.get("comm")), str(p.get("state"))) for p in left]

    def work() -> None:
        try:
            for index, proc in enumerate(left):
                procs[index] = reader.look(proc)
            reader.finish(procs)
        except Exception as error:  # what was not reached stays unknown
            log.error("the survivors' diagnosis stopped: %s", describe_error(error))

    thread = threading.Thread(target=work, name="stuck-reader", daemon=True)
    thread.start()
    thread.join(min(timeout_s * DIAGNOSE_SHARE, DIAGNOSE_MAX_S))  # the abort that frees the cell keeps the rest
    shown = [survivor_record(proc, reader.roles, reader.known) for proc in procs[:SURVIVORS_KEPT]]
    found: dict[str, Any] = {
        "survivors": shown,
        "survivors_cut": max(0, len(procs) - SURVIVORS_KEPT),
        "blocks": [block_record(proc) for proc in procs],
    }
    if (conn := connection_record(connection)) is not None:
        found["connection"] = conn
    return found


def diagnose_node(
    node: Any, diagnosis: Diagnosis | None, left: Sequence[Mapping[str, Any]], timeout_s: float
) -> dict[str, Any] | None:
    """`diagnose` over a node's own `/proc` (`proc_reader`); None for a node with none or a run with no `diagnosis`."""
    make = getattr(node, "proc_reader", None)
    if make is None or diagnosis is None:
        return None
    return diagnose(make(), left, timeout_s, diagnosis.roles(), connection=diagnosis.connection)


__all__ = ["Diagnosis", "LocalProcReader", "ProcReader", "Waits", "diagnose", "diagnose_node"]
