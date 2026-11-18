"""C43: an elastic campaign merged by run, from its ledger and its lease stores, and checked, analysed and reported.

The fleet is the example study leased run by run through a real ledger to federations at two sites, each lease run
for real in a store of its own (`merge_by_run_support`). The merge must take exactly one counted lease per run into the
batch, keep a superseded lease's store and a lost lease's store as evidence (the lost one settled in the merge's own
copy), charge a lease that left nothing its grant, and refuse, naming the lease or the run, every set of stores that is
not exactly that. The merged batch is then checked, analysed and reported like any batch, with each run's federation
and site in its export, the balance over them, and the federation rerun beside the primary result.
"""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from merge_by_run_support import ENVIRONMENT, HEALTHY, Fleet, build_fleet, by_run_study, make_running
from shard_support import make_study, write_meta
from study_support import Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.by_run_layout import EVIDENCE_DIR, LEASES_DIR, MERGE_BY_RUN_SCHEMA
from expcore.campaign.elastic.config import usd_of
from expcore.campaign.export_join import FEDERATION_KEY, LEASE_KEY, SITE_KEY
from expcore.campaign.interrupted_spend import COPIED_BACK_KEY
from expcore.campaign.shard import META_ENVIRONMENT, NOT_FINAL_FILE
from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.records.record import is_record_name
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("by-run")


@pytest.fixture(scope="module")
def fleet(base, clean_environment) -> Fleet:
    return build_fleet(base, clean_environment)


@pytest.fixture(scope="module")
def merged(base, fleet) -> Campaign:
    """Every counted store, the superseded one and the lost one, merged, checked, analysed and reported."""
    campaign = Campaign(by_run_study(base, "merged"))
    campaign.codes["merge"], errors = fleet.merge(campaign, fleet.outs(fleet.superseded, fleet.lost))
    assert campaign.codes["merge"] == 0, errors
    finish(fleet.env, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign, unmerged: the scores the merged batch must give. It does not declare the
    federation rerun, which only a batch merged by run runs (S19)."""
    campaign = Campaign(make_study(base, "whole"))
    campaign.codes["run"], errors = cli(clean_environment, "run", str(campaign.root))
    assert campaign.codes["run"] == 0, errors
    return campaign


def document_of(campaign: Campaign) -> dict:
    return campaign.read("merge.json")


def refused(fleet: Fleet, base: Path, name: str, outs: list[str], ledger: Path | None = None) -> str:
    """Merge into a fresh copy and expect a refusal that leaves nothing behind; return its message."""
    target = Campaign(by_run_study(base, name))
    code, errors = fleet.merge(target, outs, ledger)
    assert code == 1, errors
    assert not (target.root / "out").exists() or not any((target.root / "out").iterdir())
    return errors


def records(root: Path) -> dict[str, str]:
    """Every record file under a runs/ directory, by path relative to it, with its SHA-256."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.json"))
        if path.parent != root and "attempt-marker" not in path.name
    }


# --- the union ------------------------------------------------------------------------------------------------------


def test_c43_the_batch_holds_exactly_the_records_of_each_runs_counted_lease(merged, fleet):
    """C43: runs/ is the union of the counted stores' records, one run each, and nothing of the evidence."""
    union: dict[str, str] = {}
    for campaign in fleet.counted():
        own = records(campaign.runs)
        assert not set(own) & set(union)
        union |= own
    assert records(merged.runs) == union
    document = document_of(merged)
    assert document["schema"] == MERGE_BY_RUN_SCHEMA
    plan = [row.key.text for row in load_study(merged.root).manifest.rows]
    assert [entry["run_key"] for entry in document["leases"]] == plan
    assert document["runs"] == len(plan) == len(fleet.counted())


def test_c43_the_merged_batch_scores_what_one_driver_of_the_whole_campaign_scores(merged, whole, fleet):
    """C43: the analysis of the merged batch gives every run the score one driver gives it."""
    finish(fleet.env, whole)
    assert merged.read("scores.json") == whole.read("scores.json")


def test_c43_each_analysed_run_names_its_counted_lease_federation_and_site(merged, fleet):
    """C43: the export carries the lease, federation and site of every run, from the ledger, and no evidence lease."""
    export = merged.read("export.json")
    leases = {entry["run_key"]: entry for entry in document_of(merged)["leases"]}
    entries = [e for name in ("analysed_attempts", "exhausted_runs", "unfinished_runs") for e in export[name]]
    assert len(entries) == export["planned_runs"] == len(leases)
    for entry in entries:
        lease = leases[entry["run_key"]]
        assert (entry[LEASE_KEY], entry[FEDERATION_KEY], entry[SITE_KEY]) == (
            lease["lease_id"],
            lease["federation"],
            lease["site"],
        )
        assert entry[FEDERATION_KEY] in HEALTHY
        assert entry[SITE_KEY] == HEALTHY[entry[FEDERATION_KEY]]
    assert {fleet.superseded, fleet.lost} & {entry[LEASE_KEY] for entry in entries} == set()
    assert "shards" not in export
    assert export["leases"] == document_of(merged)["leases"]


