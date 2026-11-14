# Provenance: new
"""The fault injector's journal: every injection written down durably before it happens, and the targets' locks (O11).

A line is appended, flushed and fsynced before the call returns, and every directory and file the journal creates is
made durable in its parent (the journal's directory and its missing parents, by `try_lock` or by `append`, whichever
comes first), so neither a crash nor a power cut after `append` can lose the line. The injector appends `injecting`
before it stops a service, `stopping` right before the stop command, `injected` once the service was seen down and
`cleared` once it is up again with a new identity, so an injection without its `cleared` line is open and the next
start settles it.

A crash in the middle of an append can leave a last line without its newline. The reader ignores such an incomplete
final line, and never anything else: a complete line that is not an injection event makes the journal unreadable. The
next append first cuts the incomplete line off and syncs the cut, so a later line is never glued to it. The repair and
the append are one critical section across every process: both run under an exclusive lock on `journal.lock` beside
the journal, held only for that section, so a writer that read a torn tail cannot cut off a line another wrote since.

Each target also has a lock file in the journal's directory. An injector holds the target's lock, exclusively, for as
long as it may have the target down; the kernel drops it when the holder dies, so recovery settles only the open
injections whose holder is gone and never brings back a provider another live run took down on purpose. A held lock is a
`HeldLock`, closed explicitly or, when its owner is discarded without closing it, by its finalizer, so an injector
dropped after a failed clear lets the target go. A forked child would inherit the lock and keep it after its parent
died, so every lock descriptor this process holds is closed in a child right after a fork (`os.register_at_fork`); the
child must not use an injector of its parent. The `injecting`
line also names its holder, the pid and the boot id (`holder`), so that recovery can tell a dead holder from a live one:
a lock that someone holds while the holder named in the journal is gone is stuck, and the start fails closed instead of
skipping a provider nobody will bring back. A pid reused by an unrelated process still reads as alive, so the stuck
lock is then skipped as a live one; the boot id rules out a holder from before a reboot.

A restore that failed is journalled too (`clear_failed`, with how it failed), so an injection that is still open after
a failed clear can be told from one in progress. A stop command that raised (it timed out, so its CLI was killed while
the Docker daemon goes on stopping the container) is journalled as `stop_unknown`. A process killed while the stop
command blocks has no time to journal anything, so the `stopping` line before the command stands for it: an open
injection that was never seen down (no `injected` line) is one whose stop may still be running, and whoever settles it,
this process or a later one, waits for the container to show down before it judges the restore. A journal that ends at
`stopping` or `stop_unknown` says the stop was asked for. One that ends at `injecting` says nothing about it
(`stop_unrecorded`): the process died before the command, or its journal predates the `stopping` line. A container that
never shows down is `stop_unsettled` after the first, and `never_down` after the second only when every reading of the
whole bound showed it running with its identity. Beside the journal,
whoever holds the federation exclusively keeps a holder record (`federation.holder`, written whole and fsynced by
`write_holder`; `expcore_onedata.admission`): its pid and boot id, its purpose, the target's label and the time it took
the lock.
"""

import contextlib
import fcntl
import json
import os
import re
import weakref
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.healthgate import Health, HealthCheck
from expcore.errors import TARGET_LABEL

JOURNAL = "injections.jsonl"
JOURNAL_LOCK = "journal.lock"
HOLDER = "federation.holder"  # who holds the federation exclusively: written by the holder, read when a wait runs out
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_HELD: dict[int, weakref.finalize] = {}  # the lock descriptors this process holds, with the finalizer of each


def _close_inherited_locks() -> None:
    """In a child just forked: close every inherited lock descriptor, never unlocking it (the lock is the parent's), and
    detach its finalizer, which would otherwise close whatever later reuses the number."""
    for descriptor, finalizer in list(_HELD.items()):
        finalizer.detach()
        with contextlib.suppress(OSError):
            os.close(descriptor)
    _HELD.clear()


def _release(descriptor: int) -> None:
    """Drop a held lock and close its descriptor; run once, by `HeldLock.close` or by its finalizer."""
    _HELD.pop(descriptor, None)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class HeldLock:
    """A lock this process holds (a target's, or the federation's) until `close` or until the object is collected."""

    def __init__(self, descriptor: int) -> None:
        self._finalizer = weakref.finalize(self, _release, descriptor)
        _HELD[descriptor] = self._finalizer

    @property
    def held(self) -> bool:
        return self._finalizer.alive

    def close(self) -> None:
        """Drop the lock; closing it again is harmless."""
        self._finalizer()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_inherited_locks)


