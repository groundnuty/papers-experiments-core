# Provenance: new
"""Whose block it is: the verdict on the survivors of a cell's deadline (O97).

A process that outlives its kill at a cell's cap is waiting in the kernel on a FUSE request. When the study's own
manipulation holds a data provider down, a read of a file only that provider holds waits for as long as the outage
lasts, and that is the agent's outcome: the study measures what an agent does against an unavailable provider. A wait
the harness cannot account for, a wedge between two providers that are both up, a block on the agent's own work
directory, a process that is not waiting in the kernel at all, is not, and stays infrastructure. This module is the one
place that tells the two apart: a pure function of what the survivors were read to wait on (`stuck`'s reading, taken at
the `survived` step before the abort ends their wait), the providers the study holds down at that moment, and the
holder of each space.

**The verdict is `agent` only if all of these hold:** the set held down now is not empty; there is at least one
survivor, and every survivor was read whole; every process the deadline listed has only tasks in uninterruptible sleep
(D), at least one; every task's blocking object is known; each such object is on the data mount; and each lies in a
space whose holder is in the held-down set. **Anything else is `infrastructure`, with the first reason that failed**:
`no_injection_in_effect`, `blocking_object_unknown` (no reading, a reading cut short, a task whose object could not be
named, a verdict the caller made no sense of), `not_in_D`, `not_data_mount`, `space_not_held_down`. It fails closed: an
empty list, a reading that is not a reading and a flag that says a reading was cut each end in `infrastructure`, never
in the agent through an `all()` over nothing.

**What it reads:** the `reading` of `deadline.Tracker.diagnose`'s answer, and nothing else of it (the record keeps the
capped `survivors`, so a cap on what is shown never makes a verdict). The escalation has checked already that it holds
an entry for every process it listed (`deadline._Run.covers`), each with its `pid` and `start`:

    {"reading": [{"pid": ..., "start": ..., "comm": ..., "state": "D" | "Z" | ...,
                  "tasks": [{"state": "D", "wchan": ..., "syscall": {"nr": 0, "fd": 3} | {"nr": -1} | ...,
                             "blocking": {"via": "fd" | "path" | "holdings", "cut": False,
                                          "objects": [{"mount": "data" | "work" | ..., "space": name | None,
                                                       "path": rest | None}, ...]} | None}, ...],
                  "tasks_cut": 0, ...}, ...]}

`blocking` is the reader's resolution of what one task waits on, and `syscall.nr` (x86_64) is what it based it on:
- `via: "fd"` is the file of the fd a read-family call took (`FD_CALLS`), and `via: "path"` the object a path call named
  (`PATH_CALLS`): each only for a call of its list, so a reader that resolved an fd for another call is not believed.
- `via: "holdings"` is every FUSE object the process holds (its fds, its mapped files, its cwd), and is sound only for a
  task that is blocked OUTSIDE any syscall (a page fault on a mapping), which `/proc/<pid>/syscall` reads as `nr: -1`:
  a task blocked in a call the lists do not name may wait on a path it does not hold (`access`, `readlink`, `chdir`,
  `statfs`, `getxattr`, `execve`), so it is `blocking_object_unknown`, never attributed by what the process holds.
A cell's process that has a zombie leader and a task in D is read by its tasks.

The function takes the held-down set as a callable and reads it when the verdict is asked for, at the `survived` step:
never a set taken when the sandbox started.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from expcore_onedata.sandbox import deadline as DL

BLOCKED_ON_HOLDER = "blocked_on_held_down_holder"
NO_INJECTION = "no_injection_in_effect"
SPACE_NOT_HELD_DOWN = "space_not_held_down"
NOT_DATA_MOUNT = "not_data_mount"
UNKNOWN = DL.UNKNOWN_BLOCK  # "blocking_object_unknown"
NOT_IN_D = "not_in_D"
REASONS = (BLOCKED_ON_HOLDER, NO_INJECTION, SPACE_NOT_HELD_DOWN, NOT_DATA_MOUNT, UNKNOWN, NOT_IN_D)
DATA = "data"  # the `mount` of an object on the run's data mount
VIAS = ("fd", "path", "holdings")
# x86_64 syscall numbers a reader may resolve an object for: the fd is the first argument (`sendfile`'s the second), or
# the object is a path argument; `-1` is a task blocked outside any syscall, the one case the holdings rule is sound for
FD_CALLS = frozenset({0, 17, 19, 295, 327, 217, 5, 74, 326, 275, 16, 40})
PATH_CALLS = frozenset({257, 262, 332, 2, 4, 6, 269, 267})
OUTSIDE_A_SYSCALL = -1
CALLS_OF = {"fd": FD_CALLS, "path": PATH_CALLS, "holdings": frozenset({OUTSIDE_A_SYSCALL})}


def _verdict(why: str, *, target: str | None = None, holder: str | None = None) -> dict[str, Any]:
    agent = why == BLOCKED_ON_HOLDER
    return {"verdict": DL.AGENT if agent else DL.INFRASTRUCTURE, "why": why, "target": target, "space_holder": holder}


def _objects(task: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """The FUSE objects one task waits on, or None when they are not known: no `blocking`, an unknown `via`, a
    `syscall` that is not one `via` is sound for (`CALLS_OF`: the holdings rule only for a task outside any syscall),
    no object, an object that is no mapping, or a list the reader cut."""
    blocking = task.get("blocking")
    if not isinstance(blocking, Mapping) or blocking.get("via") not in VIAS or blocking.get("cut"):
        return None
    syscall = task.get("syscall")
    nr = syscall.get("nr") if isinstance(syscall, Mapping) else None
    if isinstance(nr, bool) or not isinstance(nr, int) or nr not in CALLS_OF[blocking["via"]]:
        return None
    objects = blocking.get("objects")
    if not isinstance(objects, list) or not objects or not all(isinstance(o, Mapping) for o in objects):
        return None
    return objects


def _survivors(diagnostics: Mapping[str, Any] | None) -> list[Mapping[str, Any]] | None:
    """The processes the verdict is made on, the diagnosis's `reading`, or None when there is no whole reading of them:
    none given, an empty one, or one that is no list of mappings."""
    if not isinstance(diagnostics, Mapping):
        return None
    reading = diagnostics.get("reading")
    if not isinstance(reading, list) or not reading:
        return None
    return reading if all(isinstance(r, Mapping) for r in reading) else None


def attribute(
    diagnostics: Mapping[str, Any] | None, held_down: Collection[str], holders: Mapping[str, str]
) -> dict[str, Any]:
    """The verdict on the survivors read as `diagnostics` (the module's docstring), given the providers held down now
    (`held_down`, their labels) and the holder of each space by the name the mount shows it (`holders`):
    `{"verdict": "agent" | "infrastructure", "why": ..., "target": ..., "space_holder": ...}`. `target` is the held-down
    label the survivors waited on, or for another verdict the one provider held down, when it is one; `space_holder`
    is the holder of the space that decided it, None when it names none. Pure: never raises for what a reading holds."""
    held = frozenset(held_down)
    sole = next(iter(held)) if len(held) == 1 else None
    if not held:
        return _verdict(NO_INJECTION)
    survivors = _survivors(diagnostics)
    if survivors is None:
        return _verdict(UNKNOWN, target=sole)
    matched: set[str] = set()
    for survivor in survivors:
        tasks = survivor.get("tasks")
        if not isinstance(tasks, list) or survivor.get("tasks_cut") or not all(isinstance(t, Mapping) for t in tasks):
            return _verdict(UNKNOWN, target=sole)
        live = [t for t in tasks if t.get("state") != "Z"]  # a zombie waits on nothing: its leader is read by its tasks
        if not live or any(t.get("state") != "D" for t in live):
            return _verdict(NOT_IN_D, target=sole)
        for task in live:
            objects = _objects(task)
            if objects is None:
                return _verdict(UNKNOWN, target=sole)
            for found in objects:
                if found.get("mount") != DATA:
                    return _verdict(NOT_DATA_MOUNT, target=sole)
                space = found.get("space")
                holder = holders.get(space) if isinstance(space, str) else None
                if holder not in held:
                    return _verdict(SPACE_NOT_HELD_DOWN, target=sole, holder=holder)
                matched.add(holder)
    holder = min(matched)  # every object was in a held-down space, and there was at least one: one label, always
    return _verdict(BLOCKED_ON_HOLDER, target=holder, holder=holder)


def attributer(
    held_down: Callable[[], Collection[str]], holders: Mapping[str, str]
) -> Callable[[dict[str, Any] | None], dict[str, Any]]:
    """`deadline.Escalation.attribute` for a run: `held_down()` is called each time a verdict is asked for, at the
    `survived` step, so the set is the one in effect then; `holders` maps each space's name, as the mount shows it, to
    its data provider's label."""
    return lambda diagnostics: attribute(diagnostics, held_down(), holders)


__all__ = [
    "BLOCKED_ON_HOLDER",
    "CALLS_OF",
    "FD_CALLS",
    "NOT_DATA_MOUNT",
    "NOT_IN_D",
    "NO_INJECTION",
    "OUTSIDE_A_SYSCALL",
    "PATH_CALLS",
    "REASONS",
    "SPACE_NOT_HELD_DOWN",
    "UNKNOWN",
    "attribute",
    "attributer",
]
