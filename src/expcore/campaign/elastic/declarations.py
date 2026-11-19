# Provenance: new
"""The declared fingerprints of each model: an append-only log in the ledger, and the pause on an undeclared one (C48).

Deployments of one checkpoint and its flags are interchangeable within a model's stochastic margins, and they join
and leave mid-campaign (a provider redeploys, a fleet adds an endpoint), so the set of fingerprints a model may be
served under is not part of the campaign fingerprint: it is this log. A row is a model, a fingerprint or "none" (NULL:
a reply that carries no fingerprint), when, why and by whom (`schema.DECLARATIONS_SQL`). Nothing is ever removed or
changed; a value already declared is refused. models.toml's `accepted_fingerprints` are the first rows, written when
the ledger is created; `declare` adds the rest, from another process under the file's own write lock while a
dispatcher holds the ledger, as `set-cap` does.

Each lease carries its model's declared set at its issue (`lease_sets`, `Lease.declared`), and its run judges every
reply against it (`campaign.identity`). What its completion or return reports seen undeclared is kept
(`undeclared_seen`), and a model with a value seen that the log does not declare is **paused**: no new lease of it is
issued, while leases already out run to their end. The pause is read from the two tables at every issue, never kept,
so it ends as soon as every value seen is declared, by whichever process declares it. A value seen and never declared
keeps its model paused.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.records import Declaration, DeclareRefused
from expcore.campaign.run_lease import declared_json, declared_of
from expcore.models.served import MIXED_FINGERPRINTS, DeclaredSet

FROM_MODELS = "models.toml"  # the actor of the declarations a ledger is created with
FROM_MODELS_REASON = "accepted_fingerprints in models.toml when the ledger was created"


def _has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def kept(db: sqlite3.Connection) -> bool:
    """Whether the ledger keeps the log: a ledger made before it has none, and its leases carry no declared set."""
    return _has(db, "declarations")


def fingerprint_of(value: Any) -> str | None:
    """A value to declare: one fingerprint exactly as an endpoint reports it, or None for "none"; ValueError for an
    empty one or several joined."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"a fingerprint is a non-empty string, got {value!r}")
    if MIXED_FINGERPRINTS in value:
        raise ValueError(f"declare one fingerprint at a time, not several joined by {MIXED_FINGERPRINTS.strip()!r}")
    return value


def first_rows(declared: Mapping[str, Iterable[str]], models: Iterable[str], at: float) -> list[tuple]:
    """The rows a ledger is created with: each planned model's models.toml fingerprints, in order."""
    planned = set(models)
    unplanned = sorted(set(declared) - planned)
    if unplanned:
        raise ValueError(f"declared fingerprints name {', '.join(unplanned)}, which the ledger has no runs of")
    return [
        (at, model, fingerprint_of(fingerprint), FROM_MODELS_REASON, FROM_MODELS)
        for model in sorted(declared)
        for fingerprint in sorted(set(declared[model]))
    ]


def history(db: sqlite3.Connection) -> list[Declaration] | None:
    """The log, oldest first; None for a ledger that keeps none."""
    if not kept(db):
        return None
    keys = ("id", "at", "model", "fingerprint", "reason", "actor")
    return [Declaration(*(row[k] for k in keys)) for row in db.execute("SELECT * FROM declarations ORDER BY id")]


def declared_set(db: sqlite3.Connection, model: str) -> DeclaredSet:
    """A model's declared set now."""
    values = [row[0] for row in db.execute("SELECT fingerprint FROM declarations WHERE model=?", (model,))]
    return DeclaredSet(tuple(sorted({v for v in values if v is not None})), None in values)


def declared_by_model(db: sqlite3.Connection) -> dict[str, tuple[str | None, ...]] | None:
    """Each model of the ledger's runs with its declared values, fingerprints in order and None ("none") last; None
    for a ledger that keeps no log."""
    if not kept(db):
        return None
    models = [row[0] for row in db.execute("SELECT DISTINCT model FROM runs ORDER BY model")]
    out = {}
    for model in models:
        found = declared_set(db, model)
        out[model] = (*found.fingerprints, *((None,) if found.none else ()))
    return out


def paused(db: sqlite3.Connection) -> dict[str, tuple[str | None, ...]]:
    """Each model with a value seen undeclared that the log does not declare, with those values in the order first
    seen: the models no lease is issued for."""
    if not _has(db, "undeclared_seen"):
        return {}
    rows = db.execute(
        "SELECT s.model, s.fingerprint FROM undeclared_seen s WHERE NOT EXISTS (SELECT 1 FROM declarations d"
        " WHERE d.model = s.model AND d.fingerprint IS s.fingerprint) GROUP BY s.model, s.fingerprint"
        " ORDER BY MIN(s.id)"
    ).fetchall()
    out: dict[str, tuple[str | None, ...]] = {}
    for model, fingerprint in rows:
        out[model] = (*out.get(model, ()), fingerprint)
    return out


