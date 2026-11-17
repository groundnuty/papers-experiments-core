# Provenance: new
"""The ordered, verified teardown of a run of the Apptainer backend (O56, O15, O05).

On every path that ends a sandbox, a failed start's included, in this order, each step run whatever an earlier one
raised: the run's file of the token (first: a secret is never evidence, and a later step that hangs must find none);
every process of the run's cells, killed and shown gone; the watch; the mount's helper (hook c); the gate and the
collector, each shown gone (`helpers.stop`); with a work disk image (O71), every process whose command line names the
image, killed and shown gone (`workdisk.end_holders`); then the run's directory, removed and shown gone, or, when the
run is kept as evidence (a failed collection, a deadline that ended the run), moved to `work_root/_kept_evidence` once
its token file and its copy of the CA bundle are gone, and its image too, which cannot be shown free of the token. The
run's record (`runs.py`) is dropped last, and only when all of it is shown gone: a teardown that is not complete raises
`SandboxError` naming each step that failed, and leaves the record for a next try, the reaper and the probe.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from expcore_onedata.fault_drain import GATE, MOUNT
from expcore_onedata.sandbox import termination as TERM
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import helpers, runs, workdisk
from expcore_onedata.sandbox.hostops import describe_error

if TYPE_CHECKING:
    from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

log = logging.getLogger(__name__)

KEPT = "_kept_evidence"


class Teardown:
    """The teardown of one sandbox (the module's docstring); `left` is what a stop under way has not shown gone."""

    def __init__(self, box: ApptainerSandbox) -> None:
        self.box = box
        self.step: str | None = None
        self.gone: set[str] = set()

    def left(self) -> dict[str, Any]:
        """What a stop under way has not yet shown removed (O42): its step, the helpers and the run's directory."""
        box = self.box
        return {
            "step": self.step,
            "helpers": sum(1 for role in box._helpers if role not in self.gone),
            "run_directory": os.path.lexists(box.dir),
        }

    def _do(self, errors: list[str], what: str, fn: Callable[[], Any]) -> bool:
        try:
            fn()
        except SandboxError as error:
            log.error("teardown step %s failed: %s", what, describe_error(error))
            errors.append(str(error))
            return False
        except Exception as error:
            log.error("teardown step %s failed: %s", what, describe_error(error))
            errors.append(f"a teardown step failed ({what}: {type(error).__name__})")
            return False
        return True

    def run(self, force: bool) -> None:
        box = self.box
        if box._stopped and not force:
            return
        if box._cells is not None and box._cells.ended is not None:
            box.keep_evidence = True  # a run a deadline ended is set aside as infrastructure: its evidence is kept
        with box._created_lock:
            created = box._created
        errors: list[str] = []
        complete = False
        try:
            self.step = "token files"
            self._do(errors, self.step, box._remove_token_files)
            self.step = "cells"
            self._do(errors, self.step, self._cells)
            if box._watch is not None:
                box._watch.stop()
            if box._mount is not None:
                self.step = "mount"
                if self._do(errors, self.step, box._mount.stop):
                    box.note_end(MOUNT)
            self.step = "helpers"
            for role in ("gate", "collector"):
                if role in box._helpers and role not in self.gone and self._do(errors, role, self._helper(role)):
                    self.gone.add(role)
                    if role == "gate":
                        box.note_end(GATE)
            if box.work_image:
                self.step = "work disk"
                self._do(errors, self.step, self._work_disk)
            if not errors:  # a helper or a cell not shown gone may still write there: the directory stays
                self.step = "run directory"
                self._do(errors, self.step, self._directory)
            complete = not errors
            if complete:  # a session never started is nothing left connected
                for kind in box.session_kinds():
                    box.note_end(kind)
        finally:
            with box._created_lock:
                box._stopped = complete and box._created == created
            if box._stopped:
                runs.drop(box.s.work_root, box.sid)
                TERM.unregister(box)
        if errors:
            raise SandboxError("sandbox cleanup incomplete: " + "; ".join(errors))

    def _cells(self) -> None:
        cells = self.box._cells
        if cells is None:
            return
        state = cells.quiesce("teardown")
        # stricter than the collection's `quiet`: a process in D may never leave the kernel (a dead mount), and no
        # container's removal follows here to show it gone, so only a zombie, which is dead, may be left
        if state["killed"] is None or any(p["state"] != "Z" for p in state["left"]):
            raise SandboxError("a process of the run's cells could not be shown gone")

    def _helper(self, role: str) -> Callable[[], None]:
        def stop() -> None:
            box = self.box
            helper = box._helpers[role]
            if box.node.alive(helper.process.pid, helper.process.start) is False:
                return  # stopped by the collection, or ended by itself: shown gone
            helpers.stop(box.node, helper, sleep=box._sleep, clock=box._clock)

        return stop

    def _work_disk(self) -> None:
        box = self.box
        if os.path.lexists(workdisk.image_path(box.dir)):
            workdisk.end_holders(box.node, workdisk.image_path(box.dir), box.sid, sleep=box._sleep, clock=box._clock)

    def _directory(self) -> None:
        box = self.box
        if not os.path.lexists(box.dir):
            return
        if not box.keep_evidence:
            runs.remove_tree(box.dir)
            if os.path.lexists(box.dir):
                raise SandboxError("the run directory still exists")
            return
        if box.token_files_left():  # a secret is never evidence: the run stays where it is
            raise SandboxError("the run directory is left where it is: a copy of the data-path token is in it")
        if box._ca_copy is not None:
            box._ca_copy.unlink(missing_ok=True)  # configuration, not evidence
        token = box.spec.token.reveal().encode() if box.spec.token is not None else b""
        image = workdisk.image_path(box.dir)
        dropped = bool(token) and os.path.lexists(image)  # its files' blocks need not be contiguous: never scanned
        if dropped:
            image.unlink()
        removed = scrub(box.dir, token)
        kept = box.s.work_root / KEPT
        kept.mkdir(mode=0o700, exist_ok=True)
        os.rename(box.dir, kept / box.sid)
        with box._meta_lock:
            box._meta["evidence_kept"] = {"under": "sandbox.work_root", "directory_id": box.sid.rsplit("-", 1)[-1],
                                          "relocated": True, "files_holding_the_token_removed": removed,
                                          **({"work_disk_image_dropped": True} if dropped else {})}  # fmt: skip


def scrub(root: Path, token: bytes) -> int:
    """Remove every regular file under `root` that holds `token` (never following a link): a secret is never
    evidence (O05). How many were removed; a file that cannot be read is removed too, since it cannot be shown clean."""
    if not token:
        return 0
    removed = 0
    for base, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(base, name)
            if path.is_symlink() or not path.is_file():
                continue
            try:
                holds = token in path.read_bytes()
            except OSError:
                holds = True
            if holds:
                path.unlink()
                removed += 1
    return removed


__all__ = ["Teardown", "scrub"]
