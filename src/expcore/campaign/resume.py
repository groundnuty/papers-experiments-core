# Provenance: new
"""Open a campaign's store for a start or a resume, refusing to continue a different experiment (C06, C08).

A start pins the order seed and the order rule and records the fingerprint. Every later start must present the same
seed and rule and a fingerprint that agrees with the record: the same tasks, models, conditions, caps, prompt and tool
hashes, scorer digest and configuration hashes. The plan may grow by whole tasks, models or conditions when the caller
says so. The checks and the pins happen in one transaction, so a refused start leaves the store as it found it.
"""

import json
from pathlib import Path

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import ORDER_RULE, Manifest, compare_fingerprints
from expcore.campaign.recovery import Recovery, recover
from expcore.campaign.store import Store

FINGERPRINT_KEY = "fingerprint"


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

    Raises ResumeRefused (PinMismatch for the order seed or rule, FingerprintMismatch for the experiment, SchemaMismatch
    for the factors) and leaves the store unchanged. The caller must hold the campaign's exclusive lock, because
    recovery treats every 'running' attempt as stale.
    """
    store = Store(path, schema)
    try:
        store.transact(lambda: _check(store, manifest, fingerprint, now, allow_growth))
        store.sync_manifest(manifest.rows)
        recovery = recover(store, now, max_attempts)
    except BaseException:
        store.close()
        raise
    return store, recovery


def _check(store: Store, manifest: Manifest, fingerprint: dict, now: str, allow_growth: bool) -> None:
    """Pin the order seed and rule and compare the fingerprint, inside one transaction."""
    store.pin_within("order_seed", str(manifest.order_seed), now)
    store.pin_within("order_rule", ORDER_RULE, now)
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
            hint=" (the plan grew; pass allow_growth to add whole tasks, models or conditions)",
        )
    store.db.execute("UPDATE meta SET value=?, set_utc=? WHERE key=?", (text, now, FINGERPRINT_KEY))
    store.db.execute(
        "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)",
        (now, "plan_grown", None, "added: " + ", ".join(comparison.grown)),
    )