def boot_id() -> str | None:
    """The id of the running boot, where the system reports one; None otherwise."""
    try:
        return BOOT_ID.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def holder() -> dict[str, Any]:
    """Who journals an injection: this process's pid and the boot it runs in."""
    return {"pid": os.getpid(), "boot_id": boot_id()}


def holder_alive(named: Any) -> bool | None:
    """Tell whether the holder named in an `injecting` line still runs: False when its boot is over or no process has
    its pid, True when one has, None when the line names no readable holder."""
    if not isinstance(named, Mapping):
        return None
    pid = named.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    recorded, now = named.get("boot_id"), boot_id()
    if recorded is not None and now is not None and recorded != now:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # a process of another user has that pid
    return True


def _fsync_directory(directory: Path) -> None:
    """Make a directory's entries durable: a file created in it survives a crash only once this has returned."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_directories(directory: Path) -> None:
    """Create the directory and its missing parents, outermost first, each made durable in its parent."""
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for made in reversed(missing):
        with contextlib.suppress(FileExistsError):  # another process made it: its entry is synced here all the same
            made.mkdir()
        _fsync_directory(made.parent)


class InjectionJournal:
    """Append-only record of every injection and its clear: the target's label, its service, its identity's digest and
    its holder.

    Lines: `injecting` (before the stop command), `stopping` (right before it, so a process killed while it blocks
    leaves the line), `stop_unknown` (once the stop command raised, so what it did is not known), `injected` (once the
    target was seen down) and `cleared` (once a restore was verified, or recovery found the target up). An `injecting`
    line no `cleared` line settles is open.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / JOURNAL

    @contextlib.contextmanager
    def _section(self, mode: int = fcntl.LOCK_EX) -> Iterator[None]:
        """Hold the journal-wide lock for one critical section; closing the descriptor drops it."""
        descriptor = os.open(self.directory / JOURNAL_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, mode)
            yield
        finally:
            os.close(descriptor)

    def append(self, line: Mapping[str, Any]) -> None:
        """Append one line durably: under the journal-wide lock, an incomplete last line cut off first, then the line
        written, flushed and fsynced, with the directory fsynced when the line created the file."""
        make_directories(self.directory)
        with self._section():
            self._cut_incomplete_line()
            new_file = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(line), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if new_file:
                _fsync_directory(self.directory)

    def _cut_incomplete_line(self) -> None:
        """Cut off a last line a crash left without its newline, and make the cut durable."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0:
            return
        with open(self.path, "r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return
            handle.seek(0)
            keep = handle.read().rfind(b"\n") + 1
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())

    def open_entries(self) -> list[dict[str, Any]]:
        """Return the open injections, each with `injected` set when it was seen down, `stop_unknown` when the stop was
        asked for (`stopping`, or a stop command that raised) and the target was not then seen down, and
        `stop_unrecorded` when no line says the stop was asked for and the target was not seen down either, and
        `healed` when a `clear_failed` line says a link heal ran for it (`healing`, O35); raise ValueError when a
        complete line is not an injection event. Only an incomplete final line is ignored: it was never acted on."""
        if not self.path.exists():
            return []
        with self._section(fcntl.LOCK_SH):
            data = self.path.read_bytes()
        complete = data[: data.rfind(b"\n") + 1].decode("utf-8")
        entries: dict[str, dict[str, Any]] = {}
        for number, text in enumerate(complete.splitlines(), 1):
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                raise ValueError(f"line {number} of the injection journal is not JSON") from None
            if not isinstance(line, dict):
                raise ValueError(f"line {number} of the injection journal is not an injection event")
            event, key = line.get("event"), line.get("id")
            if event == "injecting":
                if not TARGET_LABEL.fullmatch(str(line.get("target"))) or not isinstance(line.get("service"), str):
                    raise ValueError(f"line {number} of the injection journal names no target or service")
                entries[key] = {**line, "injected": False}
            elif event == "injected" and key in entries:
                entries[key]["injected"] = True
            elif event in ("stopping", "stop_unknown") and key in entries:
                entries[key]["stop_unknown"] = True  # the stop was asked for: the daemon may still be stopping it
            elif event == "clear_failed" and key in entries:
                entries[key]["clear_failed"] = str(line.get("how"))  # still open: its restore failed
                if line.get("healing") is True:  # a link heal ran for it (O35): no process heals it again
                    entries[key]["healed"] = True
                held = line.get("settle_until")  # a heal's settle (O35): no process reads through the links before it
                if isinstance(held, int | float) and not isinstance(held, bool):
                    entries[key]["settle_until"] = max(held, entries[key].get("settle_until", held))
            elif event == "cleared":
                entries.pop(key, None)
            else:
                raise ValueError(f"line {number} of the injection journal is not an injection event")
        for entry in entries.values():  # a target seen down has nothing left to wait for
            entry["stop_unknown"] = entry.get("stop_unknown", False) and not entry["injected"]
            entry["stop_unrecorded"] = not entry["injected"] and not entry["stop_unknown"]
        return list(entries.values())

    # --- the federation's holder record ---------------------------------------------------------------------------

    def write_holder(self, record: Mapping[str, Any]) -> None:
        """Write the record of whoever holds the federation exclusively, whole or not at all: to a file of its own,
        fsynced, renamed over the record and the rename made durable, under the journal-wide lock."""
        make_directories(self.directory)
        with self._section():
            self._replace_holder(record)

    def _replace_holder(self, record: Mapping[str, Any]) -> None:
        """Under the section: write the record to a draft, fsynced, rename it over the record and sync the rename."""
        draft = self.directory / f"{HOLDER}.draft"  # one writer at a time: the section is held
        with open(draft, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, self.directory / HOLDER)
        _fsync_directory(self.directory)

    def read_holder(self) -> dict[str, Any] | None:
        """The holder record, or None when there is none or it is not a record (it then names no holder)."""
        path = self.directory / HOLDER
        if not path.exists():
            return None
        with self._section(fcntl.LOCK_SH):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                return None
        return record if isinstance(record, dict) else None

    def clear_holder(self, token: str | None) -> bool:
        """Remove the holder record if it is the one `token` names (any record when None); whether one was removed. The
        read and the removal are one critical section, so a record written since it was read is never removed."""
        path = self.directory / HOLDER
        if not path.exists():
            return False
        with self._section():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return False
            except ValueError:
                record = None  # not a record: whoever wrote it, it names no holder
            if token is not None and (not isinstance(record, dict) or record.get("token") != token):
                return False
            path.unlink()
            _fsync_directory(self.directory)
        return True

    def mark_holder_stuck(self, token: str | None, since: str) -> dict[str, Any] | None:
        """Mark the holder record `token` names (any record when None) stuck since `since`, unless it is marked
        already; return the record as it now stands, or None when it is not that record. One critical section."""
        path = self.directory / HOLDER
        with self._section():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                return None
            if not isinstance(record, dict) or (token is not None and record.get("token") != token):
                return None
            if "stuck_since" in record:
                return record
            record["stuck_since"] = since
            self._replace_holder(record)
        return record

    def try_lock(self, target: str) -> HeldLock | None:
        """Take the target's exclusive lock without waiting; return it, or None when another holds it."""
        make_directories(self.directory)
        name = re.sub(r"[^a-z0-9_]+", "-", target).strip("-")
        descriptor = os.open(self.directory / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return HeldLock(descriptor)


class SettledInjections:
    """A health check that fails while the fault journal holds an unsettled injection, then answers as `inner` does.

    `PlatformHealth` runs it first, built in, whenever platform.toml has a `[faults]` table, so the scheduler's gate
    holds back every run while a provider is down or unverified (`expcore_onedata.faults`, Baseline). Its detail names
    targets by their labels only.
    """

    def __init__(self, settings: Any, inner: HealthCheck | None = None) -> None:
        """`settings` is the `FaultSettings` of `[faults]`; only its journal directory is read."""
        self._journal, self._inner = InjectionJournal(settings.journal), inner

    def __call__(self) -> Health:
        try:
            entries = self._journal.open_entries()
        except (OSError, ValueError) as error:
            return Health(False, f"the injection journal cannot be read ({type(error).__name__})")
        if entries:
            targets = ", ".join(sorted({entry["target"] for entry in entries}))
            return Health(False, f"an injection of {targets} is unsettled: the federation is not in its baseline state")
        return self._inner() if self._inner is not None else Health(True, "no injection is unsettled")
