# Provenance: new
"""The link-check record, `link-check.json`: the report of one all-pairs link check, written whole or not at all (O35).

`deploy/compose-federation`'s `up` writes it in the federation's root, beside `facts.json`, once its link check has run
(#221), and so do a restore's link check, every read of the pairs a heal makes (`fault_serves.ServingProbe.matrix`) and
the health gate's routine check (`health.PlatformHealth`), so the record on disk is the verdict of the latest check
made, passing or failing, and an operator's pre-check that wants a check under an hour old finds the heal's own
re-check instead of re-running `up`. They all write it through `write_record`, so there is one format: `record_text`,
the report's JSON (`LinkReport.to_json`) with sorted keys and an indent of 2, and a final newline, the bytes `up`
always wrote.

**Where: the federation's root, whichever platform file the check ran through (`record_path`).** The record describes
the federation's links, so it lies where `up` writes it and where a pre-check reads it, not beside whatever
`platform.toml` a check happened to run through: a file derived elsewhere (the outage check's example, in a work
directory) would otherwise strand it. A platform file names its federation by `[faults] journal`, the directory of the
federation's lock and heal journal, which `up` makes as `<root>/fault-journal` (`fedlayout.Layout.journal`) and every
file of one federation must name alike, or they would not share a lock. So the root is the journal's parent
(`federation_root`), and `load_faults` refuses a journal whose parent holds no `platform.toml` (the one `up` renders
there; the root's own, or any file derived beside it, is what makes a directory a federation's), rather than guess where
the record goes. A check run without `[faults]` (a plain platform file: the root's own rendered one has no such table)
declares no root and writes nothing; the record is the fault-gate checks' (a restore's, a heal's and the gate's
routine check, all of which need `[faults]`).

**Atomic.** The text goes to a draft of the writer's own in the record's directory (its name holds the process and a
random part, so two writers never share one), which is flushed and fsynced and then renamed over the record, and the
directory is fsynced so that the rename survives a crash (best effort: the rename is done by then). A reader sees the
old record or the new one, whole. A write that fails leaves the old record as it was and raises `OSError`; the draft
is removed unless that removal fails too. The record keeps the mode a plain write gives it (the process's umask), as
`up`'s always had: it holds labels, seconds, bytes and transfer ids, no secret.

**In the order of the checks.** One check at a time makes the record: `recording` holds `link-check.lock`, beside the
record, exclusively around a check and the writing of its record, so a check that starts while another is under way
waits for it and writes after it, and the record is the latest-started check's, never an older one's that finished
late. Under a restore's and a heal's exclusive admission the lock is never contended, and nor is it between the gate's
routine check, which holds the federation shared, and a heal, which holds it exclusively; it orders what the admission
does not (`up` run on a federation another process is checking, say, or the routine checks of two campaigns that hold
the federation shared together). No clock is read for the order. The lock is a leaf of the federation's lock order
(`admission`). The lock file is opened for writing (created when it is not there) and its owner makes it 0644:
NFS implements `flock` as a POSIX lock, which needs a descriptor open for writing to be exclusive, so a read-only
descriptor fails there with EBADF and no check would ever be serialized on an NFS root (found live, on an HPC
federation's NFSv4 root). A file this process cannot write (another user's 0644 file, a read-only mount, an immutable
file: EACCES, EROFS or EPERM) is opened read-only instead, which is enough on a local filesystem, where `flock` needs
no write permission, so every process that can write the directory can take part there. A lock that still cannot be
taken is no reason to skip the check: `recording` yields False, and a caller that cannot serialize publishes no
verdict (`fault_serves.recorded` removes the older record instead), so an unordered write never leaves an old pass
standing; `up`, which alone runs while nothing else does, writes and says so. The residuals: a lock file that
something other than `recording` made unreadable to a process (not 0644, another user's), and, where `flock` needs a
write descriptor (NFS), one that this process cannot write, while a process that holds it is mid-write. The first runs
unserialized, publishes nothing and removes the older record, which the second can still replace with its older pass.
No lock the first cannot take orders it; the file's mode, or its owner, is the operator's to fix.

**An older verdict never stands.** `discard_record` removes the record, or, when the directory refuses that, empties it
in place (which needs only the file's own write permission) and dates it to the epoch, so that no reader takes it, by
its contents or by its time, for a recent check. It raises `OSError` only when neither is possible, the one case in
which a record this process cannot change stays as it was (the same permissions would stop `up` and the injector's
journal too).

**The record is advisory.** The federation's link epoch (`fault_epoch`) and the gate's heal journal are what a run's
admission compares (`admission_recheck`); the record is read by no admission and lives in the root, not in the fault
journal's directory. Writing it never raises or lowers the epoch.

**A record that was not written is a fact of the run, not only a log line.** A check that could not be serialized (the
lock not taken) or whose record could not be written leaves no record, and the older one is removed
(`fault_serves.recorded`). The report it answers with says so (`LinkReport.not_written`: `LOCK_NOT_TAKEN` or
`WRITE_FAILED`, fixed words that hold no path and no error text), and that reaches the record of the run whose restore,
heal or routine check it was, as an optional fact, `link_record: "not_written"` with `link_record_reason` (`fact`):
the fault injector's snapshot for a restore and for a heal (`faults`), the heal's own matrices, the try's note for a
routine check (`LINK_RECORD_NOTE`, `health.PlatformHealth.link_record`). It is absent, never null, when the record was
written. It is never part of the record file, whose bytes stay what `up` always wrote.
"""

