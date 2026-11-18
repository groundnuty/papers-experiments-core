# Provenance: new
"""Settling a lost lease's charge to the spend its dead federation made (#462 step 1, C42).

A lease is charged its grant, the most it can have spent, until its spend is known. When its holder is proven ended
with no completion, or its archive never verified, the lease is lost and its whole grant stays locked in the budget.
`settle` replaces that grant with the spend read from the dead federation's call log (`root`: the surviving root's own
log; `pulled`: evidence pulled back), and the difference goes back to the budget. Reading the log is not done here
(step 4 recovers it from the journal): this is the ledger's side only, and a lease nobody can read stays charged its
grant.

A lease is settled once. The same spend and source again changes nothing; any other is refused. Evidence read after a
settle (a completion verified, a return) never raises the charge above the settled spend (`charge_of`). A spend above
the lease's grant would mean its cap failed: it is refused, and the failure is kept in the log (once for each claim),
so the lease stays charged its grant, the bound it was granted. A lease expired but not yet proven ended is not lost:
its holder may still be spending. Nor is one whose job the controller cancelled until KillWait has passed, as the job
may run on, still spending, until then.
"""

import json
import sqlite3
from typing import Any

from expcore.campaign.elastic import attempts, schema
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import SETTLE_SOURCES, LedgerError, Settlement, SettleRefused


def check(lease_id: Any, spend_micro: Any, source: Any, attempts_charged: Any = None) -> None:
    """Refuse (ValueError) a lease id that is not a whole number from 1 (a bool is not lease 1), a spend that is not a
    whole number of micro-dollars, not below zero, or a source unknown."""
    if isinstance(lease_id, bool) or not isinstance(lease_id, int) or lease_id < 1:
        raise ValueError(f"a lease id is a whole number of at least 1, got {lease_id!r}")
    if isinstance(spend_micro, bool) or not isinstance(spend_micro, int) or spend_micro < 0:
        raise ValueError(
            f"spend_micro_usd must be a whole number of micro-dollars, not below zero, got {spend_micro!r}"
        )
    if source not in SETTLE_SOURCES:
        raise ValueError(f"source must be one of {', '.join(SETTLE_SOURCES)}, got {source!r}")
    if attempts_charged is not None and (
        isinstance(attempts_charged, bool) or not isinstance(attempts_charged, int) or attempts_charged < 0
    ):
        raise ValueError(
            f"attempts_charged must be a whole number of attempts, not below zero, got {attempts_charged!r}"
        )


def charge_of(db: sqlite3.Connection, lease_id: int, measured_micro: int) -> int:
    """What a lease is charged once its spend is measured (a verified completion, or a return). A settled lease is
    never charged more than it was settled at: money it released may be leased out again, so a higher figure read
    later would pass the budget. It is charged the lower one, and `contradicted` keeps the higher in the log."""
    row = db.execute("SELECT spend_micro FROM settlements WHERE lease_id=?", (lease_id,)).fetchone()
    return measured_micro if row is None else min(measured_micro, row["spend_micro"])


def contradicted(db: sqlite3.Connection, now: float, lease: Any, measured_micro: int, charged_micro: int) -> None:
    """Log that evidence read after a settle spent more than the settle said, which the charge does not follow."""
    schema.add_event(
        db,
        now,
        "settle_contradicted",
        run_key=lease["run_key"],
        lease_id=lease["id"],
        federation=lease["federation"],
        measured_micro=measured_micro,
        settled_micro=charged_micro,
    )


def _as_settlement(row: Any) -> Settlement:
    return Settlement(row["lease_id"], row["at"], row["source"], row["spend_micro"], row["grant_micro"], row["charged"])


def read(db: sqlite3.Connection) -> list[Settlement]:
    """Every settlement, by lease id."""
    return [_as_settlement(row) for row in db.execute("SELECT * FROM settlements ORDER BY lease_id")]