# --- the evidence ---------------------------------------------------------------------------------------------------


def test_c43_a_superseded_lease_is_evidence_with_its_spend_counted(merged, fleet):
    """C43: its snapshot is kept under evidence/, it is never in leases/ or runs/, and its spend counts."""
    evidence = {entry["lease_id"]: entry for entry in document_of(merged)["evidence"]}
    entry = evidence[fleet.superseded]
    assert entry["reason"] == "superseded"
    assert entry["snapshot_sha256"] is not None
    assert (merged.root / "out" / EVIDENCE_DIR / str(fleet.superseded) / SNAPSHOT_FILE).is_file()
    assert not (merged.root / "out" / LEASES_DIR / str(fleet.superseded)).exists()
    assert entry["spend_usd"] is not None


def test_c43_a_lost_lease_whose_store_came_back_running_is_settled_in_the_merges_copy(merged, fleet):
    """C43: the evidence snapshot has the attempt interrupted and uncharged, marked copied back (its call log never
    trusted), and the store given is not written: its attempt is still running there."""
    entry = {e["lease_id"]: e for e in document_of(merged)["evidence"]}[fleet.lost]
    assert entry["reason"] == "expired"
    snapshot = sqlite3.connect(merged.root / "out" / EVIDENCE_DIR / str(fleet.lost) / SNAPSHOT_FILE)
    try:
        states = snapshot.execute("SELECT state, charged FROM attempts ORDER BY id").fetchall()
        copied = snapshot.execute("SELECT value FROM meta WHERE key=?", (COPIED_BACK_KEY,)).fetchone()
    finally:
        snapshot.close()
    assert states[-1] == ("interrupted", 0)
    assert copied is not None
    original = fleet.stores[fleet.lost].sql("SELECT state FROM attempts ORDER BY id")
    assert original[-1]["state"] == "running"
    assert entry["spend_usd"] is not None


def test_c43_a_lost_lease_that_left_nothing_is_charged_its_grant(merged, fleet):
    """C43: no snapshot, and its spend is its grant, as the ledger charges it."""
    entry = {e["lease_id"]: e for e in document_of(merged)["evidence"]}[fleet.vanished]
    assert (entry["reason"], entry["snapshot_sha256"], entry["started_utc"]) == ("expired", None, None)
    ledger = sqlite3.connect(fleet.ledger_file)
    try:
        grant, charge = ledger.execute(
            "SELECT grant_micro, charge_micro FROM leases WHERE id=?", (fleet.vanished,)
        ).fetchone()
    finally:
        ledger.close()
    assert grant == charge > 0
    assert entry["spend_usd"] == usd_of(grant)


# --- the analysis ---------------------------------------------------------------------------------------------------


def test_c43_the_analysis_reports_the_balance_and_the_federation_rerun_beside_the_primary(merged):
    """C43: analysis.json has the primary contrasts as ever, the balance and the federation rerun apart from them, and
    the report has both sections."""
    analysis = merged.read("analysis.json")
    assert analysis["contrasts"]
    assert analysis["sensitivity_reruns"] == ["federation"]
    balance = analysis["balance"]
    assert balance["runs"] == merged.read("export.json")["planned_runs"]
    for model in balance["models"]:
        assert sum(n for counts in model["by_site"].values() for n in counts.values()) == model["runs"]
        assert set().union(*(counts for counts in model["by_site"].values())) <= {"site-a", "site-b"}
    federation = analysis["federation"]
    assert set(federation) == {"fits", "unadjusted", "sensitivity", "sensitivity_refused"}
    assert "## Balance over sites and federations" in merged.report
    assert "## Sensitivity analysis: site and federation effects" in merged.report


def test_c43_a_merged_by_run_batch_whose_merge_json_was_edited_is_refused_by_the_check(base, merged, fleet):
    """C43: merge.json is rebuilt from the snapshots and the ledger; a federation label changed after the merge is
    refused, naming the field."""
    copy = copy_campaign(merged, base, "edited")
    path = copy.root / "out" / "merge.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["leases"][0]["federation"] = "fed-b1" if document["leases"][0]["federation"] != "fed-b1" else "fed-a1"
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    code, errors = cli(fleet.env, "check", str(copy.runs))
    assert code == 1
    assert "differs in leases" in errors


# --- each refusal ---------------------------------------------------------------------------------------------------


def test_c43_a_run_whose_counted_store_is_missing_is_refused(base, fleet):
    """C43: no run is missing: the store of a run's counted lease must be given."""
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "missing", outs[1:])
    assert "the store of its counted lease" in errors


def test_c43_a_store_given_twice_is_refused(base, fleet):
    """C43: none twice: one lease's store given twice is refused."""
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "twice", [*outs, outs[0]])
    assert "given twice" in errors


def test_c43_a_superseded_store_left_out_is_refused(base, fleet):
    """C43: the ledger verified a superseded lease's archive, so its store is evidence the merge keeps."""
    errors = refused(fleet, base, "no-evidence", fleet.outs())
    assert f"lease {fleet.superseded} " in errors
    assert "give it, as the evidence it is" in errors


