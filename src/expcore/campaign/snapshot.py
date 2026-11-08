# Provenance: new
"""A snapshot of the campaign store, so that the analysis reads bytes the replay check has seen.

The records of a batch are checked and hashed, but the store says which attempt of which run is the one that counts, and
it is a live SQLite file that a dispatcher may be writing. An analysis that read it directly could be steered by an edit
made after the check. So the check takes a consistent copy of the store into the batch and hashes it with the records,
and the analysis reads that copy and nothing else.

- `take_snapshot` copies the store with `VACUUM INTO` over a read-only connection, which sees one consistent state
  of the database whatever a dispatcher is doing, checks the copy's integrity, and moves it into place atomically. It
  refuses a store with attempts still running unless told that a partial snapshot is wanted, because a running
  attempt has no final state to analyse.
- `StoreSnapshot` is a read-only view of such a copy, built from bytes that the caller has already hashed, so that
  nothing is read twice. It answers what `export_run_state` asks of a store.
"""

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from expcore.campaign.factors import FactorSchema
from expcore.campaign.store import column_for

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


def take_snapshot(source: Path, destination: Path, *, allow_running: bool = False) -> int:
    """Copy the store at `source` to `destination` and return how many attempts were running in the copy.

    The copy is made beside `destination` under a scratch name, checked with `PRAGMA integrity_check`, flushed and
    moved into place, so a reader sees the old file or the whole new one. With running attempts and no `allow_running`
    nothing is left at `destination`: the scratch copy is removed and `AttemptsRunning` is raised.
    """
    if not source.is_file():
        raise SnapshotError(f"{source}: no campaign store here; run the study first")
    scratch = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    scratch.unlink(missing_ok=True)
    try:
        origin = sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True, timeout=_LOCK_TIMEOUT_S)
        try:
            origin.execute("VACUUM INTO ?", (str(scratch),))
        finally:
            origin.close()
        copy = sqlite3.connect(scratch)
        try:
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
    finally:
        scratch.unlink(missing_ok=True)
    return int(running)


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
