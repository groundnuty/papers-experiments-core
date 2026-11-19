# Provenance: new
"""The elastic ledger's SQLite file: its tables, and how it is created whole and opened (C42).

It follows the store's durability rules (`campaign.store`): WAL, synchronous=FULL, foreign keys on, every change in
one BEGIN IMMEDIATE transaction. A new ledger is written under a temporary name in one transaction, synced, and renamed
into place, so a kill during creation leaves either no ledger or a whole one.

Two partial unique indexes keep the fencing rules in the file itself: a run has at most one lease in flight, and a
federation holds at most one cap slot. A lease is settled once (`settlements`), and a ceiling only goes up
(`ceilings`). A model's cap goes either way (`cap_changes`). A model's declared fingerprints only grow
(`declarations`, C48): each lease keeps the set in force at its issue (`lease_sets`), and what a lease's run saw that
its set did not declare is kept apart (`undeclared_seen`).
"""

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "expcore-ledger/1"

# The changes of a model's cap, up or down (`caps`). A ledger made before this table has none until its first change
# creates it, so the readers of it look for the table first and the pinned configuration's caps stand without it.
CAP_CHANGES_SQL = """CREATE TABLE IF NOT EXISTS cap_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, model TEXT NOT NULL,
  old_cap INTEGER,  -- NULL: the model had no cap
  new_cap INTEGER NOT NULL CHECK(new_cap >= 1), reason TEXT NOT NULL, actor TEXT NOT NULL)"""

# The declaration log (C48, `declarations`): append-only, one row per declared fingerprint of a model, NULL for "none"
# (a reply that carries no fingerprint). A ledger made before these tables keeps no log: its leases carry no declared
# set, and the kernel judges them by models.toml's, as before.
DECLARATIONS_SQL = """CREATE TABLE IF NOT EXISTS declarations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, model TEXT NOT NULL,
  fingerprint TEXT,  -- NULL: "none", a reply that carries no fingerprint
  reason TEXT NOT NULL, actor TEXT NOT NULL)"""
LEASE_SETS_SQL = """CREATE TABLE IF NOT EXISTS lease_sets(  -- the model's declared set at each lease's issue
  lease_id INTEGER PRIMARY KEY REFERENCES leases(id), declared TEXT NOT NULL)"""
UNDECLARED_SEEN_SQL = """CREATE TABLE IF NOT EXISTS undeclared_seen(  -- what a lease's completion or return reported
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, model TEXT NOT NULL,
  fingerprint TEXT,  -- NULL: a reply that carried none
  lease_id INTEGER NOT NULL REFERENCES leases(id))"""

# The platform each federation joined on (C50), when the campaign declares its platforms: the identity each of its
# runs ran under. A ledger made before the table, or one that declares none, has no rows in it.
FEDERATION_PLATFORMS_SQL = """CREATE TABLE IF NOT EXISTS federation_platforms(
  label TEXT PRIMARY KEY REFERENCES federations(label), platform TEXT NOT NULL)"""

_SQL = (
    """
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
  charged_added INTEGER NOT NULL DEFAULT 0,  -- the charged attempts counted into its run's: a loss counts one
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
CREATE TABLE settlements(  -- a lost lease's grant replaced by the spend read from its dead federation, once
  lease_id INTEGER PRIMARY KEY REFERENCES leases(id), at REAL NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('root', 'pulled')),
  spend_micro INTEGER NOT NULL CHECK(spend_micro >= 0), grant_micro INTEGER NOT NULL,
  charged INTEGER CHECK(charged >= 0),  -- the lease's charged attempts as its log gave them, NULL: not read
  CHECK(spend_micro <= grant_micro));
CREATE TABLE ceilings(  -- the raises of the dollar ceiling, the campaign's (model NULL) or a model's
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, model TEXT,  -- the pinned configuration never changes
  old_micro INTEGER NOT NULL, new_micro INTEGER NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
  CHECK(new_micro > old_micro));
"""
    + ";\n".join((CAP_CHANGES_SQL, DECLARATIONS_SQL, LEASE_SETS_SQL, UNDECLARED_SEEN_SQL, FEDERATION_PLATFORMS_SQL))
    + ";"
)


def create_file(path: Path, rows: list[tuple], meta: dict[str, str], declared: list[tuple] = ()) -> None:
    """Write a new ledger at `path` whole: its tables, its runs (run_key, seq, model, condition, set_index), its meta
    and its first declarations (at, model, fingerprint or None, reason, actor), under a temporary name, then renamed
    into place. An existing ledger is refused."""
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
        for at, model, fingerprint, reason, actor in declared:
            found = db.execute(
                "INSERT INTO declarations(at, model, fingerprint, reason, actor) VALUES(?,?,?,?,?)",
                (at, model, fingerprint, reason, actor),
            )
            detail = json.dumps({"declaration_id": found.lastrowid, "fingerprint": fingerprint, "model": model})
            db.execute("INSERT INTO events(at, kind, detail) VALUES(?, 'declare', ?)", (at, detail))
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
        found = None  # look before changing anything: a file that is not a ledger is left as it was
        with contextlib.suppress(sqlite3.DatabaseError):
            found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if found is None or found[0] != SCHEMA:
            raise ValueError(f"{path} is not a ledger of {SCHEMA}")
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":
            raise sqlite3.OperationalError(f"{path}: the journal mode stayed {mode}, not wal")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        db.close()
        raise
    return db


def add_event(
    db: sqlite3.Connection, now: float, kind: str, *, run_key=None, lease_id=None, federation=None, **detail: Any
) -> None:
    """Append a step to the log, in the caller's transaction."""
    db.execute(
        "INSERT INTO events(at, kind, run_key, lease_id, federation, detail) VALUES(?,?,?,?,?,?)",
        (now, kind, run_key, lease_id, federation, json.dumps(detail, sort_keys=True) if detail else None),
    )


@contextlib.contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    """One `BEGIN IMMEDIATE` transaction on a connection from `connect`: committed on exit, rolled back on an error."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
