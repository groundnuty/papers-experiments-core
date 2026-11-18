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
attempts reach `max_attempts` is `infra_exhausted`, however it was put back to pending. The loss's one attempt is a
conservative charge, so when later evidence (a settle, a return, a late completion) lowers the run's charged attempts
below `max_attempts` again, an `infra_exhausted` run **reopens** to pending, and the log's `reopen` entry names the
lease, its old and new counts, the run's, and why. Only the count decides, and only `infra_exhausted` reopens: a run
that is `done` or `harness_error` stays so, and a run whose real attempts reach `max_attempts` stays exhausted. Two
leases of one run can each have an attempt of one number: the lease id, which the lease's store records in every
record and export row, tells them apart.
"""

import sqlite3

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig


def log_reopen(
    db: sqlite3.Connection, config: LedgerConfig, now: float, lease: sqlite3.Row, after: int, run_before: int, how: str
) -> None:
    """Log that the run of `lease` (its row as read before the change) is below `max_attempts` again: the lease was
    counted for `lease['charged_added']` attempts and `how` (its settle, its return, its late completion) shows `after`,
    which took the run's charged attempts from `run_before` to what they are now."""
    before = lease["charged_added"]
    run = db.execute("SELECT charged FROM runs WHERE run_key=?", (lease["run_key"],)).fetchone()
    schema.add_event(
        db,
        now,
        "reopen",
        run_key=lease["run_key"],
        lease_id=lease["id"],
        federation=lease["federation"],
        lease_charged_before=before,
        lease_charged_after=after,
        run_charged_before=run_before,
        run_charged_after=run["charged"],
        max_attempts=config.max_attempts,
        reason=(
            f"lease {lease['id']} was counted for {before} charged attempt(s), but {how} shows {after}: the run is "
            f"below its {config.max_attempts} attempts again, so it is pending, not infra_exhausted"
        ),
    )


def set_charged(
    db: sqlite3.Connection, config: LedgerConfig, now: float, lease_id: int, charged: int, how: str
) -> None:
    """Count the lease into its run's charged attempts as `charged`, replacing what it was counted for, in the caller's
    transaction. A pending run that has no attempt left is ended; an `infra_exhausted` one that this leaves with an
    attempt (its loss was counted conservatively, `how` shows fewer) reopens to pending and the log says so."""
    row = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    before = db.execute("SELECT charged FROM runs WHERE run_key=?", (row["run_key"],)).fetchone()["charged"]
    if charged != row["charged_added"]:
        db.execute("UPDATE leases SET charged_added=? WHERE id=?", (charged, lease_id))
        db.execute(
            "UPDATE runs SET charged = charged + ? WHERE run_key=?", (charged - row["charged_added"], row["run_key"])
        )
    run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (row["run_key"],)).fetchone()
    if run["state"] == "pending" and run["charged"] >= config.max_attempts:
        db.execute("UPDATE runs SET state='infra_exhausted' WHERE run_key=?", (row["run_key"],))
    elif run["state"] == "infra_exhausted" and run["charged"] < config.max_attempts:
        db.execute("UPDATE runs SET state='pending' WHERE run_key=?", (row["run_key"],))
        log_reopen(db, config, now, row, charged, before, how)


def _settled_with_attempts(db: sqlite3.Connection, lease_id: int) -> bool:
    row = db.execute("SELECT charged FROM settlements WHERE lease_id=?", (lease_id,)).fetchone()
    return row is not None and row["charged"] is not None


def lose(db: sqlite3.Connection, config: LedgerConfig, now: float, lease_id: int, reported: int = 0) -> None:
    """The lease is lost: it counts one charged attempt, or the `reported` ones if its completion named more, unless it
    was counted for more already, or a settle gave its attempts from the dead store's log (which stand)."""
    if _settled_with_attempts(db, lease_id):
        return
    row = db.execute("SELECT charged_added FROM leases WHERE id=?", (lease_id,)).fetchone()
    set_charged(db, config, now, lease_id, max(row["charged_added"], 1, reported), "its loss")  # never lowers


def unstarted(db: sqlite3.Connection, config: LedgerConfig, now: float, lease_id: int) -> None:
    """The lease was returned unstarted: it cost its run no attempt, unless a settle gave attempts from its log."""
    if not _settled_with_attempts(db, lease_id):
        set_charged(db, config, now, lease_id, 0, "its return (it never started)")


def requeue(db: sqlite3.Connection, config: LedgerConfig, run_key: str) -> None:
    """Put the run back to pending, or end it if its charged attempts have reached `max_attempts`."""
    db.execute("UPDATE runs SET state='pending' WHERE run_key=?", (run_key,))
    run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (run_key,)).fetchone()
    if run["charged"] >= config.max_attempts:
        db.execute("UPDATE runs SET state='infra_exhausted' WHERE run_key=?", (run_key,))