def apply(
    db: sqlite3.Connection,
    config: LedgerConfig,
    now: float,
    lease_id: int,
    spend_micro: int,
    source: str,
    attempts_charged: int | None = None,
) -> Settlement | str:
    """Settle the lease in the caller's transaction. Returns the settlement, or, for a spend above the grant, the
    reason it is refused: the failure was recorded, so the caller commits first and then raises it. Every other
    refusal raises here (`SettleRefused`, or `LedgerError` for a lease the ledger never issued)."""
    row = db.execute(
        "SELECT l.*, f.ended_at AS fed_ended, f.end_proof AS fed_proof, f.kill_wait_s AS fed_kill_wait"
        " FROM leases l JOIN federations f ON f.label = l.federation WHERE l.id=?",
        (lease_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"lease {lease_id} was never issued by this ledger")
    done = db.execute("SELECT * FROM settlements WHERE lease_id=?", (lease_id,)).fetchone()
    if done is not None:
        if (done["spend_micro"], done["source"], done["charged"]) == (spend_micro, source, attempts_charged):
            return _as_settlement(done)  # the same values again
        raise SettleRefused(
            f"lease {lease_id} is already settled at {done['spend_micro']} micro-dollars from {done['source']}: "
            f"{spend_micro} from {source} is not what it was settled with"
        )
    if config.max_usd is None:
        raise SettleRefused("the ledger has no budget: no lease holds a charge to settle")
    state = row["state"]
    if state == "expired" and row["fed_ended"] is None:
        raise SettleRefused(f"lease {lease_id} is expired but its holder is not proven ended: it may still be spending")
    if state == "expired" and row["fed_proof"] == "cancelled" and now < row["fed_ended"] + row["fed_kill_wait"]:
        raise SettleRefused(
            f"lease {lease_id}'s job was cancelled and may run on, still spending, until KillWait passes "
            f"({row['fed_kill_wait']:g} s after the cancel): settle it then"
        )
    if state not in ("expired", "lost"):
        raise SettleRefused(
            f"lease {lease_id} is {state}: only a lost lease is settled (expired with its holder proven ended, "
            "or its archive never verified)"
        )
    most = max(0, config.max_attempts - row["attempt_base"])
    if attempts_charged is not None and attempts_charged > most:
        raise SettleRefused(
            f"lease {lease_id} followed {row['attempt_base']} charged attempts of its run, so it could have charged at "
            f"most {most} of its {config.max_attempts} attempts, not {attempts_charged}: its log is not this lease's"
        )
    grant = row["grant_micro"]
    if spend_micro > grant:
        detail = {"grant_micro": grant, "source": source, "spend_micro": spend_micro}
        seen = db.execute(
            "SELECT 1 FROM events WHERE kind='cap_failed' AND lease_id=? AND detail=?",
            (lease_id, json.dumps(detail, sort_keys=True)),
        ).fetchone()
        if seen is None:
            schema.add_event(
                db, now, "cap_failed", run_key=row["run_key"], lease_id=lease_id, federation=row["federation"], **detail
            )
        return (
            f"lease {lease_id}'s spend {spend_micro} from {source} is above its grant {grant} micro-dollars: its cap "
            "failed, so it stays charged its grant and the failure is recorded"
        )
    db.execute("UPDATE leases SET charge_micro=? WHERE id=?", (spend_micro, lease_id))
    db.execute(
        "INSERT INTO settlements(lease_id, at, source, spend_micro, grant_micro, charged) VALUES(?,?,?,?,?,?)",
        (lease_id, now, source, spend_micro, grant, attempts_charged),
    )
    known = {} if attempts_charged is None else {"attempts_charged": attempts_charged}
    schema.add_event(
        db,
        now,
        "settle",
        run_key=row["run_key"],
        lease_id=lease_id,
        federation=row["federation"],
        released_micro=grant - spend_micro,
        source=source,
        spend_micro=spend_micro,
        **known,
    )
    if attempts_charged is not None:
        attempts.set_charged(db, config, lease_id, attempts_charged)
    return Settlement(lease_id, now, source, spend_micro, grant, attempts_charged)
