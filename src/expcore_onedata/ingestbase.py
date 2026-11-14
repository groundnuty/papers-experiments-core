# Provenance: new
"""A progressive ingest's dataset baseline: removing planned files for a run's start state, writing them back after
it, and confirming each change on every provider, every file id recorded (O27). `ingest.py` drives it.

**What may be removed** (the data a study keeps is never lost): a file at a planned path is removed only when the
provisioner's change log says the ingest made it, or when its bytes read back are those of its staged source. Either
way its staged source is checked against the plan's hash first, in every removal, the ingest's own files included
(`remove`): the copy the baseline is restored from may be the last good one of the bytes, whatever made the file. A
damaged copy leaves the file in place and fails the restore of that file alone, an infrastructure failure, which is
recoverable, where the removal could lose data without a trace. Anything else at a planned path is refused, before
anything is sent. A "complete" file is written back from its staged source, checked first, by `Provisioner.upload`,
which resumes or rewrites only a file the ingest made. No directory is created or removed: a planned file's directory
must exist.

**Every removal is confirmed on every provider of the space** (`answered_absent` on consecutive polls, bounded by
`settle_timeout_s`), and a name removed earlier, by this run or a crashed one, is written again only once every
provider answers its old entry absent. 25.0 was measured to delete a directory asynchronously and to lose files
when a name is deleted and recreated at once (#29); the wait is applied to every removed file as a precaution.

**Every id change is recorded** (`Baseline.ids`) and journalled: a JSON-lines file kept across runs, each line pushed to
disk before the next step (a last line a crash cut short is cut off before the next is written), so the ids of a run
whose record was lost can still be mapped to files. An id event holds the file's label, what happened (`removed`,
`created`, `restored`), the phase, the old and the new id, the clock and the file's mtime and ctime as the data provider
gives them (None when it gives none; `attributes_ok` false when they could not be read).
"""

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.ingestplan import IngestPlan, PlannedFile
from expcore_onedata.provision import Entry, NotPlaced, Provisioner, ProvisionError
from expcore_onedata.rangemap import safe_path
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.streamed import Digest, Upload, chunks

FAILURES = (PlatformError, ProvisionError, consistency.WaitTimeout, KeyError, OSError)  # an ingest failure's causes
_PLAIN_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")
_HASH_BYTES = 1 << 20


def detail(error: BaseException) -> str:
    """A failure's text for a record or a message: the layer's own (which names nothing of the federation), or a fixed
    text where Python's would hold a path (a KeyError of `resolve`, an OSError of a staged source)."""
    if isinstance(error, KeyError):
        return "a planned path, or the directory it lies in, is not in the space"
    if isinstance(error, OSError):
        return "a staged source could not be read"
    return str(error)


