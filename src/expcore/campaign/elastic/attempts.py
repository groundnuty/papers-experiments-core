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

**A run is leased again only once its earlier leases' counts are final** (`holds`, C68). A lease's store allows
`max_attempts - attempt_base` attempts, and its base is the run's count at its issue, so a count that rises after the
next lease is issued lets the run pass `max_attempts` across its leases. A count rises at a loss (to one) and at a
settle (to the dead store's attempts), so a pending run is held, not leased, while a lease of it is:

- `expired` with its holder not proven ended: it may still be charging attempts, and its loss is still to be counted.
  The dead-holder rule proves every holder ended in the end (at the latest its recorded job end plus KillWait);
- lost (`lost`, or `expired` with its holder proven ended) in a ledger with a budget, and neither settled with its
  charged attempts nor overdue: its settle may still name more attempts than its loss's one. A settle of its spend
  alone gives no count, and a lease is settled once, so it ends nothing: the overdue does. Without a budget nothing
  is settled, so the loss's count is final at once.

A settle that never comes (a dead store nobody can read, a site no controller reads, a spend above the grant) must not
hold its run for ever: `site_out_s` after the lease could first be settled (its loss; for a holder the controller
cancelled, its proven end plus KillWait), the sweep logs `settle_overdue` for it, and its loss's count is final from
then on. Only the controller that holds the ledger settles, so the wait counts from the ledger's open when that is
later, as expiry does: a restart leaves it a whole `site_out_s` to read the dead stores before any loss is final. A
settle that lands after that is still applied as any other.
"""

import sqlite3

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig

OVERDUE = "settle_overdue"  # the event that makes a lost lease's count final with no settle

_RUNNING = (
    "SELECT l.id, l.run_key FROM leases l JOIN federations f ON f.label = l.federation"
    " JOIN runs r ON r.run_key = l.run_key"
    " WHERE r.state = 'pending' AND l.state = 'expired' AND f.ended_at IS NULL ORDER BY l.id"
)
_UNSETTLED = (
    "SELECT l.id, l.run_key, l.federation, l.state, l.ended_at, l.charged_added, f.ended_at AS fed_ended,"
    " f.end_proof AS fed_proof, f.kill_wait_s AS fed_kill_wait"
    " FROM leases l JOIN federations f ON f.label = l.federation JOIN runs r ON r.run_key = l.run_key"
    " WHERE r.state = 'pending' AND (l.state = 'lost' OR (l.state = 'expired' AND f.ended_at IS NOT NULL))"
    " AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.lease_id = l.id AND s.charged IS NOT NULL) ORDER BY l.id"
)


def _unsettled(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """The lost leases of pending runs neither settled with their charged attempts nor overdue (a settle of the spend
    alone gives no count, and a lease is settled once, so only the overdue ends it). The log is read once for the
    overdue ones (it has no index by kind or lease, as `pauses` reads it), not once for each lease."""
    done = {row[0] for row in db.execute("SELECT lease_id FROM events WHERE kind=?", (OVERDUE,))}
    return [row for row in db.execute(_UNSETTLED) if row["id"] not in done]


def holds(db: sqlite3.Connection, config: LedgerConfig) -> dict[str, str]:
    """Each pending run that is not leased now, with why: a lease of it whose count may still rise (module
    docstring). Read from the file alone, with no clock, so a reader without the lock sees what `issue` sees."""
    held: dict[str, list[str]] = {}
    for row in db.execute(_RUNNING):
        held.setdefault(row["run_key"], []).append(
            f"lease {row['id']} expired and its holder is not proven ended: it may still charge attempts"
        )
    if config.max_usd is not None:
        for row in _unsettled(db):
            held.setdefault(row["run_key"], []).append(
                f"lease {row['id']} is lost and not settled with its attempts: its dead store may show more than its "
                f"loss counted (final at such a settle, or {config.site_out_s:g} s after it could first be settled)"
            )
    return {run_key: "; ".join(reasons) for run_key, reasons in held.items()}


def held_for_settle(db: sqlite3.Connection, config: LedgerConfig) -> set[str]:
    """The pending runs held for a lost lease's settle (the second hold, module docstring): leased the moment a settle
    gives the lease's attempts, or at its overdue. Empty without a budget."""
    return set() if config.max_usd is None else {row["run_key"] for row in _unsettled(db)}


def settleable_at(row: sqlite3.Row) -> float:
    """When a lost lease (a row of `_unsettled`) could first be settled: its loss, or its holder's proven end, plus
    KillWait for a holder the controller cancelled (`settling.apply` refuses it before then)."""
    if row["state"] == "lost":
        return row["ended_at"]
    wait = row["fed_kill_wait"] if row["fed_proof"] == "cancelled" else 0.0
    return row["fed_ended"] + wait


def overdue(db: sqlite3.Connection, config: LedgerConfig, now: float, opened_at: float) -> None:
    """Log `settle_overdue` for each lost lease of a pending run that has gone `site_out_s` with no settle of its
    charged attempts since it could first be settled, or since the ledger was opened (`opened_at`) if that is later,
    in the caller's transaction: its loss's count is final, and its run is no longer held for it."""
    if config.max_usd is None:
        return  # nothing is settled, so no lost lease holds its run
    for row in _unsettled(db):
        since = max(settleable_at(row), opened_at)  # a controller just restarted has had no tick to settle it
        if now - since < config.site_out_s:
            continue
        schema.add_event(
            db,
            now,
            OVERDUE,
            run_key=row["run_key"],
            lease_id=row["id"],
            federation=row["federation"],
            since=since,
            waited_s=config.site_out_s,
            charged=row["charged_added"],
            reason=(
                f"lease {row['id']} was not settled within {config.site_out_s:g} s of when it could first be: its "
                f"loss's {row['charged_added']} charged attempt(s) are final, and its run may be leased again"
            ),
        )


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
