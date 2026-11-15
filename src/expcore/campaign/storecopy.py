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
- both files are stated (device, inode, size, modification and change times) before and after the copy, and read
  again after it and compared, by SHA-256, with the bytes copied: a file system's timestamps may be too coarse to
  show a write (a checkpoint into the store and a reset of the `-wal` between the two copies, say), its bytes are
  not. A copy across which either file changed, appeared or went away may mix two states: it is thrown away and made
  again, at most `COPY_ATTEMPTS` times, `COPY_PAUSE_S` apart, and then `StoreChanged` is raised, naming the store;
- the copy lies in a temporary directory of its own (`store_copy`), which the caller opens read-write, so that SQLite
  replays the `-wal` into what it reads, and which is removed afterwards.

The reader holds the store's lock across the copy (`lock.ReaderLock`), so no dispatcher of this core writes it
meanwhile; the checks above are what a writer that ignores the lock would still meet. A store a dispatcher holds is
being written, and no copy of its files is one state; it is read in place, by the online backup over a read-only
connection (`snapshot.take_snapshot(..., in_place=True)`), as a live store's own connections already make its `-shm`
and `-wal`.
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

_Identity = tuple[int, int, int, int, int] | None


class StoreChanged(RuntimeError):
    """The store or its -wal changed while it was copied, on every attempt: it has a writer."""


def _identity(path: Path) -> _Identity:
    """What changes when a file is written, replaced or removed: device, inode, size, modification and change times."""
    try:
        found = os.stat(path)
    except FileNotFoundError:
        return None
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)


def _copy_file(source: Path, target: Path) -> str:
    """Copy one file's bytes, and nothing of its metadata; return the SHA-256 of the bytes copied."""
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(target, "wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str | None:
    """The SHA-256 of a file's bytes; None when it is not there."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as reader:
            for chunk in iter(lambda: reader.read(_CHUNK), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def copy_store(source: Path, into: Path) -> Path:
    """Copy the store at `source` and its -wal, never its -shm, into the existing directory `into`; return the copy.

    Raises StoreChanged when every attempt saw either file change, and leaves `into` without a copy then.
    """
    wal = source.with_name(source.name + WAL_SUFFIX)
    target, target_wal = into / source.name, into / wal.name
    for attempt in range(COPY_ATTEMPTS):
        if attempt:
            time.sleep(COPY_PAUSE_S)
        before = (_identity(source), _identity(wal))
        if before[0] is None:
            raise FileNotFoundError(f"{source}: no campaign store here")
        try:
            copied = (_copy_file(source, target), None if before[1] is None else _copy_file(wal, target_wal))
        except FileNotFoundError:
            copied = None  # one of them went away during the copy: a change
        unchanged = copied is not None and (_sha256(source), _sha256(wal)) == copied
        if unchanged and (_identity(source), _identity(wal)) == before:
            return target
        target.unlink(missing_ok=True)
        target_wal.unlink(missing_ok=True)
    raise StoreChanged(
        f"{source}: it changed while it was copied, {COPY_ATTEMPTS} times in a row, so a process is writing it; "
        "read it once that process has stopped"
    )


@contextmanager
def store_copy(source: Path) -> Iterator[Path]:
    """A copy of the finished store at `source` (`copy_store`) in a new temporary directory, removed on exit.

    Open it read-write, so that SQLite replays the copied -wal; its own -shm and -wal stay in that directory.
    """
    with tempfile.TemporaryDirectory(prefix="expcore-store-") as scratch:
        yield copy_store(source, Path(scratch))
