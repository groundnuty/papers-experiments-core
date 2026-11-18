"""The deterministic facts of a finished campaign store, and the four forms of an existing campaign that carry them.

A change to the kernel that serves a new form (a run taken on a lease) must not move what the old forms write. The
facts here are the ones no host, clock or Python version enters: the store's schema, the names of its meta rows and the
text of the rows that pin the form, the runs and their status, each attempt's number, state and charge, the names of the
files a batch holds, and the campaign fingerprint but for its `environment` (the distributions installed on the host
that ran).
`tests/data/existing-forms-pins.json` holds them as the kernel made them before leases existed.
"""

import hashlib
import json
from pathlib import Path

from selection_support import DIRECT, run_selection
from shard_support import run_shard
from study_support import Campaign, cli, make_copy

BY_MODEL = "\n[shards]\nper_model = { alpha-stub = 1, beta-stub = 1 }\n"
PINNED_META = ("order_rule", "order_seed", "shard_spec", "shard_shares", "shard_started_utc")
FORMS = ("unsharded", "shard-2", "shard-by-model-3", "selection-4")


def run_form(form: str, base: Path, env: dict[str, str]) -> Campaign:
    """Run one existing form of the example study to its end, as the command line does."""
    if form == "unsharded":
        campaign = Campaign(make_copy(base, form))
        code, errors = cli(env, "run", str(campaign.root))
        assert code == 0, errors
        return campaign
    if form == "shard-2":
        return run_shard(base, form, env, 1)
    if form == "shard-by-model-3":
        return run_shard(base, form, env, 1, extra=BY_MODEL)
    if form == "selection-4":
        return run_selection(base, form, env, [DIRECT], "direct")
    raise ValueError(form)


def facts_of(campaign: Campaign) -> dict[str, object]:
    """What a finished store and its batch hold that no host, clock or interpreter changes."""
    meta = {row["key"]: row["value"] for row in campaign.sql("SELECT key, value FROM meta")}
    fingerprint = json.loads(meta["fingerprint"])
    fingerprint.pop("environment")
    sans_environment = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"))
    out = campaign.root / "out"
    return {
        "schema": [
            row["sql"] for row in campaign.sql("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")
        ],
        "meta_keys": sorted(meta),
        "meta": {key: meta[key] for key in PINNED_META if key in meta and key != "shard_started_utc"},
        "fingerprint_sans_environment_sha256": hashlib.sha256(sans_environment.encode("utf-8")).hexdigest(),
        "runs": [
            [row["run_key"], row["seq"], row["status"]] for row in campaign.sql("SELECT * FROM runs ORDER BY seq")
        ],
        "attempts": [
            [row["run_key"], row["attempt"], row["state"], row["charged"], row["treatment"]]
            for row in campaign.sql("SELECT * FROM attempts ORDER BY run_key, attempt, id")
        ],
        "batch_files": sorted(str(path.relative_to(out)) for path in (out / "runs").rglob("*") if path.is_file()),
    }


def collect(base: Path, env: dict[str, str]) -> dict[str, object]:
    """The facts of every existing form, by form name."""
    return {form: facts_of(run_form(form, base, env)) for form in FORMS}
