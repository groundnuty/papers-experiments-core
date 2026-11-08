# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/store.py, blob 8e7c6cbfd0a274d8ae91a5da4f3a167cc184aef6
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Durable campaign state in SQLite (WAL, synchronous=FULL).

Four tables. `runs`: one row per planned run, with its key, the factor values as JSON and one indexed generated column
per factor. `attempts`: every try of a run, whatever came of it. `meta`: campaign-level facts that must not change
under a resume. `events`: a log of what the campaign did.

Kill safety (C04). An attempt is inserted as 'running' before the run starts, and moved to its final state in one
transaction together with the run's status. A process killed in between leaves a 'running' row. The next campaign
process (holding the exclusive lock, so nothing else is running) calls `expcore.campaign.recovery.recover`: an attempt
whose outcome marker was written (`expcore.campaign.markers`) is committed from it and never run again; every other
'running' row becomes 'interrupted', is kept, is never charged, and its run is run again under the same attempt number.

Charging (C03) and treatment attempts are decided by `expcore.campaign.attempts.classify` when an attempt finishes, and
stored in its row.

Schema changes (C20). The fixed tables are created whole; a column added by a later version goes in with one
`ALTER TABLE ... ADD COLUMN` per column, so a kill between two leaves a database that the next open finishes. The
factor columns depend on the study; a database made for other factors is refused, never migrated.

Concurrent opens (C20). Many processes may open one new database together. Each migration step runs in a write
transaction of its own and the busy timeout orders them. The switch to WAL is not covered by the busy timeout, so
`Store._enter_wal` retries it, bounded by the lock timeout, and refuses the database, naming it, when it still has not
got WAL.
"""

import datetime as dt
import json
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from expcore.campaign.attempts import (
    AttemptRefused,
    BegunAttempt,
    PinMismatch,
    ResumeRefused,
    SchemaMismatch,
    TreatmentKindsMismatch,
    classify,
)
from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import ManifestRow, relative_order_problem
from expcore.campaign.markers import AttemptResult

_FIXED_SQL = """
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL REFERENCES runs(run_key), attempt INTEGER NOT NULL,
  state TEXT NOT NULL,  -- running|done|infra_fail|outage_discarded|interrupted|harness_error
  charged INTEGER NOT NULL DEFAULT 0,  -- 1: counts against the attempt limit
  started_utc TEXT, ended_utc TEXT, wall_s REAL, pid INTEGER,
  infra_kind TEXT, detail TEXT, termination TEXT,  -- termination: the loop's outcome name
  input_tokens INTEGER, output_tokens INTEGER,  -- NULL: not reported
  run_dir TEXT);
CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_key);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, set_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, model TEXT, detail TEXT);
"""
# Columns added after the first schema. Every database, new or old, gets them through the same path: one ALTER TABLE
# per column, each checked and applied in a write transaction of its own.
_ADDED_COLUMNS = (
    ("attempts", "cache_creation_tokens", "INTEGER"),  # a part of input_tokens
    ("attempts", "cache_read_tokens", "INTEGER"),  # a part of input_tokens
    ("attempts", "usage_complete", "INTEGER NOT NULL DEFAULT 1"),  # 0: the totals are known to be short
    ("attempts", "treatment", "INTEGER NOT NULL DEFAULT 0"),  # 1: a fault that is the condition's treatment
    ("attempts", "outage_kind", "TEXT"),  # the kind of an outage that overlapped the attempt; apart from infra_kind
)
# Statements that fill a column from what older databases kept elsewhere, run in the same transaction that adds it. Before
# `outage_kind` existed, the kind of an outage was stored in `infra_kind`, so an outage-discarded attempt of such a
# database has it there.
_BACKFILLS = {
    "outage_kind": "UPDATE attempts SET outage_kind = infra_kind WHERE state = 'outage_discarded' AND outage_kind IS NULL",
}


# How long SQLite waits for a lock held by another connection, and how long an open waits to switch the database to WAL.
_LOCK_TIMEOUT_S = 30.0


def utc_now() -> str:
    """Return the current time as an ISO 8601 UTC string with a Z suffix."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def column_for(factor: str) -> str:
    """Return the name of the generated column that holds a factor's value."""
    return f"f_{factor}"