def utc(epoch: float) -> str:
    """An epoch time as UTC, to the millisecond."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch * 1000) % 1000:03d}Z"


def plain(file_id: str) -> str:
    """A file id that is one plain path segment, or a PlatformError."""
    if not isinstance(file_id, str) or not _PLAIN_ID.fullmatch(file_id):
        raise PlatformError("a file id is not a plain path segment")
    return file_id


def _cut_torn_tail(handle: Any) -> None:
    """Cut off a last line without its newline, which a crash in the middle of an append leaves, and push the cut to
    disk; the handle is the journal opened for appending and reading, in bytes."""
    end = handle.seek(0, os.SEEK_END)
    if end == 0:
        return
    handle.seek(end - 1)
    if handle.read(1) == b"\n":
        return
    handle.seek(0)
    kept = handle.read().rfind(b"\n") + 1
    handle.truncate(kept)
    handle.flush()
    os.fsync(handle.fileno())


class RestoreFailed(ProvisionError):
    """Some planned files could not be brought back to their baseline; every other one was. `files` holds each file's
    result by label, a failed one as `{"ok": False, "error", "detail"}`."""

    def __init__(self, message: str, files: dict[str, dict[str, Any]]) -> None:
        super().__init__(message)
        self.files = files


class Baseline:
    """The planned files of one space: their removal for the start state and their restore, verified and recorded."""

    def __init__(self, provisioner: Provisioner, plan: IngestPlan, journal: Path, now: Callable[[], float]) -> None:
        self._p, self._plan, self._journal, self._now = provisioner, plan, journal, now
        self.holder = provisioner.settings.holder
        self.providers = list(provisioner.space.support.providers)  # where every removal is confirmed
        self.visible_on = list(plan.visible_on) if plan.visible_on is not None else list(self.providers)
        self.lock = threading.Lock()  # the schedule's thread and the caller's both record
        self.ids: list[dict[str, Any]] = []
        self.last_id: dict[str, str] = {}  # label -> the id its file had last, removed or not

    # -- the record ------------------------------------------------------------------------------------------------
    def stamp(self) -> dict[str, Any]:
        """The clock labels of a reading: the provisioner's (monotonic) clock and UTC."""
        return {"clock": round(self._p.clock(), 3), "utc": utc(self._now())}

    def journal(self, **fields: Any) -> None:
        """Append one line to the journal and push it to disk: an id change must outlive a crash. A last line a crash
        cut short is cut off first, so the new line is never glued to it (readers skip a torn last line, and a mark
        glued to one would be skipped with it)."""
        self._journal.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self._journal, "a+b") as handle:
            _cut_torn_tail(handle)
            line = json.dumps({"space": self._p.space.position, **fields}, sort_keys=True) + "\n"
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def read_journal(self) -> None:
        """Take each planned file's last id from the journal, a crashed run's included. A torn last line, which a
        crash can leave, is skipped; any other line that cannot be read fails closed."""
        if not self._journal.exists():
            return
        lines = self._journal.read_text(encoding="utf-8").splitlines()
        labels = {f.label for f in self._plan.files}
        for number, text in enumerate(lines, start=1):
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                if number == len(lines):
                    continue
                raise ProvisionError(f"{self._p.where}the ingest's journal has a line that cannot be read") from None
            if not isinstance(line, dict) or line.get("space") != self._p.space.position:
                continue
            last = line.get("new_id") or line.get("old_id") or line.get("file_id")
            if line.get("label") in labels and isinstance(last, str):
                self.last_id[line["label"]] = last

    def _times(self, file_id: str) -> dict[str, Any]:
        try:
            answer = self._p.client.get(f"/data/{plain(file_id)}", provider=self.holder, retry=self._p.retry)
        except PlatformError:
            return {"mtime": None, "ctime": None, "attributes_ok": False}
        shown = answer if isinstance(answer, dict) else {}
        picked = {k: shown.get(k) if isinstance(shown.get(k), int | float) else None for k in ("mtime", "ctime")}
        return {**picked, "attributes_ok": True}

    def record_id(self, label: str, event: str, phase: str, old: str | None, new: str | None, **extra: Any) -> None:
        """Record and journal one id change of a planned file."""
        entry = {"label": label, "event": event, "phase": phase, "old_id": old, "new_id": new, **extra}
        entry.update(self.stamp(), **(self._times(new) if new is not None else {}))
        with self.lock:
            self.ids.append(entry)
        self.journal(entry="id", **entry)
        if new is not None:
            self.last_id[label] = new

    # -- checks --------------------------------------------------------------------------------------------------------
    def check_source(self, planned: PlannedFile) -> None:
        """The staged copy the study keeps still has the plan's size and hash: nothing is written or removed
        otherwise."""
        hasher, size = hashlib.sha256(), 0
        for _, block in chunks(planned.source, _HASH_BYTES):
            hasher.update(block)
            size += len(block)
        if Digest(size, hasher.hexdigest()) != Digest(planned.size, planned.sha256):
            raise ProvisionError(f"{self._p.where}{planned.label}: its staged source no longer has the plan's hash")

    def check_sources(self) -> None:
        """Every planned file's staged copy is there, at the plan's size and SHA-256: checked whole before anything is
        removed, so no file goes while the copy of another that would follow it is damaged or gone."""
        for planned in self._plan.files:
            self.check_source(planned)

    def check_directory(self, planned: PlannedFile) -> None:
        """A planned file's directory exists: the ingest creates none, so the namespace stays the baseline's."""
        *directory, _ = safe_path(planned.path)
        if directory and self._p.resolve("/".join(directory)).kind != "DIR":
            raise ProvisionError(f"{self._p.where}{planned.label}: its directory is not a directory")

    def check_directories(self) -> None:
        """Every planned file's directory exists, so no arrival creates one."""
        for planned in self._plan.files:
            self.check_directory(planned)

    def entry(self, path: str) -> Entry | None:
        """The entry at a path on the data provider, or None when there is none."""
        try:
            return self._p.resolve(path)
        except KeyError:
            return None

    # -- removal -------------------------------------------------------------------------------------------------------
    def _absent_everywhere(self, file_id: str) -> bool:
        for provider in self.providers:
            try:
                self._p.client.get(f"/data/{plain(file_id)}", provider=provider, retry=ONE_ATTEMPT)
            except PlatformError as error:
                if answered_absent(error):
                    continue
                raise
            return False
        return True

    def await_absent(self, label: str, file_id: str) -> None:
        """Wait, bounded, until every provider of the space answers a removed file absent on consecutive polls."""
        settings = self._p.settings
        gone = consistency.converge(
            lambda: self._absent_everywhere(file_id),
            bool,
            timeout_s=settings.settle_timeout_s,
            poll_s=settings.poll_s,
            consecutive=settings.consecutive_polls,
            clock=self._p.clock,
            sleep=self._p.sleep,
        )
        if not gone.converged:
            raise ProvisionError(f"{self._p.where}{label}: a removed file still answered on a provider after the bound")

    def remove(self, planned: PlannedFile, entry: Entry, phase: str) -> None:
        """Delete a planned file's entry through the gate and wait until every provider answers it absent. Its staged
        copy is checked first, whoever made the entry, so a damaged copy leaves the file in place (a ProvisionError);
        then the intent is journalled."""
        self.check_source(planned)
        label = planned.label
        self.journal(entry="intent", label=label, action="remove", file_id=entry.file_id, phase=phase)
        try:
            self._p.mutate("DELETE", f"/data/{plain(entry.file_id)}", provider=self.holder)
        except PlatformError as error:
            if not answered_absent(error):
                raise
        self.await_absent(label, entry.file_id)
        self.record_id(label, "removed", phase, entry.file_id, None)

    def remove_for_start(self) -> dict[str, str]:
        """Remove every file whose baseline is complete, each confirmed absent everywhere; return their ids."""
        removed = {}
        for planned in self._plan.files:
            if planned.baseline == "complete":
                found = self._p.resolve(planned.path)
                self.remove(planned, found, "start")
                removed[planned.label] = found.file_id
        return removed

    # -- confirmation and restore ----------------------------------------------------------------------------------
    def confirm(self, file_id: str, size: int) -> dict[str, Any]:
        """The data provider's block map covers `[0, size)` and every provider that must see the file shows it at that
        size; return the seconds each of those providers took, by position."""
        if size > 0:
            placed = self._p.confirm_placed(self.holder, {plain(file_id): [(0, size)]})
            if not placed.placed:
                raise NotPlaced(f"{self._p.where}the data provider's block map does not cover the written bytes")
        waited = {}
        for provider in self.visible_on:
            if provider != self.holder:
                waited[self._p.client.provider_label(provider)] = consistency.wait_visible(
                    self._p.client,
                    provider,
                    plain(file_id),
                    size=size,
                    clock=self._p.clock,
                    sleep=self._p.sleep,
                    retry=self._p.retry,
                )
        return {"holder_covered": True, "visible_s": waited}

    def _made_by_ingest(self, found: Entry) -> bool:
        """The provisioner's change log says it created this file."""
        return found.file_id in {line["id"] for line in self._p.logged()}

    def _removable(self, planned: PlannedFile, found: Entry) -> bool:
        if self._made_by_ingest(found):
            return True
        if found.kind != "REG" or found.size != planned.size:
            return False
        return self._p.read_file(found.file_id, size=planned.size).sha256 == planned.sha256

    def restore_file(self, planned: PlannedFile, phase: str) -> dict[str, Any]:
        """Bring one file to its baseline and verify it; return what was done and the id it ends with. Nothing is
        removed or rewritten before the file's staged copy is checked, the ingest's own file included (`remove`,
        `_write_back`): a damaged copy fails this file's restore, which leaves the file in place."""
        before = self.entry(planned.path)
        if before is None and planned.label in self.last_id:  # a name removed earlier, perhaps by a crashed run
            self.await_absent(planned.label, self.last_id[planned.label])
        if planned.baseline == "complete":
            return self._write_back(planned, phase, before)
        if before is None:
            return {"action": "absent", "file_id": None}
        if not self._removable(planned, before):
            raise ProvisionError(
                f"{self._p.where}{planned.label}: a file at its path is neither the ingest's nor its staged source; "
                "nothing was removed"
            )
        self.remove(planned, before, phase)
        return {"action": "removed", "file_id": None}

    def upload(self, planned: PlannedFile, phase: str, old: str | None, *, stop: int | None = None) -> Upload:
        """Upload a planned file (up to `stop`) through the provisioner. When the upload fails after it made the file,
        the file it made is recorded as `created` before the failure is raised, so no id the ingest made goes
        unrecorded: the change log, the ingest's own, names each file the upload created at the planned path."""
        made_before = self._made_at(planned.path)
        try:
            return self._p.upload(planned.path, planned.source, stop=stop)
        except FAILURES:
            for new in self._made_at(planned.path):
                if new not in made_before:
                    self.record_id(planned.label, "created", phase, old, new, action="created")
                    old = new
            raise

    def _made_at(self, path: str) -> list[str]:
        """The ids of the regular files the provisioner's change log says it created at a path, oldest first."""
        return [line["id"] for line in self._p.logged() if line.get("kind") == "REG" and line.get("path") == path]

    def _write_back(self, planned: PlannedFile, phase: str, before: Entry | None) -> dict[str, Any]:
        """Write a "complete" file back from its staged source, checked first, into its existing directory. Its id is
        recorded as soon as the upload returns, before its hash is checked and its arrival confirmed."""
        self.check_source(planned)
        if before is None:
            self.check_directory(planned)  # the upload would create a missing one
        old = before.file_id if before is not None else self.last_id.get(planned.label)
        done = self.upload(planned, phase, old)
        self.record_id(planned.label, "restored", phase, old, done.file_id, action=done.action)
        if done.digest != Digest(planned.size, planned.sha256):
            raise ProvisionError(f"{self._p.where}{planned.label}: the restored file is not its staged source")
        self.confirm(done.file_id, planned.size)
        return {"action": done.action, "file_id": done.file_id}

    def restore_all(self, phase: str) -> dict[str, Any]:
        """Restore every planned file, in the plan's order, each on its own: a file that fails stops none of the others,
        so one bad staged copy never leaves the files after it missing. Return each file's result by label, or raise
        `RestoreFailed` after the loop, naming every file that failed and holding every result."""
        files: dict[str, dict[str, Any]] = {}
        for planned in self._plan.files:
            try:
                files[planned.label] = {"ok": True, **self.restore_file(planned, phase)}
            except FAILURES as error:
                files[planned.label] = {"ok": False, "error": type(error).__name__, "detail": detail(error)}
        failed = [label for label, done in files.items() if not done["ok"]]
        if failed:
            raise RestoreFailed(
                f"{self._p.where}not brought back to the baseline: {', '.join(failed)}, every other file was; "
                f"{failed[0]}: {files[failed[0]]['detail']}",
                files,
            )
        return files


__all__ = ["FAILURES", "Baseline", "RestoreFailed", "detail", "plain", "utc"]
