# Provenance: new
"""A snapshot of the campaign store, so that the analysis reads bytes the replay check has seen.

The records of a batch are checked and hashed, but the store says which attempt of which run is the one that counts, and
it is a live SQLite file that a dispatcher may be writing. An analysis that read it directly could be steered by an edit
made after the check. So the check takes a consistent copy of the store into the batch and hashes it with the records,
and the analysis reads that copy and nothing else.

- `take_snapshot` copies the store with SQLite's online backup API, in one step, which sees one consistent state of
  the database, checks the copy's integrity, and moves it into place atomically. A finished store is backed up from a
  private copy of its files (`campaign.storecopy`), so nothing is written beside it and its directory may be
  read-only (R16); a store a dispatcher holds is backed up in place over a read-only connection (`in_place`), which
  sees one committed state whatever the dispatcher is doing. The backup copies pages, so the copy has the same
  schema as the store whatever the SQLite version (`VACUUM INTO` rebuilds the tables, and older versions fail on the
  runs table's generated columns). It
  refuses a store with attempts still running unless told that a partial snapshot is wanted, because a running
  attempt has no final state to analyse. In the copy, each attempt's directory is written relative to the batch
  (`<digest>/try<N>`, as the export gives it), and the copy is then rebuilt with a plain `VACUUM`, which keeps no free
  space and so none of the old row versions that free space holds: the store keeps the absolute path its recovery
  needs, but the copy that is hashed, analysed and shipped in a replication package holds no path of the machine the
  campaign ran on. (`VACUUM INTO` is what fails on older SQLite with generated columns; a plain `VACUUM` does not.)
- `StoreSnapshot` is a read-only view of such a copy, built from bytes that the caller has already hashed, so that
  nothing is read twice. It answers what `export_run_state` asks of a store.
"""

import datetime as dt
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import quote

from expcore.campaign.export import batch_relative
from expcore.campaign.factors import FactorSchema
from expcore.campaign.interrupted_spend import COPIED_BACK_KEY
from expcore.campaign.store import column_for
from expcore.campaign.storecopy import StoreChanged, store_copy

SNAPSHOT_FILE = "campaign-snapshot.sqlite"
_LOCK_TIMEOUT_S = 30.0


class SnapshotError(RuntimeError):
    """The store cannot be copied, or the copy is not a store for this study."""


class AttemptsRunning(SnapshotError):
    """The store has attempts still running, so no final state exists for them; `count` says how many."""

    def __init__(self, count: int) -> None:
        super().__init__(
            f"{count} attempt(s) are still running (or a killed dispatcher left them); run the campaign again to "
            "settle them, or ask for a partial snapshot"
        )
        self.count = count


def take_snapshot(source: Path, destination: Path, *, allow_running: bool = False, in_place: bool = False) -> int:
    """Copy the store at `source` to `destination` and return how many attempts were running in the copy.

    A finished store (no dispatcher holds it) is read from a private copy of its file and its -wal
    (`storecopy.store_copy`), opened read-write in a directory of its own so the -wal replays: nothing is written
    beside the store, whose directory may be read-only, and a store that changes during that copy is refused. With
    `in_place`, for a store a dispatcher holds and is writing, the store itself is read over a read-only connection,
    which leaves the -shm and -wal a live store has anyway.

    The snapshot is made beside `destination` under a scratch name by the online backup API (all pages in one step,
    so one consistent state), put in the rollback-journal mode so that it is one self-contained file (a store in WAL
    mode makes a copy that says WAL), checked with `PRAGMA integrity_check`, flushed and moved into place, so a reader
    sees the old file or the whole new one. With running attempts and no `allow_running` nothing is left at
    `destination`: the scratch copy is removed and `AttemptsRunning` is raised.
    """
    if not source.is_file():
        raise SnapshotError(f"{source}: no campaign store here; run the study first")
    scratch = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    scratch.unlink(missing_ok=True)
    try:
        with _readable(source, in_place) as origin:
            copy = sqlite3.connect(scratch)
            try:
                origin.backup(copy)  # pages=-1: one step, so one consistent state of the store
                copy.execute("PRAGMA journal_mode = DELETE")
                _relative_run_dirs(copy)
                verdict = copy.execute("PRAGMA integrity_check").fetchall()
                running = copy.execute("SELECT COUNT(*) FROM attempts WHERE state = 'running'").fetchone()[0]
            finally:
                copy.close()
        if [tuple(row) for row in verdict] != [("ok",)]:
            raise SnapshotError(f"{source}: the copy of the store failed its integrity check")
        if running and not allow_running:
            raise AttemptsRunning(running)
        with open(scratch, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(scratch, destination)
    except sqlite3.Error as error:
        raise SnapshotError(f"{source}: cannot copy the store ({type(error).__name__}: {error})") from None
    except StoreChanged as error:
        raise SnapshotError(str(error)) from None
    except OSError as error:
        raise SnapshotError(f"{source}: cannot copy the store ({type(error).__name__}: {error})") from None
    finally:
        scratch.unlink(missing_ok=True)
    return int(running)


@contextmanager
def _readable(source: Path, in_place: bool) -> Iterator[sqlite3.Connection]:
    """A connection to read the store at `source` from: a read-only one to the store itself with `in_place`, else a
    read-write one to a private copy (`storecopy.store_copy`), so that its copied -wal replays. Closed on exit."""
    with ExitStack() as stack:
        if in_place:
            connection = sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True, timeout=_LOCK_TIMEOUT_S)
        else:
            connection = sqlite3.connect(stack.enter_context(store_copy(source)), timeout=_LOCK_TIMEOUT_S)
        try:
            yield connection
        finally:
            connection.close()