class Store:
    """The campaign's state in one SQLite file, safe to share between threads of one process."""

    def __init__(self, path: Path, schema: FactorSchema, lock_timeout_s: float = _LOCK_TIMEOUT_S):
        """Open or create the database for a schema's factors, finish any unfinished migration, refuse other factors.

        An open that cannot get a lock, or the WAL journal mode, within `lock_timeout_s` raises `sqlite3.OperationalError`
        naming the database.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.schema = schema
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=lock_timeout_s)
        self.db.row_factory = sqlite3.Row
        try:
            self._open(lock_timeout_s)
        except BaseException:
            self.db.close()
            raise

    def _open(self, lock_timeout_s: float) -> None:
        """Set the pragmas, create what is missing, check the factor columns, add the later columns one by one."""
        self._enter_wal(lock_timeout_s)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(self._runs_sql())
        found = [r["name"] for r in self.db.execute("PRAGMA table_xinfo(runs)") if r["hidden"] in (2, 3)]
        expected = [column_for(f) for f in self.schema.order]
        if found != expected:
            raise SchemaMismatch(f"{self.path} holds factor columns {found}, but the schema's are {expected}")
        for column in expected:
            self.db.execute(f"CREATE INDEX IF NOT EXISTS runs_{column} ON runs({column})")
        self.db.executescript(_FIXED_SQL)
        for table, column, declaration in _ADDED_COLUMNS:
            self._migrate(table, column, declaration)

    def _enter_wal(self, timeout_s: float) -> None:
        """Put the database in WAL mode, trying again while a concurrent opener is doing the same (C20).

        The switch is a write that first reads, then raises its read lock to the write lock. When another connection
        already holds the write lock, SQLite returns SQLITE_BUSY at once and does not wait out the busy timeout, since
        two connections that both waited there could deadlock. Many openers of one new database meet exactly this. A
        loser pauses a random moment and tries again; once the winner has committed, the statement changes nothing and
        reports 'wal'. The mode it reports is checked, and a database that does not reach WAL in time is refused.
        """
        deadline = time.monotonic() + timeout_s
        pause = 0.001
        while True:
            failure = None
            try:
                mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode == "wal":
                    return
                reason = f"the journal mode stayed {mode}"
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
                    raise
                failure, reason = error, str(error)
            if time.monotonic() >= deadline:
                message = f"{self.path}: could not switch the database to WAL within {timeout_s} s: {reason}"
                raise sqlite3.OperationalError(message) from failure
            time.sleep(random.uniform(0, pause))  # jitter, so the losers do not meet again in step
            pause = min(pause * 2, 0.05)

    def _runs_sql(self) -> str:
        """Return the CREATE TABLE statement of `runs`, with one generated column per factor in identity order."""
        generated = ",\n  ".join(
            f"{column_for(f)} {'INTEGER' if f == 'seed' else 'TEXT'} GENERATED ALWAYS AS (json_extract(factors, '$.{f}')) VIRTUAL"
            for f in self.schema.order
        )
        return (
            "CREATE TABLE IF NOT EXISTS runs(\n"
            "  run_key TEXT PRIMARY KEY,  -- the canonical text of the factor values\n"
            "  digest TEXT NOT NULL UNIQUE,  -- its SHA-256\n"
            "  seq INTEGER NOT NULL,  -- place in the manifest\n"
            "  factors TEXT NOT NULL CHECK(json_valid(factors)),\n"
            "  status TEXT NOT NULL DEFAULT 'pending',  -- pending|done|infra_exhausted|harness_error\n"
            f"  {generated})"
        )

    def _columns(self, table: str) -> list[str]:
        """Return the names of a table's columns, generated ones apart."""
        return [r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")]

    def _migrate(self, table: str, column: str, declaration: str) -> None:
        """Add one column if it is missing, checking and altering in one write transaction (C20).

        A column that older databases kept elsewhere is backfilled (`_BACKFILLS`) in the same transaction, so a kill
        never leaves the column added and empty.

        Two processes may open a database at once. Under BEGIN IMMEDIATE the second waits for the first, then finds the
        column there. A "duplicate column" error from a concurrent opener is taken as applied only after the column is
        seen to exist; any other failure, or a duplicate error with the column still missing, is raised.
        """

        def go() -> None:
            if column not in self._columns(table):
                self._add_column(table, column, declaration)
                if column in _BACKFILLS:
                    self.db.execute(_BACKFILLS[column])

        try:
            self.transact(go)
        except sqlite3.OperationalError as error:
            if "duplicate column name" in str(error) and column in self._columns(table):
                return
            raise

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        """Add one column; call inside a transaction (C20)."""
        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        """Close the connection; a thread inside a statement while it closes would crash the process, so wait for it."""
        with self._lock:
            self.db.close()

    # --- transactions and reads ---------------------------------------------------------------------------------

    def transact(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` inside BEGIN IMMEDIATE: commit what it returns, roll back whatever it raises."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = fn()
                self.db.execute("COMMIT")
                return result
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Run a read and return all rows."""
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Hold one read transaction, so every read inside sees the database as of the first."""
        with self._lock:
            self.db.execute("BEGIN")
            try:
                yield
            finally:
                self.db.execute("COMMIT")

    # --- campaign-level facts -----------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Return a campaign-level fact, or None."""
        rows = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def _upsert_meta(self, key: str, value: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc",
            (key, value, now),
        )

    def put_meta(self, key: str, value: str, now: str) -> None:
        """Upsert a campaign-level fact that the status process reads (not a pin: it may change)."""
        self.transact(lambda: self._upsert_meta(key, value, now))

    def put_meta_many(
        self, facts: dict[str, str], now: str, events: Iterable[tuple[str, str | None, str]] = ()
    ) -> None:
        """Upsert several facts and append events (kind, model, detail) in one transaction, so a kill never splits them."""

        def go() -> None:
            for key, value in facts.items():
                self._upsert_meta(key, value, now)
            for kind, model, detail in events:
                self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, kind, model, detail))

        self.transact(go)

    def update_meta(
        self,
        key: str,
        fn: Callable[[str | None], tuple[str | None, list[tuple[str, str | None, str]], Any]],
        now: str,
    ) -> Any:
        """Read, change and write one fact, with its events, in one BEGIN IMMEDIATE transaction (C20).

        `fn(current text or None)` returns (the new text or None to leave it, events, a result for the caller). Two
        writers of one fact, the dispatcher and a command run from a shell, never overwrite each other's step.
        """

        def go() -> Any:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            new, events, result = fn(row["value"] if row else None)
            if new is not None:
                self._upsert_meta(key, new, now)
            for kind, model, detail in events:
                self.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, kind, model, detail))
            return result

        return self.transact(go)

    def pin(self, key: str, value: str, now: str, repin: bool = False) -> str:
        """Record `value` under `key` at the first start; later it must be the same value.

        A different one raises PinMismatch unless `repin` is set (an explicit operator decision), which replaces it
        and logs an event. Returns the value in force.
        """
        return self.transact(lambda: self.pin_within(key, value, now, repin))

    def pin_within(self, key: str, value: str, now: str, repin: bool = False) -> str:
        """Do what `pin` does inside a transaction the caller has begun, so several pins and checks stand or fall together."""
        current = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if current is None:
            self.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (key, value, now))
        elif current["value"] != value:
            if not repin:
                raise PinMismatch(key, current["value"], value)
            self.db.execute("UPDATE meta SET value=?, set_utc=? WHERE key=?", (value, now, key))
            self.db.execute(
                "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
                (now, "repin", None, f"{key}: {current['value']} -> {value} (operator flag)"),
            )
        return value

    def event(self, ts: str, kind: str, model: str | None, detail: str) -> None:
        """Append one event."""
        self.transact(
            lambda: self.db.execute(
                "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (ts, kind, model, detail)
            )
        )

    # --- the manifest -------------------------------------------------------------------------------------------

    def sync_manifest(self, rows: Sequence[ManifestRow]) -> int:
        """Insert the runs the store lacks and refresh every run's place, in one transaction; return the number added."""
        return self.transact(lambda: self.sync_manifest_within(rows))

    def sync_manifest_within(self, rows: Sequence[ManifestRow]) -> int:
        """Do what `sync_manifest` does inside a transaction the caller has begun, so it can stand or fall with more.

        Statuses are never touched. Refuses a key listed twice, a key from another schema or that no condition runs, and
        a plan whose order of the runs it shares with the store has changed; the caller's transaction then rolls back.
        """
        texts = [row.key.text for row in rows]
        repeated = [text for text in dict.fromkeys(texts) if texts.count(text) > 1]
        if repeated:
            raise ValueError(f"run key {repeated[0]!r} appears more than once in the manifest")
        for row in rows:
            if row.key.names != self.schema.order:
                raise ValueError(f"run key {row.key.text!r} is not of this schema's identity order")
            self.schema.check_key(row.key)  # a key no condition runs cannot have come from this plan
        recorded = [r["run_key"] for r in self.db.execute("SELECT run_key FROM runs ORDER BY seq")]
        problem = relative_order_problem(recorded, texts)
        if problem is not None:
            raise ResumeRefused(problem)
        added = 0
        for row in rows:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO runs(run_key,digest,seq,factors) VALUES(?,?,?,?)",
                (row.key.text, row.key.digest, row.seq, row.key.factors_json),
            )
            added += cursor.rowcount
            self.db.execute("UPDATE runs SET seq=? WHERE run_key=?", (row.seq, row.key.text))
        return added

    def run_keys(self) -> set[str]:
        """Return the key of every run the store holds."""
        return {r["run_key"] for r in self.q("SELECT run_key FROM runs")}

    def pending(self, **where: str | int) -> list[sqlite3.Row]:
        """Return the pending runs in manifest order, optionally only those with the given factor values."""
        for name in where:
            if name not in self.schema.order:
                raise ValueError(f"unknown factor {name!r}; the factors are: {', '.join(self.schema.order)}")
        clause = "".join(f" AND {column_for(name)}=?" for name in where)
        return self.q(f"SELECT * FROM runs WHERE status='pending'{clause} ORDER BY seq", where.values())

    # --- attempts -----------------------------------------------------------------------------------------------

    def charged_attempts(self, run_key: str) -> int:
        """Return the attempts of a run that count against the attempt limit."""
        return self.q("SELECT COALESCE(SUM(charged),0) c FROM attempts WHERE run_key=?", (run_key,))[0]["c"]

    def begin_attempt(self, run_key: str, started: str, pid: int, run_dir: str) -> BegunAttempt:
        """Insert the 'running' row before the run starts; refuse in the transaction a run that is not pending.

        The attempt number is one more than the run's charged attempts, so an interrupted or uncharged attempt is
        run again under the number it had.
        """

        def go() -> BegunAttempt:
            run = self.db.execute("SELECT status FROM runs WHERE run_key=?", (run_key,)).fetchone()
            if run is None:
                raise AttemptRefused(f"{run_key} is not in the store")
            if run["status"] != "pending":
                raise AttemptRefused(f"{run_key} is not pending (it is {run['status']})")
            if self.db.execute("SELECT 1 FROM attempts WHERE run_key=? AND state='running'", (run_key,)).fetchone():
                raise AttemptRefused(f"{run_key} already has a running attempt")
            number = self.db.execute(
                "SELECT COALESCE(SUM(charged),0)+1 FROM attempts WHERE run_key=?", (run_key,)
            ).fetchone()[0]
            cursor = self.db.execute(
                "INSERT INTO attempts(run_key,attempt,state,started_utc,pid,run_dir) VALUES(?,?,?,?,?,?)",
                (run_key, number, "running", started, pid, run_dir),
            )
            return BegunAttempt(int(cursor.lastrowid), number)

        return self.transact(go)

    def running_attempts(self) -> list[sqlite3.Row]:
        """Return the attempts now marked running."""
        return self.q("SELECT id, run_key, run_dir FROM attempts WHERE state='running' ORDER BY id")

    def finish_attempt(self, attempt_id: int, result: AttemptResult, max_attempts: int) -> str:
        """Record how an attempt ended and move its run on, in one transaction; return the run's new status.

        The state, the charge and the treatment mark follow from the cause and the run's condition (`classify`). A run is
        done on an agent outcome or a treatment fault, exhausted when its charged attempts reach `max_attempts`, set
        aside as a harness error when our own code crashed, and otherwise pending again. An attempt that is not running is refused: its result would be recorded twice. So is a
        result whose `treatment_kinds` differ from the condition's declaration (TreatmentKindsMismatch), and the attempt
        then stays running.
        """
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        outcome = result.outcome
        detail = outcome.detail
        if outcome.defect is not None:
            detail = f"{outcome.defect.exception} {outcome.defect.stack_sha256[:12]}: {detail}"

        def go() -> str:
            row = self.db.execute(
                "SELECT a.run_key, r.factors FROM attempts a JOIN runs r USING(run_key) WHERE a.id=? AND a.state='running'",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptRefused(f"attempt {attempt_id} is not running, so its result is not recorded")
            condition = self.schema.condition_named(json.loads(row["factors"]), row["run_key"])
            declared = self.schema.treatment_kinds[condition]
            if result.treatment_kinds != declared:
                raise TreatmentKindsMismatch(
                    f"{row['run_key']}: the record carries treatment kinds {list(result.treatment_kinds)} but "
                    f"condition {condition!r} declares {list(declared)}; the attempt stays running"
                )
            state, charged, treatment = classify(result, declared)
            self.db.execute(
                "UPDATE attempts SET state=?, charged=?, treatment=?, ended_utc=?, wall_s=?, infra_kind=?, outage_kind=?,"
                " detail=?, termination=?, input_tokens=?, output_tokens=?, cache_creation_tokens=?,"
                " cache_read_tokens=?, usage_complete=? WHERE id=?",
                (
                    state,
                    int(charged),
                    int(treatment),
                    result.ended_utc,
                    result.wall_s,
                    outcome.infra_kind,
                    result.outage_kind,
                    detail,
                    outcome.name,
                    result.input_tokens,
                    result.output_tokens,
                    result.cache_creation_tokens,
                    result.cache_read_tokens,
                    int(result.usage_complete),
                    attempt_id,
                ),
            )
            status = self._status_after(row["run_key"], state, max_attempts)
            self.db.execute("UPDATE runs SET status=? WHERE run_key=?", (status, row["run_key"]))
            return status

        return self.transact(go)

    def _status_after(self, run_key: str, state: str, max_attempts: int) -> str:
        """Return the run's status once an attempt has ended in `state`; call inside a transaction."""
        if state == "done":
            return "done"
        if state == "harness_error":
            return "harness_error"
        if state == "infra_fail":
            charged = self.db.execute(
                "SELECT COALESCE(SUM(charged),0) FROM attempts WHERE run_key=?", (run_key,)
            ).fetchone()[0]
            return "infra_exhausted" if charged >= max_attempts else "pending"
        return "pending"
