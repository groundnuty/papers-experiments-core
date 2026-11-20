# Provenance: new
"""A run's infrastructure streak across its leases (C58): a run whose consecutive infrastructure attempts reach
`max_infra_streak` is lost by it, as `infra_streak`, and never leased again.

Today's counts miss two loops. A kind that costs no attempt (a hold) leaves its run pending, and a lease's store retries
it for as long as its job lives; and a lease that outlives its hard term is expired and its run leased again, so its
store's completion, read later, is superseded, and a superseded lease's charged attempts never reach the run's. Either
way `max_attempts` is never reached and the run takes one federation after another.

So every completion read counts, whatever its verdict (counts, superseded, or lost for an archive that never verified):
each says the streak its store's attempts end with (`attempts.infra_streak`, the worker's `infra_streak`). In the order
the ledger read them, a completion whose last attempt was done or a harness error breaks the streak, and every
completion adds its own. A lease with no completion read (in flight, expired, lost with its federation, returned or
cancelled) neither adds nor breaks it: a federation lost costs its run a charged attempt already, which `max_attempts`
bounds.

A run is ended by its streak only when it is pending: a run with a lease out waits for it, and is ended at once when it
comes back (expired, returned, lost, or counted with its run pending again). Every ledger transaction ends the pending
runs that are due (`end_due`), so no path back to pending can lease such a run again. The run's state is `infra_streak`
and the log's `infra_streak` event names its latest lease, the streak, the kind of its last attempt and the bound. A
completion of its latest lease read afterwards is superseded, as one of a done run's is.

None of this runs, and nothing is read or written, when the ledger was made without `max_infra_streak`.
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig

STREAK_ENDED = "infra_streak"
"""The run state of a run lost by its infrastructure streak, and the kind of the event that records it."""
_BREAKS = ("done", "harness_error")


def run_streak(completions: Iterable[Mapping[str, Any]]) -> tuple[int, str | None]:
    """The streak of a run whose completions, in the order the ledger read them, are `completions` (each as the ledger
    keeps it, `Completion.as_dict`): the attempts that ended as infrastructure since the last done or harness-error
    one, and the kind of the latest of them (None with no streak)."""
    count, kind = 0, None
    for completion in completions:
        if completion.get("state") in _BREAKS:
            count, kind = 0, None
        added = completion.get("infra_streak") or 0
        if added:
            count, kind = count + added, completion.get("infra_kind")
    return count, kind


def streak_of(db: sqlite3.Connection, run_key: str) -> tuple[int, str | None]:
    """The run's streak from the completions its leases' files gave, in the order they were read."""
    rows = db.execute(
        "SELECT completion FROM leases WHERE run_key=? AND completion IS NOT NULL ORDER BY reported_at, id",
        (run_key,),
    )
    return run_streak(json.loads(row[0]) for row in rows)


def end_due(db: sqlite3.Connection, config: LedgerConfig, now: float) -> list[str]:
    """End every pending run whose streak has reached `max_infra_streak`, in the caller's transaction, each with its
    `infra_streak` event; return their keys. Nothing with no bound."""
    bound = config.max_infra_streak
    if bound is None:
        return []
    rows = db.execute(
        "SELECT l.run_key, l.completion FROM leases l JOIN runs r ON r.run_key = l.run_key"
        " WHERE r.state='pending' AND l.completion IS NOT NULL ORDER BY l.reported_at, l.id"
    ).fetchall()
    by_run: dict[str, list[Mapping[str, Any]]] = {}
    for run_key, completion in rows:
        by_run.setdefault(run_key, []).append(json.loads(completion))
    ended = []
    for run_key, completions in sorted(by_run.items()):
        count, kind = run_streak(completions)
        if count < bound:
            continue
        latest = db.execute("SELECT lease_id FROM runs WHERE run_key=?", (run_key,)).fetchone()[0]
        db.execute("UPDATE runs SET state=? WHERE run_key=?", (STREAK_ENDED, run_key))
        schema.add_event(
            db,
            now,
            STREAK_ENDED,
            run_key=run_key,
            lease_id=latest,
            streak=count,
            infra_kind=kind,
            max_infra_streak=bound,
            reason=(
                f"{count} consecutive infrastructure attempts across its leases, the last of kind {kind}, reached "
                f"max_infra_streak {bound}: the run is lost by its streak and never leased again"
            ),
        )
        ended.append(run_key)
    return ended