import contextlib
import errno
import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from expcore_onedata.fault_journal import _fsync_directory

__all__ = [
    "LINK_RECORD_NOTE",
    "LOCK",
    "LOCK_NOT_TAKEN",
    "NOT_WRITTEN",
    "RECORD",
    "ROOT_FILE",
    "WRITE_FAILED",
    "discard_record",
    "fact",
    "federation_root",
    "record_path",
    "record_text",
    "recording",
    "write_record",
]

RECORD = "link-check.json"  # in the federation's root, beside facts.json and platform.toml
LOCK = "link-check.lock"  # beside the record: held exclusively around a check and the writing of its record
ROOT_FILE = "platform.toml"  # what a federation's root holds: `up` renders it there (`fedlayout.Layout.platform`)
NOT_WRITTEN = "not_written"  # the value of a run's `link_record` fact: the check's record was not written
LOCK_NOT_TAKEN = "lock_not_taken"  # why: the check could not be serialized, so its verdict was published by no one
WRITE_FAILED = "write_failed"  # why: the lock was taken and the record could not be written
LINK_RECORD_NOTE = "link_record"  # the try's note (`expcore.campaign.prerun`) a routine check that wrote none leaves


def fact(reason: str) -> dict[str, str]:
    """The optional fact a run's record holds for a check whose record was not written: `link_record` is
    `NOT_WRITTEN` and `link_record_reason` is `reason` (`LOCK_NOT_TAKEN` or `WRITE_FAILED`). A check that wrote its
    record has no such fact, and its readings hold neither key."""
    return {"link_record": NOT_WRITTEN, "link_record_reason": reason}


def federation_root(journal: Path) -> Path:
    """The federation's root, from the `[faults] journal` its platform files name: the journal's parent, since `up`
    makes the journal as `<root>/fault-journal` (`fedlayout.Layout.journal`; a test pins the two together)."""
    return Path(journal).parent


def record_path(journal: Path) -> Path:
    """Where the federation's record lies: `link-check.json` in its root (`federation_root`), the file `up` writes and
    an operator's pre-check reads, whichever platform file the check ran through."""
    return federation_root(journal) / RECORD


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


def _open_lock(lock: Path) -> int:
    """Open the lock file, creating it when it is not there: for writing, which an exclusive `flock` needs on NFS, and,
    when the file is one this process cannot write (EACCES, EROFS, EPERM), read-only, which suffices on a local
    filesystem. Any other failure to open it raises, and a read-only descriptor that the filesystem will not lock
    exclusively is refused by the `flock` that follows."""
    try:
        return os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EROFS, errno.EPERM):  # EPERM: an immutable or append-only file
            raise
    return os.open(lock, os.O_RDONLY | os.O_CREAT, 0o666)


@contextlib.contextmanager
def recording(path: Path) -> Iterator[bool]:
    """Hold the record's lock (`LOCK`, beside `path`) exclusively, waiting for any other check under way to end, for a
    check and the writing of its record; yield whether it is held. False means the lock file could not be opened or
    locked, and the caller must not publish a verdict that is not ordered. The file is opened for writing (created when
    it is not there), because an exclusive `flock` needs a write descriptor on NFS; one this process cannot write
    (EACCES, EROFS, EPERM) is opened read-only, which a local filesystem locks all the same and NFS refuses, so that
    case is False there. Its owner makes it readable to all (0644, whatever the umask), so that a process of another
    user can take part through that read-only open. A lock file made some other way and unreadable to this process
    leaves it unserialized (the module's residual). The lock is let go when the block ends, and by the kernel with a
    process that dies."""
    descriptor = None
    held = False
    try:
        descriptor = _open_lock(Path(path).parent / LOCK)
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
