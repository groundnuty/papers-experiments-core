# Provenance: new
"""A progressive ingest's dataset baseline: removing planned files for a run's start state, writing them back after
it, and confirming each change on every provider, every file id recorded (O27). `ingest.py` drives it.

**What may be removed** (the data a study keeps is never lost): a file at a planned path is removed only when the
provisioner's change log says the ingest made it, or when its bytes read back are those of its staged source, which has
just been checked against the plan's hash. Anything else at a planned path is refused, before anything is sent. A
"complete" file is written back from its staged source by `Provisioner.upload`, which resumes or rewrites only a file
the ingest made. No directory is created or removed: a planned file's directory must exist.

**Every removal is confirmed on every provider of the space** (`answered_absent` on consecutive polls, bounded by
`settle_timeout_s`), and a name removed earlier, by this run or a crashed one, is written again only once every
provider answers its old entry absent. 25.0 was measured to delete a directory asynchronously and to lose files
when a name is deleted and recreated at once (#29); the wait is applied to every removed file as a precaution.

**Every id change is recorded** (`Baseline.ids`) and journalled: a JSON-lines file kept across runs, each line pushed to
disk before the next step, so the ids of a run whose record was lost can still be mapped to files. An id event holds
the file's label, what happened (`removed`, `created`, `restored`), the phase, the old and the new id, the clock and the
file's mtime and ctime as the data provider gives them (None when it gives none; `attributes_ok` false when they could
not be read).
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
from expcore_onedata.streamed import Digest, chunks

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
        """Append one line to the journal and push it to disk: an id change must outlive a crash."""
        self._journal.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self._journal, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"space": self._p.space.position, **fields}, sort_keys=True) + "\n")
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
            answer = self._p.client.get(f"/data/{plain(file_id)}", provider=self.holder)
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

    def check_directories(self) -> None:
        """Every planned file's directory exists: the ingest creates none, so the namespace stays the baseline's."""
        for planned in self._plan.files:
            *directory, _ = safe_path(planned.path)
            if directory and self._p.resolve("/".join(directory)).kind != "DIR":
                raise ProvisionError(f"{self._p.where}{planned.label}: its directory is not a directory")

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
                self._p.client.get(f"/data/{plain(file_id)}", provider=provider)
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

    def remove(self, label: str, entry: Entry, phase: str) -> None:
        """Delete a file through the gate, the intent journalled first, and wait until every provider answers it
        absent."""
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
                self.remove(planned.label, found, "start")
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
                    self._p.client, provider, plain(file_id), size=size, clock=self._p.clock, sleep=self._p.sleep
                )
        return {"holder_covered": True, "visible_s": waited}

    def _removable(self, planned: PlannedFile, found: Entry) -> bool:
        if found.file_id in {line["id"] for line in self._p.logged()}:
            return True
        if found.kind != "REG" or found.size != planned.size:
            return False
        return self._p.read_file(found.file_id, size=planned.size).sha256 == planned.sha256

    def restore_file(self, planned: PlannedFile, phase: str) -> dict[str, Any]:
        """Bring one file to its baseline and verify it; return what was done and the id it ends with."""
        before = self.entry(planned.path)
        if before is None and planned.label in self.last_id:  # a name removed earlier, perhaps by a crashed run
            self.await_absent(planned.label, self.last_id[planned.label])
        if planned.baseline == "complete":
            self.check_source(planned)
            done = self._p.upload(planned.path, planned.source)
            if done.digest != Digest(planned.size, planned.sha256):
                raise ProvisionError(f"{self._p.where}{planned.label}: the restored file is not its staged source")
            self.confirm(done.file_id, planned.size)
            old = before.file_id if before is not None else self.last_id.get(planned.label)
            self.record_id(planned.label, "restored", phase, old, done.file_id, action=done.action)
            return {"action": done.action, "file_id": done.file_id}
        if before is None:
            return {"action": "absent", "file_id": None}
        if not self._removable(planned, before):
            raise ProvisionError(
                f"{self._p.where}{planned.label}: a file at its path is neither the ingest's nor its staged source; "
                "nothing was removed"
            )
        self.remove(planned.label, before, phase)
        return {"action": "removed", "file_id": None}

    def restore_all(self, phase: str) -> dict[str, Any]:
        """Restore every planned file, in the plan's order."""
        return {f.label: self.restore_file(f, phase) for f in self._plan.files}


__all__ = ["FAILURES", "Baseline", "detail", "plain", "utc"]
