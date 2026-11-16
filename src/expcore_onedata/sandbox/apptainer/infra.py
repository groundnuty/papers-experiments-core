# Provenance: new
"""The state of a run's other infrastructure on an HPC node, in the shape of Docker's `infra_check` (O17, O56).

Docker's check compares each of the run's other containers with its state when it came up. On a node the run's other
infrastructure is processes of the driver's, read by role: the egress collector (`collector`), the gate of a REST run
(`gate`), and for a mount run the helper's oneclient with its FUSE mount (`mount`) and the mount guard's watcher
(`mount_watch`, `hostmount`). For each, `start` is its process (pid and start time) and `end` its state now:

* `running`: a process alive now ran throughout, since no process can come back under the same pid and start time;
  one that ended is a death, with its `exit_status`, and one whose state cannot be told is a death too, since unknown
  is not clean;
* `mount` (oneclient's role alone): None when the mount is at its mount point as the host mount made it (this uid's,
  read-only, without `allow_other`: `hostmount.fuse_state`), else what is wrong (`absent`, `not_ours`, `allow_other`,
  `writable`, or `unread` when the mount table cannot be read): any of them is a death of the mount, whatever
  oneclient's state.

A process the harness stops itself (the collection stops the gate and the collector, the guard's window closes with its
watcher, and the teardown stops everything) is judged by its state just before that stop, which `stopped_by_harness`
marks, as Docker's check does: so the check reads the same before the collection, after it and after the stop. The
harness notes that state before it stops anything, and the check reads a component's live state first and looks for
the note after, so a stop that lands while the check reads is found by its note, never taken for a death.

No per-process limit, OOM counter or kernel line is kept for a process of the driver's, so every death's cause is
`undetermined` and its category `infrastructure`, as the collection always recorded it (`kinds.death_error` then makes
the run `unattributed_kill`). The guard's watcher is the guard's own evidence too: its death also leaves the guard's log
without its end line, which the mount guard reads as unknown, never clean (O60).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from expcore_onedata.sandbox import infradeath as ID
from expcore_onedata.sandbox.apptainer.node import NodeOps, Spawned

if TYPE_CHECKING:
    from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

HELPERS = ("collector", "gate")  # the sandbox's own helpers, in the order they are listed
BASIS = (
    "a process of the driver's on the node: no per-process limit, OOM counter or kernel line names how it ended, so "
    "nothing attributes it"
)
NO_STATE = "the mount helper gives no state of its own"


def process_state(node: NodeOps, process: Spawned) -> dict[str, Any]:
    """A process's state now: `running` (None when it cannot be told) and, once it has ended, its `exit_status`."""
    running = node.alive(process.pid, process.start)
    return {"running": running, "exit_status": node.exit_status(process) if running is False else None}


def problems(role: str, condition: str, end: Mapping[str, Any] | None) -> list[str]:
    """What is wrong with a role's state, each line naming the role and the condition; [] when it ran throughout."""
    who = f"{role} (condition {condition})"
    if end is None:
        return [f"{who}: {NO_STATE}, so nothing shows it ran throughout"]
    found = []
    if end.get("running") is None:
        found.append(f"{who}: its state could not be read")
    elif end["running"] is False:
        status = end.get("exit_status")
        found.append(f"{who}: its process ended during the run (exit status {'unknown' if status is None else status})")
    if "mount" in end and end["mount"] is not None:
        state = end["mount"]
        found.append(
            f"{who}: its mount table could not be read"
            if state == "unread"
            else f"{who}: its mount is not at the mount point as the host mount made it ({state})"
        )
    return found


class InfraCheck:
    """The check of one sandbox (the module's docstring); `stopping` notes a helper's state before the harness stops
    it."""

    def __init__(self, box: ApptainerSandbox) -> None:
        self.box = box
        self._before: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def stopping(self, role: str) -> None:
        """Note, just before the harness stops one of the sandbox's helpers, its state then: it stands as its last
        state of the run, so the stop is never a death. A death before this moment shows in that state."""
        helper = self.box._helpers.get(role)
        if helper is None:
            return
        with self._lock:
            if role in self._before:
                return
        state = process_state(self.box.node, helper.process)
        with self._lock:
            self._before.setdefault(role, state)

    def _roles(self) -> dict[str, dict[str, Any]]:
        box, found = self.box, {}
        for role in HELPERS:
            helper = box._helpers.get(role)
            if helper is None:
                continue
            now = process_state(box.node, helper.process)  # first: a note taken after it is found below
            with self._lock:
                before = self._before.get(role)
            entry = {"start": helper.process.record(), "end": before or now}
            if before is not None:
                entry["stopped_by_harness"] = True
            found[role] = entry
        if box._mount is not None:
            read = getattr(box._mount, "infra_state", None)
            found.update(read() if read is not None else {"mount": {"start": None, "end": None}})
        return found

    def check(self) -> dict[str, Any]:
        """`infra_check()`'s answer: each role's state with its problems, the deaths, and the infrastructure's
        problems (`api.InfraReadings`)."""
        condition = self.box.spec.mode.value
        containers, deaths = {}, []
        for role, entry in self._roles().items():
            found = problems(role, condition, entry["end"])
            containers[role] = {**entry, "problems": found}
            if found:
                deaths.append({"role": role, "condition": condition, "cause": ID.UNDETERMINED,
                               "category": "infrastructure", "basis": BASIS, "problems": found})  # fmt: skip
        infra = [death for death in deaths if death["category"] == "infrastructure"]
        return {"containers": containers, "deaths": deaths, "death": [p for d in infra for p in d["problems"]]}


__all__ = ["InfraCheck", "problems", "process_state"]
