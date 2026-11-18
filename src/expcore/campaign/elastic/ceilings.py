# Provenance: new
"""A dollar ceiling that can be raised while the campaign runs (#462 step 1, C42).

The pinned `LedgerConfig` never changes, and the ceiling is not part of the campaign fingerprint (it is not what an
agent sees), so a raise changes neither. A raise is a row of its own table (`schema`): when, whose ceiling (the
campaign's, or a model's), the old and new value, the reason and the actor. The ceiling in force is the pinned one
unless a raise says more, and the ledger reads it from the file at every lease, so a raise from another process (the
operator's command, which does not take the dispatcher's lock) is seen by the next lease.

A ceiling is never lowered, and never set below what is already committed.
"""

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig, micro_down
from expcore.campaign.elastic.records import Ceiling, CeilingRefused, Ceilings

INT64_MAX = 2**63 - 1  # what the ledger file holds in an integer column


def pinned(config: LedgerConfig) -> Ceilings:
    """The ceilings as the configuration pinned them, in whole micro-dollars rounded down."""
    return Ceilings(
        None if config.max_usd is None else micro_down(config.max_usd),
        {model: micro_down(usd) for model, usd in config.per_model_usd.items()},
    )


def effective(db: sqlite3.Connection, config: LedgerConfig) -> Ceilings:
    """The ceilings in force: the pinned ones, replaced by the latest raise of each (each is above the last)."""
    now = pinned(config)
    max_micro, per_model = now.max_micro, dict(now.per_model_micro)
    for row in db.execute("SELECT model, new_micro FROM ceilings ORDER BY id"):
        if row["model"] is None:
            max_micro = row["new_micro"]
        else:
            per_model[row["model"]] = row["new_micro"]
    return Ceilings(max_micro, per_model)


def history(db: sqlite3.Connection) -> list[Ceiling]:
    """Every raise, oldest first."""
    rows = db.execute("SELECT * FROM ceilings ORDER BY id")
    return [
        Ceiling(*(row[k] for k in ("id", "at", "model", "old_micro", "new_micro", "reason", "actor"))) for row in rows
    ]


def text_of(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must say who or why, a non-empty string, got {value!r}")
    return value.strip()


def raise_ceiling(
    db: sqlite3.Connection,
    config: LedgerConfig,
    now: float,
    new_micro: int,
    reason: str,
    actor: str,
    model: str | None = None,
) -> Ceiling:
    """Raise the campaign's ceiling (`model` None) or a model's, in the caller's transaction, and record it. Refused
    (`CeilingRefused`) when there is no ceiling to raise, when `new_micro` is not above the current one, and when it is
    below what is committed (every lease's charge, or the model's)."""
    if isinstance(new_micro, bool) or not isinstance(new_micro, int) or not 1 <= new_micro <= INT64_MAX:
        raise ValueError(
            f"new_max_micro_usd must be a whole number of micro-dollars from 1 to {INT64_MAX}, got {new_micro!r}"
        )
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    current = effective(db, config)
    if model is None:
        old, whose = current.max_micro, "the campaign's ceiling"
        if old is None:
            raise CeilingRefused("the ledger has no budget: it has no ceiling to raise")
        spent = db.execute("SELECT COALESCE(SUM(charge_micro), 0) FROM leases").fetchone()[0]
    else:
        old, whose = current.per_model_micro.get(model), f"model {model}'s ceiling"
        if old is None:
            raise CeilingRefused(f"model {model} has no ceiling of its own: there is none to raise")
        spent = db.execute("SELECT COALESCE(SUM(charge_micro), 0) FROM leases WHERE model=?", (model,)).fetchone()[0]
    if new_micro <= old:
        raise CeilingRefused(
            f"{whose} is {old} micro-dollars: {new_micro} is not above it, and a ceiling is never lowered"
        )
    if new_micro < spent:
        raise CeilingRefused(f"{whose} cannot be set to {new_micro}: below the {spent} micro-dollars already committed")
    cursor = db.execute(
        "INSERT INTO ceilings(at, model, old_micro, new_micro, reason, actor) VALUES(?,?,?,?,?,?)",
        (now, model, old, new_micro, reason, actor),
    )
    ceiling = Ceiling(cursor.lastrowid, now, model, old, new_micro, reason, actor)
    schema.add_event(
        db, now, "raise_ceiling", ceiling_id=ceiling.ceiling_id, model=model, old_micro=old, new_micro=new_micro
    )
    return ceiling


def raise_in_file(
    path: Path,
    new_micro: int,
    reason: str,
    actor: str,
    *,
    model: str | None = None,
    clock: Callable[[], float] = time.time,
) -> Ceiling:
    """Raise a ceiling of the ledger file at `path` without its dispatcher lock, for an operator while a dispatcher
    runs: the change is one transaction under the file's own write lock, and the dispatcher's next lease reads it."""
    db = schema.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if row is None:
            raise ValueError(f"{path} holds no ledger configuration")
        config = LedgerConfig.from_json(row[0])
        with schema.transaction(db):
            return raise_ceiling(db, config, clock(), new_micro, reason, actor, model)
    finally:
        db.close()