def rebase_run_dirs(store: Path, runs: Path) -> int:
    """Point every attempt's directory in the store at `store` to `runs/<digest>/try<N>`, and return how many.

    A snapshot names each attempt's directory within the batch (`<digest>/try<N>`), and a store moved to another
    directory keeps the absolute ones of the place it ran in; the recovery of a resumed store reads each running
    attempt's marker at the path its row holds (C04). So a store copied back to run again, from a snapshot or an
    archive (`deploy/hpc-federation/shards.py`, O62), has its directories pointed at the runs directory it now has,
    before any dispatcher opens it. Only the last two parts of each path are kept, as the export keeps them. The store
    must be one no process holds; its -wal, if any, replays on the open.

    In the same transaction the store is marked copied back (`interrupted_spend.COPIED_BACK_KEY` in its meta, the
    time): an attempt the copy holds as running may have gone on after the copy, so its call log is never trusted on
    the copy's word alone, only as its spend journal confirms it (C05)."""
    connection = sqlite3.connect(store, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT id, run_dir FROM attempts WHERE run_dir IS NOT NULL").fetchall()
        moved = [(str(runs / str(batch_relative(directory))), attempt) for attempt, directory in rows]
        connection.executemany("UPDATE attempts SET run_dir = ? WHERE id = ?", moved)
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value, set_utc) VALUES(?, ?, ?)", (COPIED_BACK_KEY, now, now)
        )
        connection.execute("COMMIT")
        return len(moved)
    finally:
        connection.close()


def _relative_run_dirs(copy: sqlite3.Connection) -> None:
    """Write each attempt's directory in the copy as `<digest>/try<N>`, then rebuild the copy so no old path stays.

    A page copy carries the store's free space, which still holds earlier versions of updated rows; `VACUUM` writes
    the live rows into fresh pages and drops it.
    """
    rows = copy.execute("SELECT id, run_dir FROM attempts WHERE run_dir IS NOT NULL").fetchall()
    copy.executemany("UPDATE attempts SET run_dir = ? WHERE id = ?", [(batch_relative(d), i) for i, d in rows])
    copy.commit()
    copy.execute("VACUUM")


class StoreSnapshot:
    """A read-only view of a snapshot's bytes, answering `q` and `snapshot` as a store does.

    The bytes are loaded into memory, so what is read is exactly what the caller hashed: the file is never opened again.
    A database made for other factors than the schema's is refused.
    """

    def __init__(self, data: bytes, schema: FactorSchema) -> None:
        self.schema = schema
        self._lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        try:
            self.db.deserialize(data)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only = ON")
            found = [r["name"] for r in self.db.execute("PRAGMA table_xinfo(runs)") if r["hidden"] in (2, 3)]
        except sqlite3.Error as error:
            self.db.close()
            raise SnapshotError(f"the snapshot is not a campaign store ({type(error).__name__}: {error})") from None
        expected = [column_for(f) for f in schema.order]
        if found != expected:
            self.db.close()
            raise SnapshotError(f"the snapshot holds factor columns {found}, but the schema's are {expected}")

    def q(self, sql: str, args: Iterable[object] = ()) -> list[sqlite3.Row]:
        """Run a read and return all rows."""
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Hold one read transaction, as the store's does."""
        with self._lock:
            self.db.execute("BEGIN")
            try:
                yield
            finally:
                self.db.execute("COMMIT")

    def close(self) -> None:
        """Close the in-memory database."""
        with self._lock:
            self.db.close()