def test_c43_a_store_of_another_campaign_is_refused(base, fleet):
    """C43: a store whose fingerprint is not the others' is refused, naming it."""
    copy = copy_campaign(fleet.counted()[0], base, "foreign")
    meta = {row["key"]: row["value"] for row in copy.sql("SELECT key, value FROM meta")}
    fingerprint = json.loads(meta["fingerprint"])
    fingerprint["seeds"] = [*fingerprint.get("seeds", []), 99]
    write_meta(copy.root / "out" / "state.sqlite", {"fingerprint": json.dumps(fingerprint, sort_keys=True)})
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "other-campaign", [str(copy.root / "out"), *outs[1:]])
    assert "ran another campaign" in errors


def test_c43_a_store_of_another_environment_is_refused(base, fleet):
    """C43: a store that ran against another environment identity is refused, naming it."""
    copy = copy_campaign(fleet.counted()[0], base, "elsewhere")
    other = {"layout": hashlib.sha256(b"another layout").hexdigest(), "platform": ENVIRONMENT}
    write_meta(copy.root / "out" / "state.sqlite", {META_ENVIRONMENT: json.dumps(other)})
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "other-environment", [str(copy.root / "out"), *outs[1:]])
    assert "another environment" in errors


def test_c43_a_ledger_whose_tables_are_not_what_its_events_give_is_refused(base, fleet):
    """C43: the ledger is a hashed input that is never trusted: a run pointed at another lease is refused."""
    tampered = base / "tampered-ledger" / "ledger.sqlite"
    tampered.parent.mkdir()
    shutil.copyfile(fleet.ledger_file, tampered)
    db = sqlite3.connect(tampered)
    try:
        db.execute("UPDATE leases SET state='counted' WHERE id=?", (fleet.superseded,))
        db.commit()
    finally:
        db.close()
    errors = refused(fleet, base, "tampered", fleet.outs(fleet.superseded), tampered)
    assert "not what its events give" in errors


def test_c43_stores_of_another_environment_than_the_ledgers_are_refused(base, fleet):
    """C43: every store agrees with the others, and all ran against another environment than the one the ledger
    pinned at its first join: refused."""
    pinned = base / "other-environment-ledger" / "ledger.sqlite"
    pinned.parent.mkdir()
    shutil.copyfile(fleet.ledger_file, pinned)
    db = sqlite3.connect(pinned)
    try:
        db.execute("UPDATE meta SET value=? WHERE key='environment'", (hashlib.sha256(b"elsewhere").hexdigest(),))
        db.commit()
    finally:
        db.close()
    errors = refused(fleet, base, "ledger-environment", fleet.outs(fleet.superseded), pinned)
    assert "another environment than the ledger's" in errors


def test_c43_a_counted_store_without_its_final_record_is_refused(base, fleet):
    """C43: the record of a counted lease's final attempt is the run's only one; a store that lost it is refused."""
    copy = copy_campaign(fleet.counted()[0], base, "no-record")
    for path in copy.runs.rglob("*.json"):
        if is_record_name(path.name):
            path.unlink()
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "record-missing", [str(copy.root / "out"), *outs[1:]])
    assert "the record of a counted lease's final attempt is missing" in errors


def test_c43_a_record_added_to_a_merged_batch_is_refused_by_the_check(base, merged, fleet):
    """C43: the check replays exactly the counted leases' records; one no counted store accounts for is refused."""
    copy = copy_campaign(merged, base, "stray-record")
    first = next(path for path in sorted(copy.runs.rglob("*.json")) if is_record_name(path.name))
    stray = first.parent.with_name("try9")
    shutil.copytree(first.parent, stray)
    code, errors = cli(fleet.env, "check", str(copy.runs))
    assert code == 1
    assert "a record no attempt of a counted lease's store accounts for" in errors


def test_c43_a_campaign_with_a_run_not_yet_counted_is_refused(base, fleet):
    """C43: every run of the plan must have its counted lease in the ledger."""
    errors = refused(fleet, base, "pending", [str(fleet.stores[fleet.superseded].root / "out")], fleet.pending_copy)
    assert "a campaign is merged by run once every run has its counted lease" in errors


def test_c43_a_store_marked_not_final_is_refused(base, fleet):
    """C43: a store a driver's check marked not final is refused, as a shard is."""
    copy = copy_campaign(fleet.counted()[0], base, "not-final-store")
    (copy.root / "out" / NOT_FINAL_FILE).write_text(json.dumps({"reason": "a check did not pass"}), encoding="utf-8")
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "not-final", [str(copy.root / "out"), *outs[1:]])
    assert "marked not final" in errors


def test_c43_a_counted_store_with_a_running_attempt_is_refused(base, fleet):
    """C43: only a lost or expired lease's store is settled in the merge's copy; a counted one must have ended."""
    copy = copy_campaign(fleet.counted()[0], base, "still-running")
    make_running(copy)
    outs = fleet.outs(fleet.superseded)
    errors = refused(fleet, base, "running", [str(copy.root / "out"), *outs[1:]])
    assert "only a lost or expired lease's store is settled" in errors
