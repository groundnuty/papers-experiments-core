# Provenance: new
"""An operator's pause of one model's issue, and its resume (C55): two kinds of ledger event, nothing else.

An operator stops new leases of one model at once (a provider's 429 or 5xx burst, a bad deployment, a key problem)
with `pause`, and lifts it with `resume`. Each is a row of the ledger's own `events` log (`operator_pause`,
`operator_resume`) with the model, the reason and the actor in its detail and the time as the event's: no table and no
column is added, the pinned configuration, the fingerprints and every earlier row stay as they were (SQLite's own
autoincrement counter of the events table moves, as with every event), and the schema the file declares is the one it
had. An operator pause is in force for a model while its latest such event is a
pause; the ledger reads that from the file at every lease, so a pause from another process (the operator's command,
which does not take the dispatcher's lock) is seen by the next lease.

A paused model is given no new lease and its pending runs wait (the choice never sends a federation on to a later
set); leases already out run to their end and settle as usual. The pause is independent of C48's: `resume` lifts only
the operator's, and a model that an undeclared fingerprint also pauses stays paused until `declare`.

A core that does not know these events opens such a ledger without error and leases the model as before: it reads the
events by kind and ignores the two. So the process that issues leases must run a core that does, and `pause_in_file`
(the command) reads whether the controller that holds the ledger says it does (`awareness`) and refuses to write to
one that does not.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from expcore.campaign.elastic import awareness, declarations, schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.records import LedgerError, OperatorPause, PauseRefused

PAUSE = "operator_pause"
RESUME = "operator_resume"
_KIND = {PAUSE: "pause", RESUME: "resume"}


def log(db: sqlite3.Connection) -> list[OperatorPause]:
    """Every pause and resume, oldest first (empty for a ledger no operator ever paused)."""
    rows = db.execute("SELECT seq, at, kind, detail FROM events WHERE kind IN (?, ?) ORDER BY seq", (PAUSE, RESUME))
    out = []
    for seq, at, kind, detail in rows:
        try:
            found = json.loads(detail)
            out.append(OperatorPause(seq, at, found["model"], _KIND[kind], found["reason"], found["actor"]))
        except (TypeError, ValueError, KeyError):
            raise LedgerError(
                f"event {seq} ({kind}) holds no model, reason and actor: the pause log cannot be read"
            ) from None
    return out


def held(db: sqlite3.Connection) -> dict[str, OperatorPause]:
    """Each model an operator has paused and not resumed, with the pause that holds it."""
    out: dict[str, OperatorPause] = {}
    for entry in log(db):
        if entry.kind == "pause":
            out[entry.model] = entry
        else:
            out.pop(entry.model, None)
    return out


def reasons(db: sqlite3.Connection) -> dict[str, str]:
    """Each model an operator holds paused with the reason given: what the choice reports for the model."""
    return {model: entry.reason for model, entry in held(db).items()}


def _check(db: sqlite3.Connection, model: str, reason: str, actor: str) -> tuple[str, str]:
    if not isinstance(model, str) or not model:
        raise ValueError(f"a pause needs the model's key, got {model!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if db.execute("SELECT 1 FROM runs WHERE model=? LIMIT 1", (model,)).fetchone() is None:
        raise PauseRefused(f"the ledger has no runs of model {model}: there is nothing to pause or resume")
    return reason, actor


def pause(db: sqlite3.Connection, now: float, model: str, reason: str, actor: str) -> OperatorPause:
    """Pause a model's issue in the caller's transaction and record it. Refused (`PauseRefused`) for a model the
    ledger has no runs of and for one an operator already paused; a model that only C48 pauses may be paused."""
    reason, actor = _check(db, model, reason, actor)
    already = held(db).get(model)
    if already is not None:
        raise PauseRefused(
            f"model {model} is already paused by an operator (event #{already.event_seq}, by {already.actor}: "
            f"{already.reason})"
        )
    schema.add_event(db, now, PAUSE, model=model, reason=reason, actor=actor)
    return _latest(db)


def resume(db: sqlite3.Connection, now: float, model: str, reason: str, actor: str) -> OperatorPause:
    """Lift an operator's pause of a model in the caller's transaction and record it. Refused (`PauseRefused`) for a
    model the ledger has no runs of and for one no operator paused, whatever else pauses it."""
    reason, actor = _check(db, model, reason, actor)
    if model not in held(db):
        why = f"model {model} is not paused by an operator"
        undeclared = declarations.paused(db).get(model)
        if undeclared:
            shown = ", ".join("none" if v is None else v for v in undeclared)
            why += f": an undeclared fingerprint ({shown}) pauses it, and only `declare` lifts that"
        raise PauseRefused(why)
    schema.add_event(db, now, RESUME, model=model, reason=reason, actor=actor)
    return _latest(db)


def _latest(db: sqlite3.Connection) -> OperatorPause:
    return log(db)[-1]


def pause_in_file(
    path: Path, model: str, reason: str, actor: str, *, clock: Callable[[], float] = time.time
) -> tuple[OperatorPause, str]:
    """Pause a model in the ledger file at `path` without its dispatcher lock, for an operator while a dispatcher runs:
    one transaction under the file's own write lock, which the dispatcher's next issue reads. Refused
    (`PauseRefused`, nothing written) while a live controller does not declare that it honours pauses, since it would
    keep leasing the model. Return the entry and the controller's state (`awareness`): `aware` (it honours it),
    `none` (no controller holds the ledger: the next one must honour pauses) or `unknown` (the locks could not be
    read)."""
    controller = awareness.controller_of(path)
    if controller == awareness.OLDER:
        raise PauseRefused(
            f"a controller holds this ledger and does not say it honours operator pauses: it runs a core older than "
            f"the pause and would keep leasing model {model}. Restart it on this commit or later, then pause (a "
            "controller only now starting reads as older for a moment: run the command again)"
        )
    db = schema.connect(path)
    try:
        if db.execute("SELECT value FROM meta WHERE key='config'").fetchone() is None:
            raise ValueError(f"{path} holds no ledger configuration")
        with schema.transaction(db):
            return pause(db, clock(), model, reason, actor), controller
    finally:
        db.close()


def resume_in_file(
    path: Path, model: str, reason: str, actor: str, *, clock: Callable[[], float] = time.time
) -> tuple[OperatorPause, tuple[str | None, ...]]:
    """Resume a model in the ledger file at `path` as `pause_in_file` pauses. Return the entry and what still pauses
    the model (C48's undeclared values; empty: it is not paused)."""
    db = schema.connect(path)
    try:
        if db.execute("SELECT value FROM meta WHERE key='config'").fetchone() is None:
            raise ValueError(f"{path} holds no ledger configuration")
        with schema.transaction(db):
            entry = resume(db, clock(), model, reason, actor)
            return entry, declarations.paused(db).get(model, ())
    finally:
        db.close()


__all__ = ["held", "log", "pause", "pause_in_file", "reasons", "resume", "resume_in_file"]
