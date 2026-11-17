# Provenance: new
"""The collection of a run of the Apptainer backend: its outputs, the gate's log and count, the egress log (O56).

In order: every process of the run is killed and shown gone (none should be: each cell's end showed its own gone), so
nothing changes `/work` while it is copied (there is no freezer here, `facts.py`); the state of the run's other
infrastructure is read while it runs (`infra_check`, `apptainer.infra`), so a gate, a collector, a mount's oneclient or
its mount, or the guard's watcher that ended during the run is a death of infrastructure; the named outputs are copied
as on Docker (`copyout.copy_tree`: never a link, a special file, a hard link out of the tree, another file system or
more than the work disk holds; what the agent made so is refused for the scorers, the harness's own failure raises
`InfraError` of kind `output_collection`), out of the run's ext3 image by the copy helper in execs of it with
`work_disk = "image"`, every refusal the same (`workdisk.collect`, O71); the read-only inputs are hashed again; the gate
is stopped (SIGTERM, so it writes its close lines) and the bytes it served counted once (O22), its log copied; the
collector is stopped (it writes its end line) and its log read once, kept for the egress instrument and copied. Every
file of evidence the collection writes is scanned for the data-path token: one that holds it is removed and the
collection fails, since a secret is never evidence (O05). A mount run's guard closes its window here and its log is
copied too (O60, `hostmount`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from expcore_onedata.fault_drain import GATE
from expcore_onedata.sandbox import copyout, gatelog, inputs, kinds
from expcore_onedata.sandbox.api import DataPathMode, SandboxError
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import helpers, runs, workdisk
from expcore_onedata.sandbox.hostops import describe_error
from expcore_onedata.sandbox.sandbox import TEARDOWN_TROUBLE

if TYPE_CHECKING:
    from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

log = logging.getLogger(__name__)

EGRESS_COPY = "egress.jsonl"
MOUNT_WATCH_COPY = "mount-watch.jsonl"
GATE_COPY = "gate-requests.jsonl"


class Collection:
    """The collection of one sandbox (the module's docstring); `egress_bytes` is the collector's log once read."""

    def __init__(self, box: ApptainerSandbox) -> None:
        self.box = box
        self.egress_bytes: bytes | None = None

    def run(self, dest: Path, outputs: Sequence[str]) -> dict[str, Any]:
        box = self.box
        for rel in outputs:
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise SandboxError("an output to collect must be a relative path inside the working directory")
        state = box._cells.quiesce("before collection")
        if not box._cells.quiet(state):
            log.error("processes left before collection: %s", state)
            raise SandboxError("the agent's processes could not be shown stopped before collection")
        dest.mkdir(parents=True, exist_ok=True)
        box.note_evidence(dest)
        deaths = self.deaths()
        result: dict[str, Any] = {"outputs": self._outputs(dest, outputs), "deaths": deaths}
        if "refused_output" in box._meta:
            result["refused_output"] = box._meta["refused_output"]
        self._inputs_unchanged()
        written = []
        if box.spec.mode is DataPathMode.REST:
            self._stop_gate()
            written.append(self._copy_gate_log(dest))
            result["gate_log"] = GATE_COPY
        written.append(self._egress(dest))
        result["egress_log"] = EGRESS_COPY if self.egress_bytes is not None else None
        if box.spec.mode is DataPathMode.MOUNT:
            written.append(self._mount_watch(dest))
            result["mount_watch_log"] = MOUNT_WATCH_COPY if written[-1] is not None else None
        (dest / "metadata.json").write_text(json.dumps(box.metadata(), indent=1, default=str), encoding="utf-8")
        written.append(dest / "metadata.json")
        self._scan([path for path in written if path is not None])
        return result

    def deaths(self) -> list[dict[str, Any]]:
        """Each of the run's other processes that ended during the run, or whose state cannot be read (the gate, the
        collector, a mount's oneclient, its mount and the guard's watcher): infrastructure, never the agent's. The check
        is kept in the metadata as Docker's collection keeps it (`infra_check`, `container_deaths`, `infra_death`); one
        the harness stopped itself, in an earlier collection say, is judged by its state just before."""
        box = self.box
        check = box.infra_check()
        box.record(infra_check=check, container_deaths=check["deaths"], infra_death=check["death"])
        return check["deaths"]

    def _outputs(self, dest: Path, outputs: Sequence[str]) -> dict[str, bool]:
        box = self.box
        try:
            if box.work_image:
                copied = self._image_outputs(dest, outputs)
            else:
                copies = [(rel, dest / "work" / rel) for rel in outputs]
                copied = copyout.copy_tree(
                    box.node, box.dir / "work", copies, max_bytes=box.s.limits.work_disk_bytes, restore_locked=True
                )
        except copyout.UnsafeOutput as unsafe:  # the agent's own doing: for the scorers, never a set-aside
            box._meta["refused_output"] = copyout.record(unsafe.refused)
            return dict.fromkeys(outputs, False)
        except SandboxError as failed:
            raise kinds.collection_error("the run's outputs", str(failed)) from None
        return {rel: copied[rel] is not None for rel in outputs}

    def _image_outputs(self, dest: Path, outputs: Sequence[str]) -> dict[str, dict[str, int] | None]:
        """The named outputs out of the run's image (`workdisk.collect`): the copy helper in one-shot execs of the image
        that bind its `/work` and the run's staging directory and carry a tag of their own, never the run's, which the
        watch would kill them by (`workdisk.COPY_TAG`), each after the image is checked (O13); the staging directory is
        removed whatever happens."""
        box = self.box
        staging = box.dir / "export"
        runs.remove_tree(staging)
        staging.mkdir(mode=0o700)
        binds = [box.work_bind(), AV.Bind(staging, workdisk.EXPORT, True)]
        host = workdisk.ImageCopyHost(
            box.node, python=box.app.python, before_exec=box.image_unchanged, image=workdisk.image_path(box.dir),
            staging=staging,
            argv_of=lambda command: AV.exec_argv(box.app.apptainer, box.app.image.path, binds,
                                                 workdisk.copy_env(box.sid), command),
        )  # fmt: skip
        try:
            return workdisk.collect(host, staging, dest / "work", outputs, max_bytes=box.s.limits.work_disk_bytes)
        finally:
            _remove(staging)

    def prove_image_copy(self) -> None:
        """O71, at the start: the file the work disk's probe left (`workdisk.COPY_PROBE`) comes out of the image byte
        for byte through the collection's own path (`_image_outputs`), or `SandboxError`: a run whose outputs could not
        be collected is never started."""
        box = self.box
        dest = box.dir / "copy-probe"
        try:
            copied = self._image_outputs(dest, [workdisk.COPY_PROBE])
            same = copied[workdisk.COPY_PROBE] is not None and (
                (dest / "work" / workdisk.COPY_PROBE).read_bytes() == workdisk.COPY_MARK
            )
        except (SandboxError, OSError) as error:
            log.error("the start's copy out of the work disk failed: %s", describe_error(error))
            same = False
        finally:
            _remove(dest)
        if not same:
            raise SandboxError("a file could not be copied out of the work disk's image")

    def _inputs_unchanged(self) -> None:
        box = self.box
        for n, ((copy, _), found) in enumerate(zip(box._input_copies, box.read_only_inputs, strict=True)):
            try:
                same = inputs.digest(copy)["sha256"] == found["sha256"]
            except (OSError, inputs.InputUnsafe):
                same = False
            if not same:
                raise kinds.collection_error(
                    "the read-only inputs", f"input {n}'s copy no longer has its recorded hash"
                )

    def _stop_gate(self) -> None:
        """SIGTERM the gate so it writes its close lines, then count the bytes it served, once (O22)."""
        box = self.box
        if "gate_stop" in box._meta:
            return
        note: dict[str, Any] = {"signalled": False}
        gate = box._helpers.get("gate")
        try:
            box.infra.stopping("gate")  # the harness's own stop: its state now stands as its last of the run
            stopped = helpers.stop(box.node, gate, sleep=box._sleep, clock=box._clock)
            note["signalled"] = stopped["term"] is True
            if stopped["ended_on_term"] is True:
                note["exit_code"] = stopped["exit_status"]
            said = helpers.output(gate)
            note["output_read"] = True
            note["log_write_failed"] = "GATE LOG FAILED" in said
            note["teardown_incomplete"] = any(mark in said for mark in TEARDOWN_TROUBLE)
            box.note_end(GATE)
        except Exception as error:
            note["error"] = type(error).__name__
        box._meta["gate_stop"] = note
        lines, mark = box.gate_log_lines(), box._meta.get("run_start")
        box._meta["served_bytes"] = gatelog.served(lines, note, box.s.served_caps, run_start=mark)
        if box.spec.node_names:  # O73: the hits of the gate's scan for the nodes' names, from the same stopped log
            box._meta["rest_node_name_hits"] = gatelog.node_name_hits(lines, note)
        box._meta["rest_request_durations"] = gatelog.request_durations(lines, note)  # O73: how long its requests took

    def _copy_gate_log(self, dest: Path) -> Path:
        box = self.box
        target = dest / GATE_COPY
        try:
            if not copyout.copy_tree(box.node, box.dir / "gate", [("requests.jsonl", target)], max_bytes=None)[
                "requests.jsonl"
            ]:
                raise SandboxError("copying a file out of the sandbox failed: it is missing")
        except SandboxError as failed:
            raise kinds.collection_error("the gate log", str(failed)) from None
        return target

    def _egress(self, dest: Path) -> Path | None:
        """Stop the collector so it writes its end line, read its log once and copy it. A log that cannot be read is
        None, which the instrument reads as unknown, never as a run that connected nowhere."""
        box = self.box
        collector = box._helpers.get("collector")
        if "collector_stop" not in box._meta and collector is not None:
            box.infra.stopping("collector")  # the harness's own stop, as the gate's
            try:
                box._meta["collector_stop"] = helpers.stop(box.node, collector, sleep=box._sleep, clock=box._clock)
            except SandboxError as error:
                box._meta["collector_stop"] = {"error": str(error)}
        try:
            self.egress_bytes = (box.dir / "egress" / "egress.jsonl").read_bytes()
        except OSError:
            self.egress_bytes = None
            return None
        target = dest / EGRESS_COPY
        target.write_bytes(self.egress_bytes)
        return target

    def _mount_watch(self, dest: Path) -> Path | None:
        """Close the mount guard's window, now that every process of the run is shown gone, and copy its log (O60): the
        guard's verdict is then in the metadata this collection writes. A helper without a guard has none."""
        found = self.box.mount_watch_log()
        if found is None:
            return None
        target = dest / MOUNT_WATCH_COPY
        target.write_bytes(found)
        return target

    def _scan(self, paths: Sequence[Path]) -> None:
        """A file of evidence that holds the token is removed, and the collection fails (O05)."""
        token = self.box.spec.token.reveal().encode() if self.box.spec.token is not None else b""
        if not token:
            return
        held = [path for path in paths if path.exists() and token in path.read_bytes()]
        for path in held:
            path.unlink()
            if path.name == EGRESS_COPY:
                self.egress_bytes = None
        if held:
            raise kinds.collection_error("the run's evidence", f"{len(held)} file(s) held the data-path token")


def _remove(path: Path) -> None:
    """Remove a directory of the collection's own; one that stays is logged and left to the teardown, which removes the
    run's directory and fails if it cannot."""
    try:
        runs.remove_tree(path)
    except OSError as error:
        log.error("a directory of the collection could not be removed: %s", describe_error(error))


__all__ = ["Collection"]
