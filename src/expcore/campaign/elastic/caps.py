# Provenance: new
"""A model's cap on the leases it holds at once, changed while the campaign runs (#462 step 1, C42).

`LedgerConfig.caps` is pinned and never changes: it is the cap a model starts with. A change, up or down, is a row of
its own table (`schema.CAP_CHANGES_SQL`): when, which model, the old cap (None: it had none), the new one, the reason
and the actor. The cap in force is the pinned one overlaid with each model's latest change, and the ledger reads it
from the file at every lease, so a change from another process (the operator's command, which does not take the
dispatcher's lock) is seen by the next lease.

A ledger made before the table existed has none until its first change creates it: a read looks for the table first,
and without it the pinned caps stand. A lower cap never revokes or expires a lease already issued: it only stops new
ones until the model's slots are under it.
"""

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.ceilings import INT64_MAX, text_of
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import CapChange, CapRefused


def _has_table(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cap_changes'").fetchone() is not None


def effective(db: sqlite3.Connection, config: LedgerConfig) -> dict[str, int]:
    """The caps in force: the pinned ones, each replaced by the model's latest change."""
    caps = dict(config.caps)
    if _has_table(db):
        for row in db.execute("SELECT model, new_cap FROM cap_changes ORDER BY id"):
            caps[row["model"]] = row["new_cap"]
    return caps


def history(db: sqlite3.Connection) -> list[CapChange]:
    """Every change, oldest first."""
    if not _has_table(db):
        return []
    keys = ("id", "at", "model", "old_cap", "new_cap", "reason", "actor")
    return [CapChange(*(row[k] for k in keys)) for row in db.execute("SELECT * FROM cap_changes ORDER BY id")]


def set_cap(
    db: sqlite3.Connection, config: LedgerConfig, now: float, model: str, new: int, reason: str, actor: str
) -> CapChange:
    """Set a model's cap in the caller's transaction and record it. Refused (`CapRefused`) for a model the ledger has
    no runs of and for the cap the model already has; `new` is a whole number from 1 (`ValueError`)."""
    if isinstance(new, bool) or not isinstance(new, int) or not 1 <= new <= INT64_MAX:
        raise ValueError(f"a cap must be a whole number from 1, got {new!r}")
    if not isinstance(model, str) or not model:
        raise ValueError(f"a cap needs the model's key, got {model!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if db.execute("SELECT 1 FROM runs WHERE model=? LIMIT 1", (model,)).fetchone() is None:
        raise CapRefused(f"the ledger has no runs of model {model}: there is nothing to cap")
    db.execute(schema.CAP_CHANGES_SQL)  # a ledger made before the table: the first change creates it
    old = effective(db, config).get(model)
    if old == new:
        raise CapRefused(f"model {model}'s cap is already {new}")
    cursor = db.execute(
        "INSERT INTO cap_changes(at, model, old_cap, new_cap, reason, actor) VALUES(?,?,?,?,?,?)",
        (now, model, old, new, reason, actor),
    )
    change = CapChange(cursor.lastrowid, now, model, old, new, reason, actor)
    schema.add_event(db, now, "set_cap", cap_id=change.cap_id, model=model, old_cap=old, new_cap=new)
    return change


def set_in_file(
    path: Path,
    model: str,
    new: int,
    reason: str,
    actor: str,
    *,
    clock: Callable[[], float] = time.time,
) -> CapChange:
    """Set a model's cap in the ledger file at `path` without its dispatcher lock, for an operator while a dispatcher
    runs: the change is one transaction under the file's own write lock, and the dispatcher's next lease reads it."""
    db = schema.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if row is None:
            raise ValueError(f"{path} holds no ledger configuration")
        config = LedgerConfig.from_json(row[0])
        with schema.transaction(db):
            return set_cap(db, config, clock(), model, new, reason, actor)
    finally:
        db.close()
