# Provenance: new
"""What the survivors' diagnostics keep of what was read (`stuckread`, O97): the `reading` the attribution is made on
and the capped `survivors` the record keeps.

Both are built from the same entries. A `reading` entry is one survivor read whole, with the `pid` and `start` the
deadline listed it by (which the escalation checks cover every survivor, `deadline._Run.covers`) and every live task;
the record's `survivors` entry is the same cut to what a record may hold (`expcore.records.cell_deadline`): no pid, at
most `TASKS_KEPT` tasks with `tasks_cut` counting the rest, at most `WAITS_KEPT` things it waits on. A task's `blocking`
is what it waits on, resolved (`attribution`'s contract): `via` says how (`fd`, `path`, or `holdings` for a task blocked
outside any syscall), `objects` where the files are (`mount`, and for the data mount its `space` and the `path` below
it), `cut` whether the list was cut. Unknown is None, never an empty list. Nothing here holds a command line, an
environment or a file's content.
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

SURVIVORS_KEPT, TASKS_KEPT, WAITS_KEPT = 8, 4, 4  # what the record keeps; the reading is never cut
OBJECTS_KEPT = 16  # the most objects of one task's `blocking`: more is `cut`, which the attribution reads as unknown
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

    def object(self, *, path: bool = True) -> dict[str, Any]:
        """What `blocking` keeps: the role and, for the data mount, the space and (unless `path` is False) the path
        below it."""
        data = self.place.mount == S.DATA
        return {
            "mount": self.place.mount,
            "space": self.place.space if data else None,
            "path": self.place.rest if data and path else None,
        }


@dataclass
class Task:
    tid: int
    state: str
    wchan: str | None = None
    syscall: Any = S.UNREADABLE  # a `stuck.Syscall`, or `running` or `unreadable`
    via: str | None = None  # how its object is resolved: `fd`, `path` or `holdings`; None: it is not resolved at all
    objects: list[Waits] | None = None  # what it waits on; None: not known (yet)


@dataclass
class Survivor:
    pid: int
    start: str  # both as the deadline's listing names the process: the reading is matched to it by these
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


def blocking_record(task: Task) -> dict[str, Any] | None:
    """What a task waits on, resolved (the module's docstring); None when it is not known: it has no `via` or no object.
    Objects are each once; the holdings carry no path (many files of one space are one object), and more than
    `OBJECTS_KEPT` are cut and flagged."""
    if task.via is None or not task.objects:
        return None
    found: list[dict[str, Any]] = []
    for waits in task.objects:
        entry = waits.object(path=task.via != "holdings")
        if entry not in found:
            found.append(entry)
    return {"via": task.via, "cut": len(found) > OBJECTS_KEPT, "objects": found[:OBJECTS_KEPT]}


def task_record(task: Task, known: bool) -> dict[str, Any]:
    return {
        "state": task.state,
        "wchan": task.wchan,
        "syscall": syscall_record(task.syscall, known),
        "blocking": blocking_record(task) if task.state == "D" else None,
    }


def reading_entry(proc: Survivor, roles: S.Roles, known: bool) -> dict[str, Any]:
    """One survivor, read whole: the entry of `reading` (the module's docstring). A process that could not be read has
    `tasks` None, so the verdict on it is unknown, never that it waits on nothing."""
    waits: list[dict[str, Any]] = []
    for task in proc.tasks or []:
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
        "pid": proc.pid,
        "start": proc.start,
        "comm": proc.comm[:32],
        "state": proc.state,
        "tasks": None if proc.tasks is None else [task_record(task, known) for task in proc.tasks],
        "tasks_cut": 0,
        "waits_on": waits,
        **({} if cwd is None else {"cwd_mount": cwd}),
    }


def survivor_record(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The record's survivor from a reading entry: no pid and no start, the tasks in D first and cut to `TASKS_KEPT`
    (`tasks_cut` says how many were left out), the things it waits on cut to `WAITS_KEPT`. A process that could not be
    read has no `tasks`."""
    kept: dict[str, Any] = {"comm": entry["comm"], "state": entry["state"]}
    tasks = entry["tasks"]
    if tasks is not None:
        shown = sorted(tasks, key=lambda task: task["state"] != "D")[:TASKS_KEPT]
        kept.update(tasks=shown, tasks_cut=len(tasks) - len(shown), waits_on=entry["waits_on"][:WAITS_KEPT])
    if "cwd_mount" in entry:
        kept["cwd_mount"] = entry["cwd_mount"]
    return kept


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
