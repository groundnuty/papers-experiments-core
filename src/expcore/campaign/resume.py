# Provenance: new
"""Open a campaign's store for a start or a resume, refusing to continue a different experiment (C06, C08).

A start pins the order seed and the order rule and records the fingerprint. Every later start must present the same
seed and rule and a fingerprint that agrees with the record: the same tasks, models, conditions, caps, prompt and tool
hashes, scorer digest and configuration hashes. The plan may grow by whole tasks, models or conditions when the caller
says so. The planned run set must be exactly the one the fingerprint declares (tasks x models x seeds x conditions),
and against the store it must be the same set, or a superset when growth is allowed; a run once planned is never
dropped. The pins, the fingerprint, the check of the run set and the insertion of the runs happen in one transaction,
so a refused start leaves the store as it found it.
"""

import json
from pathlib import Path

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import ORDER_RULE, Manifest, compare_fingerprints, plan_problem
from expcore.campaign.recovery import Recovery, recover
from expcore.campaign.store import Store

FINGERPRINT_KEY = "fingerprint"


class RunSetMismatch(ResumeRefused):
    """The runs a start plans are not the runs the fingerprint declares, or not the runs the store holds."""


class FingerprintMismatch(ResumeRefused):
    """A start's fingerprint differs from the recorded one; `differences` names each path that does."""

    def __init__(self, differences: tuple[str, ...], hint: str = ""):
        lines = "\n  ".join(differences)
        super().__init__(
            f"this start differs from the experiment recorded in the store; a campaign never resumes into a different "
            f"experiment (start a new store for a new one){hint}:\n  {lines}"
        )
        self.differences = differences


def open_campaign(
    path: Path,
    schema: FactorSchema,
    manifest: Manifest,
    fingerprint: dict,
    *,
    now: str,
    max_attempts: int,
    allow_growth: bool = False,
) -> tuple[Store, Recovery]:
    """Open the store at `path`, check the start against what it records, add the plan's runs and settle a kill.

    Raises ResumeRefused (PinMismatch for the order seed or rule, FingerprintMismatch for the experiment,
    RunSetMismatch for the runs, SchemaMismatch for the factors) and leaves the store unchanged. The caller must hold
    the campaign's exclusive lock, because recovery treats every 'running' attempt as stale.
    """
    problem = plan_problem(schema, manifest, fingerprint)
    if problem is not None:
        raise RunSetMismatch(problem)
    store = Store(path, schema)
    try:
        store.transact(lambda: _start(store, manifest, fingerprint, now, allow_growth))
        recovery = recover(store, now, max_attempts)
    except BaseException:
        store.close()
        raise
    return store, recovery


def _start(store: Store, manifest: Manifest, fingerprint: dict, now: str, allow_growth: bool) -> None:
    """Pin, compare the fingerprint, check the run set and insert the runs; the caller's transaction holds it all."""
    store.pin_within("order_seed", str(manifest.order_seed), now)
    store.pin_within("order_rule", ORDER_RULE, now)
    _check_fingerprint(store, fingerprint, now, allow_growth)
    _check_run_set(store, manifest, allow_growth)
    store.sync_manifest_within(manifest.rows)


def _check_fingerprint(store: Store, fingerprint: dict, now: str, allow_growth: bool) -> None:
    """Record the first fingerprint, or compare with it and, when the caller allows growth, record the grown one."""
    text = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), allow_nan=False)
    row = store.db.execute("SELECT value FROM meta WHERE key=?", (FINGERPRINT_KEY,)).fetchone()
    if row is None:
        store.db.execute("INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)", (FINGERPRINT_KEY, text, now))
        return
    comparison = compare_fingerprints(json.loads(row["value"]), fingerprint)
    if comparison.refused:
        raise FingerprintMismatch(comparison.refused)
    if not comparison.grown:
        return
    if not allow_growth:
        raise FingerprintMismatch(
            tuple(f"{item}: not in the recorded experiment" for item in comparison.grown),
            hint=" (the plan grew; pass allow_growth to add whole tasks, models, seeds or conditions)",
        )
    store.db.execute("UPDATE meta SET value=?, set_utc=? WHERE key=?", (text, now, FINGERPRINT_KEY))
    store.db.execute(
        "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
        (now, "plan_grown", None, "added: " + ", ".join(comparison.grown)),
    )


def _check_run_set(store: Store, manifest: Manifest, allow_growth: bool) -> None:
    """Refuse a plan that drops a run the store holds, or adds runs without leave; a new store holds none."""
    held, planned = store.run_keys(), {row.key.text for row in manifest.rows}
    if not held:
        return
    removed, added = sorted(held - planned), sorted(planned - held)
    if removed:
        raise RunSetMismatch(f"the plan omits {len(removed)} run(s) the store holds, the first {removed[0]!r}")
    if added and not allow_growth:
        raise RunSetMismatch(
            f"the plan adds {len(added)} run(s) the store does not hold, the first {added[0]!r}; pass allow_growth"
        )
