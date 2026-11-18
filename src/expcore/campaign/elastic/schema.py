# Provenance: new
"""The elastic ledger's SQLite file: its tables, and how it is created whole and opened (C42).

It follows the store's durability rules (`campaign.store`): WAL, synchronous=FULL, foreign keys on, every change in
one BEGIN IMMEDIATE transaction. A new ledger is written under a temporary name in one transaction, synced, and renamed
into place, so a kill during creation leaves either no ledger or a whole one.

Two partial unique indexes keep the fencing rules in the file itself: a run has at most one lease in flight, and a
federation holds at most one cap slot.
"""

import contextlib
import os
import sqlite3
from pathlib import Path

SCHEMA = "expcore-ledger/1"

_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs(
  run_key TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, model TEXT NOT NULL, condition TEXT NOT NULL,
  set_index INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',  -- pending|leased|done|infra_exhausted|harness_error
  charged INTEGER NOT NULL DEFAULT 0,  -- charged attempts of its counted leases: the next lease's attempt base
  lease_id INTEGER);  -- its latest lease, the only one whose completion can count, NULL: never leased
CREATE INDEX runs_pending ON runs(state, set_index, seq);
CREATE TABLE federations(
  label TEXT PRIMARY KEY, site TEXT NOT NULL, job_ids TEXT NOT NULL, job_end_at REAL NOT NULL,
  kill_wait_s REAL NOT NULL, joined_at REAL NOT NULL,
  drained_at REAL,  -- NULL: not draining, else it gets no new lease
  ended_at REAL, end_proof TEXT);  -- NULL: its job is not proven ended
CREATE TABLE leases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- the fencing token: never reused, above every earlier one
  run_key TEXT NOT NULL REFERENCES runs(run_key), federation TEXT NOT NULL REFERENCES federations(label),
  site TEXT NOT NULL, model TEXT NOT NULL, condition TEXT NOT NULL, attempt_base INTEGER NOT NULL,
  grant_micro INTEGER NOT NULL, issued_at REAL NOT NULL, renewed_at REAL NOT NULL, counter INTEGER,
  hard_term_at REAL NOT NULL,
  state TEXT NOT NULL,  -- issued|sent|expired|reported|counted|superseded|lost|returned|cancelled
  slot INTEGER NOT NULL DEFAULT 1,  -- 1: holds its model's cap slot
  charge_micro INTEGER NOT NULL,  -- what the budget counts for it: its grant until its spend is settled
  verdict TEXT,  -- a completion read: counts|superseded
  completion TEXT,  -- the completion read, as JSON
  reported_at REAL, ended_at REAL);
CREATE UNIQUE INDEX leases_live_run ON leases(run_key) WHERE state IN ('issued', 'sent');
CREATE UNIQUE INDEX leases_slot_federation ON leases(federation) WHERE slot = 1;
CREATE INDEX leases_state ON leases(state);
CREATE TABLE estimates(
  model TEXT NOT NULL, condition TEXT NOT NULL, runs INTEGER NOT NULL, total_s REAL NOT NULL,
  PRIMARY KEY(model, condition));
CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL, run_key TEXT, lease_id INTEGER,
  federation TEXT, detail TEXT);
"""


def create_file(path: Path, rows: list[tuple], meta: dict[str, str]) -> None:
    """Write a new ledger at `path` whole: its tables, its runs (run_key, seq, model, condition, set_index) and its
    meta, under a temporary name, then renamed into place. An existing ledger is refused."""
    if path.exists():
        raise FileExistsError(f"{path} already holds a ledger: open it instead")
    scratch = path.with_name(path.name + ".creating")
    scratch.unlink(missing_ok=True)
    db = sqlite3.connect(str(scratch), isolation_level=None)
    try:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        for statement in _SQL.split(";"):
            if statement.strip():
                db.execute(statement)
        db.executemany("INSERT INTO runs(run_key, seq, model, condition, set_index) VALUES(?,?,?,?,?)", rows)
        db.executemany("INSERT INTO meta(key, value) VALUES(?,?)", sorted({**meta, "schema": SCHEMA}.items()))
        db.execute("COMMIT")
    except BaseException:
        db.close()
        scratch.unlink(missing_ok=True)
        raise
    db.close()
    os.replace(scratch, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def connect(path: Path) -> sqlite3.Connection:
    """Open an existing ledger in WAL with synchronous=FULL and foreign keys; refuse a file that is not one."""
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no ledger there")
    db = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    db.row_factory = sqlite3.Row
    try:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise sqlite3.OperationalError(f"{path}: the journal mode stayed {mode}, not wal")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        found = None
        with contextlib.suppress(sqlite3.DatabaseError):
            found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if found is None or found[0] != SCHEMA:
            raise ValueError(f"{path} is not a ledger of {SCHEMA}")
    except BaseException:
        db.close()
        raise
    return db
