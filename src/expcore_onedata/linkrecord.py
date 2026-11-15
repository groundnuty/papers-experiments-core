# Provenance: new
"""The link-check record, `link-check.json`: the report of one all-pairs link check, written whole or not at all (O35).

`deploy/compose-federation`'s `up` writes it beside `facts.json` once its link check has run (#221), and so do a
restore's link check and every read of the pairs a heal makes (`fault_serves.ServingProbe.matrix`), so the record on
disk is the verdict of the latest check made, passing or failing, and an operator's pre-check that wants a check under
an hour old finds the heal's own re-check instead of re-running `up`. They all write it through `write_record`, so
there is one format: `record_text`, the report's JSON (`LinkReport.to_json`) with sorted keys and an indent of 2,
and a final newline, the bytes `up` always wrote.

**Atomic.** The text goes to a draft of the writer's own in the record's directory (its name holds the process and a
random part, so two writers never share one), which is flushed and fsynced and then renamed over the record, and the
directory is fsynced so that the rename survives a crash (best effort: the rename is done by then). A reader sees the
old record or the new one, whole. A write that fails leaves the old record as it was and raises `OSError`; the draft
is removed unless that removal fails too. The record keeps the mode a plain write gives it (the process's umask), as
`up`'s always had: it holds labels, seconds, bytes and transfer ids, no secret.

**In the order of the checks.** One check at a time makes the record: `recording` holds `link-check.lock`, beside the
record, exclusively around a check and the writing of its record, so a check that starts while another is under way
waits for it to end and writes after it, and the record is the latest-started check's, never an older one's that
finished late. Under a restore's and a heal's exclusive admission the lock is never contended; it orders what the
admission does not (`up` run on a federation another process is checking, say). No clock is read for the order. The
lock is a leaf of the federation's lock order (`admission`). The lock file is opened read-only and its owner makes it
0644, so every process that can write the directory can take part: `flock` needs no write permission. A lock that still
cannot be taken is no reason to skip the check: `recording` yields False, and a caller that cannot serialize publishes
no verdict (the probe removes the older record instead), so an unordered write never leaves an old pass standing; `up`,
which alone runs while nothing else does, writes and says so. The residual: a lock file that something other than
`recording` made unreadable to a process (not 0644, another user's), while a process that holds it is mid-write. The
first runs unserialized, publishes nothing and removes the older record, which the second can still replace with its
older pass. No lock the first cannot take orders it; the file's mode is the operator's to fix.

**An older verdict never stands.** `discard_record` removes the record, or, when the directory refuses that, empties it
in place (which needs only the file's own write permission) and dates it to the epoch, so that no reader takes it, by
its contents or by its time, for a recent check. It raises `OSError` only when neither is possible, the one case in
which a record this process cannot change stays as it was (the same permissions would stop `up` and the injector's
journal too).

**The record is advisory.** The federation's link epoch (`fault_epoch`) and the gate's heal journal are what a run's
admission compares (`admission_recheck`); the record is read by no admission and lives beside `platform.toml`, not in
the fault journal's directory. Writing it never raises or lowers the epoch.
"""

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from expcore_onedata.fault_journal import _fsync_directory

__all__ = ["LOCK", "RECORD", "discard_record", "record_text", "recording", "write_record"]

RECORD = "link-check.json"  # beside facts.json (under `up`) and platform.toml, wherever that file lies
LOCK = "link-check.lock"  # beside the record: held exclusively around a check and the writing of its record


def record_text(report: Any) -> str:
    """The text of a record: a `linkcheck.LinkReport` as JSON, sorted keys, indent 2, ending with a newline."""
    return json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n"


def write_record(report: Any, path: Path) -> None:
    """Write `report`'s record at `path`, atomically: a draft in the same directory, fsynced, renamed over `path`, the
    directory fsynced (best effort: the rename is done by then). Raises OSError when the record cannot be written; the
    old one is then as it was."""
    path = Path(path)
    draft = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # the umask's mode, as `open` gives
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(record_text(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):  # the record is in place, whole: only its survival of a crash is not assured
        _fsync_directory(path.parent)


def discard_record(path: Path) -> None:
    """Make sure no older verdict stands as the latest one: remove the record, or, when the directory refuses that,
    empty it in place and date it to the epoch. A record that is not there is no error. Raises OSError when the record
    can be neither removed nor emptied."""
    path = Path(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _empty(path)
        return
    with contextlib.suppress(OSError):  # the removal is done; its durability is the directory's
        _fsync_directory(path.parent)


def _empty(path: Path) -> None:
    """Empty the record in place (an empty record is no record: it is not JSON), and date it to the epoch so that a
    reader that goes by its time finds it old; the dating is best effort, the emptying is not."""
    try:
        with open(path, "r+b") as handle:
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return  # there is nothing left to stand
    with contextlib.suppress(OSError):
        os.utime(path, (0, 0))


@contextlib.contextmanager
def recording(path: Path) -> Iterator[bool]:
    """Hold the record's lock (`LOCK`, beside `path`) exclusively, waiting for any other check under way to end, for a
    check and the writing of its record; yield whether it is held. False means the lock file could not be opened or
    locked, and the caller must not publish a verdict that is not ordered. The file is opened read-only (created when
    it is not there) and its owner makes it readable to all (0644, whatever the umask): `flock` needs no write
    permission, so a lock file another user made through this function is taken all the same. A lock file made some
    other way and unreadable to this process leaves it unserialized (the module's residual). The lock is let go when
    the block ends, and by the kernel with a process that dies."""
    descriptor = None
    held = False
    try:
        descriptor = os.open(Path(path).parent / LOCK, os.O_RDONLY | os.O_CREAT, 0o666)
        with contextlib.suppress(OSError):  # the file's owner makes it readable to all, whatever its umask was
            os.fchmod(descriptor, 0o644)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held = True
    except OSError:
        pass
    try:
        yield held
    finally:
        if descriptor is not None:
            os.close(descriptor)
