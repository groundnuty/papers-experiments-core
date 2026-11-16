# Provenance: new
"""Settle what a killed process left in the store (C04).

Kill safety rests on the order of three steps: the 'running' row is committed before the run starts, the outcome marker
is written when the run ends, and the store records the result in one transaction. `recover` is the one place that
repairs a kill between them, and it is safe to run again after a kill of its own.
"""

from dataclasses import dataclass
from pathlib import Path

from expcore.campaign import lost_ledger
from expcore.campaign.attempts import TreatmentKindsMismatch
from expcore.campaign.interrupted_spend import BASES, BOUND, basis_of, read_log
from expcore.campaign.markers import read_marker
from expcore.campaign.store import Store


@dataclass(frozen=True)
class Recovery:
    """What `recover` did: results committed from markers, attempts interrupted, harness-error runs set pending."""

    committed: int
    interrupted: int
    requeued: int


def recover(store: Store, now: str, max_attempts: int) -> Recovery:
    """Settle the 'running' rows. Call only while holding the campaign lock: every 'running' row is stale.

    An attempt with a marker that names it is committed from the marker and never run again. Every other running
    attempt becomes interrupted, uncharged against the attempt limit, with its usage marked incomplete, and its run
    stays pending to be run again under the same number. An attempt that kept a call log gets the basis it is charged
    on (`spend_basis`, C05): the one its log says when the store was never journaled, so it is the store that ran it,
    and `bound` when the store's ledger names a spend journal (the store may be a snapshot whose log the attempt
    outgrew, which only that journal's count can confirm, `lost_spend.reconcile`) or cannot be read. Only then do
    harness-error runs go back to pending, from the same row, their failed
    attempts kept: a kill after the marker and a kill after the metadata write leave the same state after one
    recovery, because a marker that ends as a harness error is requeued like a result that was stored. A marker that
    exists but cannot be trusted raises MarkerError and settles nothing further.
    """
    committed, orphans = 0, []
    for row in store.running_attempts():
        result = read_marker(Path(row["run_dir"]), row["run_key"], row["id"]) if row["run_dir"] else None
        if result is None:
            orphans.append(row["id"])
            continue
        try:
            store.finish_attempt(row["id"], result, max_attempts)
        except TreatmentKindsMismatch as refusal:  # a runner that built its record with other kinds: not a result
            store.event(now, "recovered_marker_refused", None, str(refusal))
            orphans.append(row["id"])
            continue
        store.event(now, "recovered_committed", None, f"{row['run_key']}: attempt {row['id']} from its marker")
        if result.legacy_marker:
            store.event(
                now,
                "legacy_marker",
                None,
                f"{row['run_key']}: attempt {row['id']} from a schema-1 marker, which has no model-turn count; "
                "the version-1 treatment rule (the declared kind alone) applied",
            )
        committed += 1
    ledger = lost_ledger.read(store)
    trusted = ledger is not None and ledger.journal is None  # never journaled: the store that ran the attempts
    interrupted = store.transact(lambda: _interrupt(store, orphans, now, trusted))
    requeued = store.transact(lambda: _requeue_harness_errors(store, now))
    return Recovery(committed, interrupted, requeued)


def _requeue_harness_errors(store: Store, now: str) -> int:
    """Set every harness-error run pending again, one event each; call inside a transaction."""
    keys = [r["run_key"] for r in store.db.execute("SELECT run_key FROM runs WHERE status='harness_error'")]
    for key in keys:
        store.db.execute("UPDATE runs SET status='pending' WHERE run_key=?", (key,))
        store.db.execute(
            "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
            (now, "harness_error_requeued", None, f"{key}: pending again from the same row"),
        )
    return len(keys)


def _interrupt(store: Store, attempt_ids: list[int], now: str, trusted: bool) -> int:
    """Turn the given running attempts into interrupted ones, each with the basis it is charged on when it kept a call
    log (`trusted`: the store's own log can be believed); call inside a transaction."""
    bases: dict[str, int] = {}
    for attempt_id in attempt_ids:
        row = store.db.execute("SELECT calls_sent, calls_usage FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        basis = None
        if row["calls_sent"] is not None:  # a log was kept: a basis is recorded, its bound unless it can be trusted
            basis = basis_of(read_log(row["calls_sent"], row["calls_usage"])) if trusted else BOUND
            bases[basis] = bases.get(basis, 0) + 1
        store.db.execute(
            "UPDATE attempts SET state='interrupted', charged=0, ended_utc=?, usage_complete=0, spend_basis=? "
            "WHERE id=?",
            (now, basis, attempt_id),
        )
    if attempt_ids:
        detail = f"{len(attempt_ids)} attempt(s)"
        if bases:
            detail += "; charged by their call logs: " + ", ".join(f"{b} {bases[b]}" for b in BASES if b in bases)
        store.db.execute(
            "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, "recovered_interrupted", None, detail)
        )
    return len(attempt_ids)
