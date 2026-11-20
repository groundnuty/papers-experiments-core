# Provenance: new
"""What the survivors' diagnostics keep of what was read (`stuckread`, O97): the capped record of each survivor and the
uncapped `blocks` the attribution reads.

The record is the one the run's evidence keeps (at most `SURVIVORS_KEPT` survivors, each with `TASKS_KEPT` tasks and
`WAITS_KEPT` things it waits on); `blocks` lists every survivor and every task, so that a process the record leaves out
is still judged. Neither holds a pid, a command line, an environment or a file's content: `space` and `path` are given
only for the data mount.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.sandbox.apptainer import stuck as S
from expcore_onedata.sandbox.hostops import describe_error

log = logging.getLogger(__name__)

SURVIVORS_KEPT, TASKS_KEPT, WAITS_KEPT = 8, 4, 4  # what the record keeps; `blocks` is never cut
FUSECTL_STATES = ("writable", "absent", "not_writable")
FSTYPE = re.compile(r"[A-Za-z0-9_.+-]{1,32}")


@dataclass(frozen=True)
class Waits:
    """One thing a task waits on: where it is and, for a descriptor, which one, its offset and its flags."""

    place: S.Place
    fd: int | None = None
    pos: int | None = None
    flags: str | None = None

    def record(self) -> dict[str, Any]:
        entry: dict[str, Any] = {} if self.fd is None else {"fd": self.fd}
        entry["mount"] = self.place.mount
        entry["fstype"] = self.place.fstype if FSTYPE.fullmatch(self.place.fstype) else "unknown"
        if self.place.mount == S.DATA:
            entry["space"], entry["path"] = self.place.space, self.place.rest
        if self.pos is not None:
            entry["pos"] = self.pos
        if self.flags is not None:
            entry["flags"] = self.flags
        return entry

    def object(self) -> dict[str, Any]:
        """What `blocks` keeps: the role and, for the data mount, the space."""
        return {"mount": self.place.mount, "space": self.place.space if self.place.mount == S.DATA else None}


@dataclass
class Task:
    tid: int
    state: str
    wchan: str | None = None
    syscall: Any = S.UNREADABLE  # a `stuck.Syscall`, or `running` or `unreadable`
    objects: list[Waits] | None = None  # what it waits on; None: not known (yet)


@dataclass
class Survivor:
    pid: int
    comm: str
    state: str
    tasks: list[Task] | None = None  # None: the process could not be read (gone, another process, hidden)
    mounts: tuple[S.MountEntry, ...] | None = None
    cwd: str | None = None  # the working directory's absolute path
    root: str = ""  # the process's root directory when it is not `/` (a chroot): its links carry this prefix
    fuse_fds: list[Waits] | None = None
    fds_complete: bool = True
    maps: list[Waits] | None = None
    maps_complete: bool = True
    base: str = ""  # the first live task's directory, through which the process's shared tables are read


def syscall_record(call: Any, known: bool) -> Any:
    if isinstance(call, str):
        return call
    fd = S.fd_call(call) if known else None
    return {"nr": call.nr, **({} if fd is None else {"fd": fd[1]})}


def survivor_record(proc: Survivor, roles: S.Roles, known: bool) -> dict[str, Any]:
    """The capped record of one survivor, from what was read: no read is made here."""
    tasks = proc.tasks or []
    shown = sorted(tasks, key=lambda task: task.state != "D")[:TASKS_KEPT]
    waits: list[dict[str, Any]] = []
    for task in tasks:
        for found in task.objects or []:
            entry = found.record()
            if entry not in waits:
                waits.append(entry)
    if not waits:  # no object of a task's own: the FUSE descriptors read for it, if any
        waits = [w.record() for w in (proc.fuse_fds or [])]
    cwd = None
    if proc.cwd is not None and proc.mounts is not None and (entry := S.mount_of_path(proc.mounts, proc.cwd)):
        cwd = roles.of(entry)
    return {
        "comm": proc.comm[:32],
        "state": proc.state,
        "tasks": [{"state": t.state, "wchan": t.wchan, "syscall": syscall_record(t.syscall, known)} for t in shown],
        "waits_on": waits[:WAITS_KEPT],
        **({} if cwd is None else {"cwd_mount": cwd}),
    }


def block_record(proc: Survivor) -> dict[str, Any]:
    if proc.tasks is None:
        return {"state": proc.state, "tasks": None}
    return {"state": proc.state, "tasks": [{"state": t.state, "objects": objects_of(t)} for t in proc.tasks]}


def objects_of(task: Task) -> list[dict[str, Any]] | None:
    """A D task's objects by role and space, each once; None when it is no D task or its objects are not known."""
    if task.state != "D" or not task.objects:
        return None
    found: list[dict[str, Any]] = []
    for waits in task.objects:
        entry = waits.object()
        if entry not in found:
            found.append(entry)
    return found


def connection_record(connection: Callable[[], Mapping[str, Any] | None] | None) -> dict[str, Any] | None:
    if connection is None:
        return None
    try:
        given = connection()
    except Exception as error:
        log.error("the run's connection could not be read for the diagnosis: %s", describe_error(error))
        return None
    state = given.get("fusectl") if isinstance(given, Mapping) else None
    return {"fusectl": state} if state in FUSECTL_STATES else None
