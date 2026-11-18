# Provenance: new
"""Settling a lost lease's charge to the spend its dead federation made (#462 step 1, C42).

A lease is charged its grant, the most it can have spent, until its spend is known. When its holder is proven ended
with no completion, or its archive never verified, the lease is lost and its whole grant stays locked in the budget.
`settle` replaces that grant with the spend read from the dead federation's call log (`root`: the surviving root's own
log; `pulled`: evidence pulled back), and the difference goes back to the budget. Reading the log is not done here
(step 4 recovers it from the journal): this is the ledger's side only, and a lease nobody can read stays charged its
grant.

A lease is settled once. The same spend and source again changes nothing; any other is refused. A spend above the
lease's grant would mean its cap failed: it is refused, and the failure is kept in the log (once for each claim), so the
lease stays charged its grant, the bound it was granted. A lease expired but not yet proven ended is not lost: its
holder may still be spending. Nor is one whose job the controller cancelled until KillWait has passed, as the job may
run on, still spending, until then.
"""

import json
import sqlite3
from typing import Any

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import SETTLE_SOURCES, LedgerError, Settlement, SettleRefused


def check(spend_micro: Any, source: Any) -> None:
    """Refuse (ValueError) a spend that is not a whole number of micro-dollars, not below zero, or a source unknown."""
    if isinstance(spend_micro, bool) or not isinstance(spend_micro, int) or spend_micro < 0:
        raise ValueError(
            f"spend_micro_usd must be a whole number of micro-dollars, not below zero, got {spend_micro!r}"
        )
    if source not in SETTLE_SOURCES:
        raise ValueError(f"source must be one of {', '.join(SETTLE_SOURCES)}, got {source!r}")


def _as_settlement(row: Any) -> Settlement:
    return Settlement(row["lease_id"], row["at"], row["source"], row["spend_micro"], row["grant_micro"])


def read(db: sqlite3.Connection) -> list[Settlement]:
    """Every settlement, by lease id."""
    return [_as_settlement(row) for row in db.execute("SELECT * FROM settlements ORDER BY lease_id")]


def apply(
    db: sqlite3.Connection, config: LedgerConfig, now: float, lease_id: int, spend_micro: int, source: str
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
        if (done["spend_micro"], done["source"]) == (spend_micro, source):
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
        "INSERT INTO settlements(lease_id, at, source, spend_micro, grant_micro) VALUES(?,?,?,?,?)",
        (lease_id, now, source, spend_micro, grant),
    )
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
    )
    return Settlement(lease_id, now, source, spend_micro, grant)