def record_set(db: sqlite3.Connection, lease_id: int, model: str) -> None:
    """Keep the model's declared set at a lease's issue, in the caller's transaction (none in a ledger with no log)."""
    if kept(db):
        text = json.dumps(declared_json(declared_set(db, model)), sort_keys=True)
        db.execute("INSERT INTO lease_sets(lease_id, declared) VALUES(?,?)", (lease_id, text))


def lease_sets(db: sqlite3.Connection, lease_id: int | None = None) -> dict[int, DeclaredSet]:
    """The declared set each lease was issued with (one lease's, given `lease_id`); none in a ledger with no log."""
    if not _has(db, "lease_sets"):
        return {}
    where, args = ("WHERE lease_id=?", (lease_id,)) if lease_id is not None else ("", ())
    rows = db.execute(f"SELECT lease_id, declared FROM lease_sets {where}", args).fetchall()
    return {row[0]: declared_of(json.loads(row[1])) for row in rows}


def see(db: sqlite3.Connection, now: float, lease: Any, values: Iterable[str | None]) -> None:
    """Keep what a lease's completion or return reported seen undeclared, each value once per lease, in the caller's
    transaction, whatever the lease's verdict; `lease` is its row."""
    if not _has(db, "undeclared_seen"):
        return
    for value in dict.fromkeys(values):
        held = db.execute(
            "SELECT 1 FROM undeclared_seen WHERE lease_id=? AND fingerprint IS ?", (lease["id"], value)
        ).fetchone()
        if held is not None:
            continue
        db.execute(
            "INSERT INTO undeclared_seen(at, model, fingerprint, lease_id) VALUES(?,?,?,?)",
            (now, lease["model"], value, lease["id"]),
        )
        schema.add_event(
            db, now, "undeclared_seen", run_key=lease["run_key"], lease_id=lease["id"], model=lease["model"],
            fingerprint=value,
        )  # fmt: skip


def declare(
    db: sqlite3.Connection, now: float, model: str, fingerprint: str | None, reason: str, actor: str
) -> Declaration:
    """Declare a fingerprint of a model, or "none" (`fingerprint` None), in the caller's transaction, and record it.
    Refused (`DeclareRefused`) in a ledger that keeps no log, for a model the ledger has no runs of, and for a value
    already declared."""
    fingerprint = fingerprint_of(fingerprint)
    if not isinstance(model, str) or not model:
        raise ValueError(f"a declaration needs the model's key, got {model!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if not kept(db):
        raise DeclareRefused("this ledger was made before the declaration log: its leases are judged by models.toml")
    if db.execute("SELECT 1 FROM runs WHERE model=? LIMIT 1", (model,)).fetchone() is None:
        raise DeclareRefused(f"the ledger has no runs of model {model}: there is nothing to declare")
    held = db.execute(
        "SELECT id, actor FROM declarations WHERE model=? AND fingerprint IS ?", (model, fingerprint)
    ).fetchone()
    if held is not None:
        what = "none" if fingerprint is None else repr(fingerprint)
        raise DeclareRefused(f"model {model} has {what} declared already (declaration #{held[0]} by {held[1]})")
    cursor = db.execute(
        "INSERT INTO declarations(at, model, fingerprint, reason, actor) VALUES(?,?,?,?,?)",
        (now, model, fingerprint, reason, actor),
    )
    found = Declaration(cursor.lastrowid, now, model, fingerprint, reason, actor)
    schema.add_event(db, now, "declare", declaration_id=found.declaration_id, model=model, fingerprint=fingerprint)
    return found


def declare_in_file(
    path: Path,
    model: str,
    fingerprint: str | None,
    reason: str,
    actor: str,
    *,
    clock: Callable[[], float] = time.time,
) -> tuple[Declaration, tuple[str | None, ...]]:
    """Declare in the ledger file at `path` without its dispatcher lock, for an operator while a dispatcher runs: one
    transaction under the file's own write lock, which the dispatcher's next issue reads. Return the declaration and
    what still pauses its model (empty: the model is not paused)."""
    db = schema.connect(path)
    try:
        if db.execute("SELECT value FROM meta WHERE key='config'").fetchone() is None:
            raise ValueError(f"{path} holds no ledger configuration")
        with schema.transaction(db):
            found = declare(db, clock(), model, fingerprint, reason, actor)
            return found, paused(db).get(model, ())
    finally:
        db.close()


__all__ = [
    "FROM_MODELS",
    "declare",
    "declare_in_file",
    "declared_by_model",
    "declared_set",
    "first_rows",
    "history",
    "kept",
    "lease_sets",
    "paused",
    "record_set",
    "see",
]
