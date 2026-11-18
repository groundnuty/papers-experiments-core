"""Today's forms are untouched by the elastic ledger: nothing on their path imports it, and a ledger made beside a
plan changes none of the plan's pins."""

import json
import os
import subprocess
import sys
from pathlib import Path

from campaign_support import make_fingerprint, make_manifest, make_schema
from elastic_support import FakeClock

import expcore
from expcore.campaign.elastic import Ledger, LedgerConfig, ledger_runs
from expcore.campaign.manifest import fingerprint_sha256

SRC = Path(expcore.__file__).resolve().parents[1]
TODAYS_ENTRY_POINTS = (
    "expcore.cli",
    "expcore.study.pipeline",
    "expcore.study.merge",
    "expcore.campaign.scheduler",
    "expcore.campaign.store",
    "expcore.campaign.shard_run",
    "expcore.campaign.merge",
    "expcore.campaign.merge_selection",
    "expcore.campaign.export",
)


def test_c42_no_entry_point_of_todays_forms_imports_the_elastic_ledger():
    """C42: an unsharded run, a shard, a selection, their merges and the export load no module of the ledger."""
    program = (
        "import importlib, sys\n"
        f"for name in {TODAYS_ENTRY_POINTS!r}:\n    importlib.import_module(name)\n"
        "print(sorted(m for m in sys.modules if m.startswith('expcore.campaign.elastic')))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=False, env=env)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "[]", f"loaded by today's entry points: {done.stdout.strip()}"


def test_c42_a_ledger_made_beside_a_plan_changes_neither_its_fingerprint_nor_its_order(tmp_path):
    """C42: the plan's fingerprint and its plan and order hashes are the same after a ledger is made from it, and the
    ledger writes only its own file and its lock."""
    schema = make_schema()
    manifest = make_manifest(schema, models=("m1", "m2"))
    fingerprint = make_fingerprint(schema, model_keys=("m1", "m2"))

    def pins():
        return (
            fingerprint_sha256(fingerprint),
            json.dumps(fingerprint, sort_keys=True),
            manifest.plan_sha256,
            manifest.order_sha256,
        )

    before = pins()
    config = LedgerConfig(fingerprint=fingerprint_sha256(fingerprint), hard_term_s=3600.0, prior_run_s=600.0)
    out = tmp_path / "out"
    Ledger.create(out / "ledger.sqlite", ledger_runs(manifest, schema), config, clock=FakeClock()).close()
    assert pins() == before
    names = sorted(path.name for path in out.iterdir())
    assert all(name == "campaign.lock" or name.startswith("ledger.sqlite") for name in names), names
