# Provenance: new
"""The federation's link epoch: a count of the heals and link failures a run's admission compares with its check (O35).

`link-epoch.json`, beside the fault journal, holds a count every process on the federation shares, raised:

- before any heal's first action, the fault gate's (`fault_gate_heal`) and a restore's (`fault_restore`), and before
  the heal's own line, so a heal that cannot raise it does not begin;
- whenever the fault gate's own link check, in any call, finds a pair failing (`fault_gate.FaultGate`).

A run's pre-run check notes the record's `id` and count (`noted_of`), and the run's admission compares them with the
record then (`admission_recheck`). The record also says which event raised it last (`heal` or `links_failed`) and when
(`utc`), for the words of a refusal only: what is compared is the count, never a clock. It is written whole or not at
all, a draft fsynced and renamed over it with the rename made durable, under the journal-wide lock, and only ever
upwards.

**A raise that cannot be written spoils the record** rather than leave the old count readable: it removes it or, when
the directory refuses that, empties it in place (an empty record is a missing one). An admission that finds no record
refuses the run, and the next check makes it anew (`ensure`) under a new `id`, so a count noted before never matches
one counted after. When even the overwrite fails (`RaiseLost`, `spoiled` False), the fault gate keeps `link-epoch.lock`
held (`hold_shared`) until it records the raise, so every admission's wait for it runs out and refuses the run.

**The finding and the raise are one step for an admission.** A link check of the gate holds `link-epoch.lock` shared
from before it reads the pairs until it has raised the count (`checking`), and an admission reads the record holding
it exclusively (`settled`), so it never reads between a check's finding a pair failing and its raise; it waits, within
its bound, for the checks under way to end. Nobody holding `link-epoch.lock` waits for the federation's locks.
"""

import contextlib
import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore_onedata.fault_journal import HeldLock, InjectionJournal, _fsync_directory, make_directories

LINK_EPOCH = "link-epoch.json"
LINK_LOCK = "link-epoch.lock"  # held shared by a link check of the gate, exclusively by an admission's re-check
GATE_NOTE = "expcore_onedata.link_epoch"  # the key of what a pre-run check notes for its try (`prerun`)
HEAL, LINKS_FAILED = "heal", "links_failed"  # what raised the count


class RaiseLost(OSError):
    """A raise of the count could not be written. `spoiled`: whether the old record was removed or emptied, so that no
    reader takes its count for the current one; when it was not, the caller must keep admissions out itself."""

    def __init__(self, error: OSError, spoiled: bool) -> None:
        super().__init__(error.errno, f"the federation's link epoch could not be written ({type(error).__name__})")
        self.spoiled = spoiled


def noted_of(record: Mapping[str, Any]) -> dict[str, Any]:
    """What a pre-run check notes of a record: its `id` and its count."""
    return {"id": record["id"], "epoch": record["epoch"]}


class LinkEpoch:
    """The federation's link epoch in the fault journal's directory (see the module docstring)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / LINK_EPOCH
        self._journal = InjectionJournal(self.directory)  # its journal-wide lock

    def read(self) -> dict[str, Any] | None:
        """The record (`id`, `epoch`, and `event` and `utc` once it was raised), or None when there is none: never
        made, or removed by a raise that could not be written. One that is not a record raises ValueError."""
        if not self.path.exists():
            return None
        with self._journal._section(fcntl.LOCK_SH):
            return self._unlocked()

    def ensure(self) -> dict[str, Any]:
        """The record, made with a new `id` and a count of 0 when there is none. A record that cannot be written
        raises OSError, and one that cannot be read ValueError."""
        found = self.read()
        if found is not None:
            return found
        make_directories(self.directory)
        with self._journal._section():
            found = self._unlocked()
            if found is None:
                found = {"id": uuid.uuid4().hex, "epoch": 0}
                self._replace(found)
        return found

    def raise_for(self, event: str) -> int:
        """Raise the count by one for `event` (`HEAL` or `LINKS_FAILED`), durably (a record made anew when there is
        none); return the new count. A record that cannot be written is spoiled and `RaiseLost` raised; one that cannot
        be read raises ValueError."""
        make_directories(self.directory)
        with self._journal._section():
            found = self._unlocked() or {"id": uuid.uuid4().hex, "epoch": 0}
            utc = datetime.now(UTC).isoformat(timespec="milliseconds")
            raised = {"id": found["id"], "epoch": int(found["epoch"]) + 1, "event": event, "utc": utc}
            self._replace(raised)
        return raised["epoch"]

    def _unlocked(self) -> dict[str, Any] | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not text.strip():  # emptied in place by a raise that could not be written: missing
            return None
        try:
            record = json.loads(text)
        except ValueError:
            raise ValueError("the federation's link epoch is not JSON") from None
        epoch = record.get("epoch") if isinstance(record, dict) else None
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0 or not isinstance(record.get("id"), str):
            raise ValueError("the federation's link epoch holds no id and count")
        return record

    def _replace(self, record: Mapping[str, Any]) -> None:
        """Under the section: the record, whole. When it cannot be written, the old one is spoiled (`_spoil`) so that no
        reader takes its count for the current one, and `RaiseLost` is raised, saying whether it was."""
        try:
            self._write(record)
        except OSError as error:
            raise RaiseLost(error, self._spoil()) from None

    def _spoil(self) -> bool:
        """Remove the record, or, when the directory refuses that, empty it in place (which needs only the file's own
        write permission); whether either was done."""
        try:
            self._remove()
            return True
        except OSError:
            pass
        try:
            self._truncate()
            return True
        except OSError:
            return False

    def _remove(self) -> None:
        self.path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # the removal is done; its durability is the directory's
            _fsync_directory(self.directory)

    def _truncate(self) -> None:
        with open(self.path, "r+b") as handle:
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())

    def _write(self, record: Mapping[str, Any]) -> None:
        draft = self.directory / f"{LINK_EPOCH}.draft"  # one writer at a time: the section is held
        try:
            with open(draft, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(draft, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                draft.unlink(missing_ok=True)
            raise
        _fsync_directory(self.directory)

    def hold_shared(self) -> HeldLock:
        """A shared hold of `link-epoch.lock` that lasts until it is closed: what the fault gate keeps while a raise it
        owes is recorded nowhere (`RaiseLost`, not `spoiled`), so that every admission's wait for the lock runs out."""
        descriptor = self._lock_file()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        except BaseException:
            os.close(descriptor)
            raise
        return HeldLock(descriptor)

    def _lock_file(self) -> int:
        make_directories(self.directory)
        return os.open(self.directory / LINK_LOCK, os.O_RDWR | os.O_CREAT, 0o600)

    @contextlib.contextmanager
    def checking(self) -> Iterator[None]:
        """Hold `link-epoch.lock` shared for a link check of the gate, from before it reads the pairs until it has
        raised the count, so no admission reads the record between the two. A blocking wait: an admission holds it
        exclusively only for two short reads."""
        descriptor = self._lock_file()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def settled(
        self, timeout_s: float | None, cancel: threading.Event | None = None, poll_s: float = 0.05
    ) -> Iterator[bool]:
        """Hold `link-epoch.lock` exclusively, waiting at most `timeout_s`, and until `cancel` is set, for every link
        check of the gate under way to end; yield whether it is held (False: the wait ran out, and nothing is held)."""
        descriptor = self._lock_file()
        try:
            deadline = None if timeout_s is None else time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    stopped = cancel is not None and cancel.is_set()
                    if stopped or (deadline is not None and time.monotonic() >= deadline):
                        yield False
                        return
                    time.sleep(poll_s)
            yield True
        finally:
            os.close(descriptor)
