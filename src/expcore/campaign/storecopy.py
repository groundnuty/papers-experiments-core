# Provenance: new
"""A private copy of a finished campaign store, so that reading it writes nothing beside it (R16).

The campaign keeps its store in WAL mode for good. An open of such a database in place, even a read-only one, makes
its `-shm` and `-wal` beside it and cannot remove them, and fails outright where the directory is read-only (a
shard's `out/` fetched back from another host, say). A reader of a finished store (no dispatcher holds it) copies it
instead and reads the copy:

- the store file and its `-wal`, which holds every transaction committed since the last checkpoint (all of them,
  after a killed dispatcher), are copied as they are, the `-wal` only when there is one;
- the `-shm` never: it is an index of the `-wal` shared between live connections, and SQLite rebuilds it from the
  `-wal` when the copy is opened;
- each file's signature is taken before and after the copy: device, inode, size, modification and change times and
  the SHA-256 of its contents, read in chunks (a missing `-wal` is part of it). A copy is kept only when both
  signatures of each file are equal and the SHA-256 of the bytes copied equals both. File times move only on the
  kernel's coarse tick, on tmpfs too, so a write in place within one tick leaves size, times and inode as they were:
  only the contents show it. The change time, which no writer can put back, shows a write in another tick even where
  the contents were put back. A copy across which either file changed, appeared or went away may mix two states: it
  is thrown away and made again, at most `COPY_ATTEMPTS` times, `COPY_PAUSE_S` apart, and then `StoreChanged` is
  raised, naming the file that changed;
- the copy lies in a temporary directory of its own (`store_copy`), which the caller opens read-write, so that SQLite
  replays the `-wal` into what it reads, and which is removed afterwards.

What makes the copy one state is the reader's hold of the store's lock across it (`lock.ReaderLock`): no dispatcher
of this core writes the store meanwhile. The checks above catch a writer that ignores the lock and leaves a trace in
what they sample; they are no snapshot against one that does not (that changes both files and puts each back, within
one tick, between the reads), which no copy of files can be. A store a dispatcher holds is being written, and no copy
of its files is one state; it is read in place, by the online backup over a read-only connection
(`snapshot.take_snapshot(..., in_place=True)`), as a live store's own connections already make its `-shm` and `-wal`.
"""

import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

COPY_ATTEMPTS = 3
COPY_PAUSE_S = 0.25
WAL_SUFFIX = "-wal"
_CHUNK = 1 << 20

_Stat = tuple[int, int, int, int, int]  # device, inode, size, modification and change times (ns)
_Signature = tuple[int, int, int, int, int, str] | None  # the stat and the SHA-256 of the contents


class StoreChanged(RuntimeError):
    """The store or its -wal changed while it was copied, on every attempt: it has a writer."""


def _sha256(path: Path) -> str | None:
    """The SHA-256 of a file's bytes, read in chunks; None when it is not there."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as reader:
            for chunk in iter(lambda: reader.read(_CHUNK), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def _stat(path: Path) -> _Stat | None:
    """A file's device, inode, size, modification and change times; None when it is not there."""
    try:
        found = os.stat(path)
    except FileNotFoundError:
        return None
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


def _signature(path: Path) -> _Signature:
    """A file's stat (`_stat`) and the SHA-256 of its contents; None when it is not there."""
    stated = _stat(path)
    if stated is None:
        return None
    digest = _sha256(path)
    if digest is None:
        return None  # it went away between the two
    return (*stated, digest)


def _copy_file(source: Path, target: Path) -> str:
    """Copy one file's bytes in chunks, and nothing of its metadata; return the SHA-256 of the bytes copied."""
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(target, "wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest()


def _changed(before: _Signature, copied: str | None, after: _Signature) -> bool:
    """Whether a file changed across its copy: its signatures differ, or the bytes copied are not the ones both name
    (for a file that is not there, None throughout)."""
    if before != after:
        return True
    return copied != (None if before is None else before[-1])


def copy_store(source: Path, into: Path) -> Path:
    """Copy the store at `source` and its -wal, never its -shm, into the existing directory `into`; return the copy.

    Raises StoreChanged, naming the file, when every attempt saw either file change, and leaves `into` without a copy
    then.
    """
    wal = source.with_name(source.name + WAL_SUFFIX)
    target, target_wal = into / source.name, into / wal.name
    changed = source
    for attempt in range(COPY_ATTEMPTS):
        if attempt:
            time.sleep(COPY_PAUSE_S)
        before = (_signature(source), _signature(wal))
        if before[0] is None:
            raise FileNotFoundError(f"{source}: no campaign store here")
        copied: list[str | None] = [None, None]
        vanished = None
        for index, (path, out) in enumerate(((source, target), (wal, target_wal))):
            if before[index] is None:
                continue
            try:
                copied[index] = _copy_file(path, out)
            except FileNotFoundError:
                vanished = path  # it went away during its copy: a change
                break
        after = (_signature(source), _signature(wal))
        moved = [
            path
            for path, was, got, now in zip((source, wal), before, copied, after, strict=True)
            if _changed(was, got, now)
        ]
        if vanished is None and not moved:
            return target
        changed = vanished or moved[0]
        target.unlink(missing_ok=True)
        target_wal.unlink(missing_ok=True)
    raise StoreChanged(
        f"{changed}: it changed while it was copied, {COPY_ATTEMPTS} times in a row, so a process is writing it; "
        "read the store once that process has stopped"
    )


@contextmanager
def store_copy(source: Path) -> Iterator[Path]:
    """A copy of the finished store at `source` (`copy_store`) in a new temporary directory, removed on exit.

    Open it read-write, so that SQLite replays the copied -wal; its own -shm and -wal stay in that directory.
    """
    with tempfile.TemporaryDirectory(prefix="expcore-store-") as scratch:
        yield copy_store(source, Path(scratch))
