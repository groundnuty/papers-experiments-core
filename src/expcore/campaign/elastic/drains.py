# Provenance: new
"""An operator's drain of one federation while a controller holds the ledger (C59).

The standing rule retires a member after repeated health-gate failures, and the operator must be able to do it while
the controller runs. `fleetcontrol.py drain` takes the ledger's dispatcher lock, which the running controller holds,
so it is refused live. This drain is written as `pause` is (C55): on the file's own write lock and not the
dispatcher's, in one `BEGIN IMMEDIATE` transaction.

It is the ledger's own drain (`Ledger.drain`): `drained_at` set on the federation's row and a `drain` event, the
operator's reason and actor in the event's detail with `by: operator`. No table or column is added, so every core reads
it as a drain. The controller reads both from the file: its dispatcher gives a drained federation no lease (each
issue checks `drained_at` in its own transaction), and its next tick retires it once no lease of it is in flight. A
lease it already holds runs to its end and counts as any other.

It is refused, writing nothing, for a federation the ledger does not hold, one already drained or proven ended, and
one past its recorded job end (that is proven, never drained: `fleetexecute.drain_refused`'s rule), all checked in the
same transaction. Like `pause`, it is refused while a live controller does not declare that it honours operator
writes (`awareness`): a controller that does not read the file between ticks must never be written beside.
"""

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from expcore.campaign.elastic import awareness, schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.records import DrainRefused

DRAIN = "drain"  # the ledger's own event kind: an operator's drain is a drain to every reader
BY_OPERATOR = "operator"


def drain(db: sqlite3.Connection, now: float, label: str, reason: str, actor: str) -> int:
    """Drain federation `label` in the caller's transaction and record it; return the event's number. Refused
    (`DrainRefused`) for a federation the ledger does not hold, one already drained or proven ended, and one whose
    recorded job end has passed."""
    if not isinstance(label, str) or not label:
        raise ValueError(f"a drain needs the federation's label in the ledger, got {label!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    row = db.execute("SELECT job_end_at, drained_at, ended_at FROM federations WHERE label=?", (label,)).fetchone()
    if row is None:
        raise DrainRefused(f"the ledger holds no federation {label}: there is nothing to drain")
    if row["ended_at"] is not None:
        raise DrainRefused(f"federation {label} is proven ended: there is nothing to drain")
    if row["drained_at"] is not None:
        raise DrainRefused(f"federation {label} is already drained")
    if now >= row["job_end_at"]:
        raise DrainRefused(f"federation {label} is past its recorded job end: it is not drained (its end is proven)")
    db.execute("UPDATE federations SET drained_at=? WHERE label=?", (now, label))
    schema.add_event(db, now, DRAIN, federation=label, by=BY_OPERATOR, reason=reason, actor=actor)
    return db.execute("SELECT max(seq) FROM events").fetchone()[0]


def drain_in_file(
    path: Path, label: str, reason: str, actor: str, *, clock: Callable[[], float] = time.time
) -> tuple[int, str]:
    """Drain federation `label` in the ledger file at `path` without its dispatcher lock, for an operator while a
    controller runs. Refused (`DrainRefused`, nothing written) while a live controller does not declare that it honours
    operator writes. Return the event's number and the controller's state (`awareness`): `aware`, `none` or
    `unknown`."""
    if not path.is_file():  # a file that is not there is a usage error before it is a controller's business
        raise FileNotFoundError(f"{path}: no ledger there")
    controller = awareness.controller_of(path)
    if controller == awareness.OLDER:
        raise DrainRefused(
            f"a controller holds this ledger and does not say it honours operator writes beside it: it runs a core "
            f"older than the operator's pause. Restart it on this commit or later, then drain {label} (a controller "
            "only now starting reads as older for a moment: run the command again)"
        )
    db = schema.connect(path)
    try:
        if db.execute("SELECT value FROM meta WHERE key='config'").fetchone() is None:
            raise ValueError(f"{path} holds no ledger configuration")
        with schema.transaction(db):
            return drain(db, clock(), label, reason, actor), controller
    finally:
        db.close()
