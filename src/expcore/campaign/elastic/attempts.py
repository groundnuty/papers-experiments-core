# Provenance: new
"""A run's charged attempts across its leases (#462 step 1, C42): one count, `runs.charged`, which is the next lease's
`attempt_base` and counts against `max_attempts`.

Each lease is counted into it once, and what it was counted for is kept on the lease (`charged_added`), so evidence that
turns up later replaces what the lease was counted for and never adds a second time:

- a counting completion verified counts the attempts it names;
- a lease that is **lost** with no log anybody can read (its job proven ended with no completion, or its archive
  never verified) counts one: the loss is itself one infrastructure attempt, so a run that keeps losing its federation
  reaches `max_attempts` and ends instead of being leased for ever. A completion of the run's latest lease that named
  more counts those;
- `settle` with the lease's charged attempts, as the dead store's log gives them, replaces what it was counted for.

A lost lease a return later shows unstarted is taken back to nothing. A run with no lease in flight whose charged
attempts reach `max_attempts` is `infra_exhausted`, however it was put back to pending. Two leases of one run
can each have an attempt of one number: the lease id, which the lease's store records in every record and export row,
tells them apart.
"""

import sqlite3

from expcore.campaign.elastic.config import LedgerConfig


def set_charged(db: sqlite3.Connection, config: LedgerConfig, lease_id: int, charged: int) -> None:
    """Count the lease into its run's charged attempts as `charged`, replacing what it was counted for, in the caller's
    transaction; and end a pending run that has no attempt left."""
    row = db.execute("SELECT run_key, charged_added FROM leases WHERE id=?", (lease_id,)).fetchone()
    if charged != row["charged_added"]:
        db.execute("UPDATE leases SET charged_added=? WHERE id=?", (charged, lease_id))
        db.execute(
            "UPDATE runs SET charged = charged + ? WHERE run_key=?", (charged - row["charged_added"], row["run_key"])
        )
    run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (row["run_key"],)).fetchone()
    if run["state"] == "pending" and run["charged"] >= config.max_attempts:
        db.execute("UPDATE runs SET state='infra_exhausted' WHERE run_key=?", (row["run_key"],))


def _settled_with_attempts(db: sqlite3.Connection, lease_id: int) -> bool:
    row = db.execute("SELECT charged FROM settlements WHERE lease_id=?", (lease_id,)).fetchone()
    return row is not None and row["charged"] is not None


def lose(db: sqlite3.Connection, config: LedgerConfig, lease_id: int, reported: int = 0) -> None:
    """The lease is lost: it counts one charged attempt, or the `reported` ones if its completion named more, unless it
    was counted for more already, or a settle gave its attempts from the dead store's log (which stand)."""
    if _settled_with_attempts(db, lease_id):
        return
    row = db.execute("SELECT charged_added FROM leases WHERE id=?", (lease_id,)).fetchone()
    set_charged(db, config, lease_id, max(row["charged_added"], 1, reported))


def unstarted(db: sqlite3.Connection, config: LedgerConfig, lease_id: int) -> None:
    """The lease was returned unstarted: it cost its run no attempt, unless a settle gave attempts from its log."""
    if not _settled_with_attempts(db, lease_id):
        set_charged(db, config, lease_id, 0)


def requeue(db: sqlite3.Connection, config: LedgerConfig, run_key: str) -> None:
    """Put the run back to pending, or end it if its charged attempts have reached `max_attempts`."""
    db.execute("UPDATE runs SET state='pending' WHERE run_key=?", (run_key,))
    run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (run_key,)).fetchone()
    if run["charged"] >= config.max_attempts:
        db.execute("UPDATE runs SET state='infra_exhausted' WHERE run_key=?", (run_key,))
