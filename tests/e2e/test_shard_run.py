"""The example study as one campaign on two hosts (#154): `run --shard i/N`, then check, analyze and report on each.

The stub model plays a script per run, so the campaigns meet every outcome the example plans for, and everything is
local. The same example is run three times from the start with the same configuration: once unsharded, which must
leave every store, record, export and report as it was before shards existed, and once as each of two shards.

The campaigns are built once per module, and each test asserts one thing about them.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from study_support import HEAD, Campaign, clean_environment, cli, finish, make_copy

from expcore.campaign import manifest as manifest_module
from expcore.campaign import shard_run
from expcore.campaign.shard import (
    EXPORT_KEY,
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    ShardSpec,
)
from expcore.provenance.codestamp import write_stamp
from expcore.study import pipeline
from expcore.study.config import load_study
from expcore.study.pipeline import OUTPUT_FILES, regenerate

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

SHARD_FACTS = {META_SHARD, META_STARTED, META_ENVIRONMENT, META_SHARES}
EXPORT_KEYS_OF_AN_ANALYSIS = [  # what `analyze` has always written, in this order, partial-analysis keys last
    "format",
    "factors",
    "planned_runs",
    "exhausted_runs",
    "unfinished_runs",
    "analysed_attempts",
    "discarded_usage",
    "retries_by_kind",
    "infrastructure_by_condition",
    "budget_holds",
    "kinds",
    "partial",
    "running_attempts",
    "excluded_attempts",
]
RUN_FIELDS = ("run_key", "digest", "condition", "factors", "attempt", "treatment", "infra_kind")


@dataclass
class Batch:
    """The three campaigns: the whole one, and each shard of two."""

    whole: Campaign
    one: Campaign
    two: Campaign


def two_units_each(base: Path, name: str) -> Campaign:
    """A stamped copy of the example whose unit caps (1 each) are 2, which two shards can share."""
    campaign = Campaign(make_copy(base, name))
    text = (campaign.root / "campaign.toml").read_text(encoding="utf-8")
    text = text.replace("alpha-stub = 1", "alpha-stub = 2").replace("beta-stub = 1", "beta-stub = 2")
    (campaign.root / "campaign.toml").write_text(text, encoding="utf-8")
    write_stamp(campaign.root, HEAD, load_study(campaign.root).scope)
    return campaign


def never(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("an unsharded campaign consulted something of a shard")


@pytest.fixture(scope="module")
def batch(tmp_path_factory, clean_environment) -> Batch:
    """The example run unsharded, with everything of a shard made to fail if it is consulted, and as shards 1 and 2."""
    base = tmp_path_factory.mktemp("shards")
    whole, one, two = (two_units_each(base, name) for name in ("whole", "one", "two"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pipeline, "shard_manifest", never)
        patch.setattr(pipeline, "share_settings", never)
        patch.setattr(pipeline, "check_identity", never)
        patch.setattr(manifest_module, "assign", never)
        patch.setattr(manifest_module, "in_shard", never)
        patch.setattr(shard_run, "caps_shares", never)
        patch.setattr(shard_run, "budget_shares", never)
        whole.codes["run"], _ = cli(clean_environment, "run", str(whole.root))
    finish(clean_environment, whole)
    for index, campaign in ((1, one), (2, two)):
        campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root), "--shard", f"{index}/2")
        finish(clean_environment, campaign)
    return Batch(whole, one, two)


def meta_of(campaign: Campaign) -> dict[str, str]:
    return {row["key"]: row["value"] for row in campaign.sql("SELECT key, value FROM meta")}


def entries_by_run(campaign: Campaign) -> dict[str, dict[str, Any]]:
    return {entry["run_key"]: entry for entry in campaign.read("export.json")["analysed_attempts"]}


def test_every_command_succeeds_on_the_whole_campaign_and_on_each_shard(batch):
    for campaign in (batch.whole, batch.one, batch.two):
        assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}


def test_without_a_shard_the_store_holds_no_fact_of_one_and_every_run_of_the_plan(batch):
    """Byte-identical to before shards existed: nothing of a shard is consulted (the fixture made each such call fail),
    the meta table has no shard fact, and the plan is the whole plan at its places, 0 to 21."""
    assert not SHARD_FACTS & set(meta_of(batch.whole))
    assert [tuple(r) for r in batch.whole.sql("SELECT seq FROM runs ORDER BY seq")] == [(n,) for n in range(22)]
    assert {r["status"] for r in batch.whole.sql("SELECT status FROM runs")} == {"done"}


def test_without_a_shard_the_export_has_the_keys_it_always_had_in_the_order_it_always_had_and_the_report_no_shard(
    batch,
):
    export = batch.whole.read("export.json")
    assert list(export) == EXPORT_KEYS_OF_AN_ANALYSIS
    assert EXPORT_KEY not in export
    assert "shard" not in batch.whole.report.lower()


def test_without_a_shard_every_output_regenerates_from_the_records_byte_for_byte(batch):
    out = batch.whole.root / "out"
    assert regenerate(batch.whole.root) == []
    assert all((out / name).is_file() for name in OUTPUT_FILES)


def test_c28_a_shards_store_holds_its_part_of_the_plan_at_the_whole_plans_places(batch):
    """C28: the two shards' runs are disjoint and together the whole plan, each at its seq of the whole plan."""
    places = {row["run_key"]: row["seq"] for row in batch.whole.sql("SELECT run_key, seq FROM runs")}
    parts = [
        {row["run_key"]: row["seq"] for row in c.sql("SELECT run_key, seq FROM runs")} for c in (batch.one, batch.two)
    ]
    assert all(parts)
    assert set(parts[0]).isdisjoint(parts[1])
    assert {**parts[0], **parts[1]} == places
    assert {r["status"] for c in (batch.one, batch.two) for r in c.sql("SELECT status FROM runs")} == {"done"}


def test_c28_a_shard_records_its_spec_start_identity_and_shares_in_its_store_and_every_shard_one_fingerprint(batch):
    """C28, C29, C30: the four facts the store pins, and the whole plan's fingerprint in each store."""
    for index, campaign in ((1, batch.one), (2, batch.two)):
        meta = meta_of(campaign)
        assert json.loads(meta[META_SHARD]) == ShardSpec(index, 2, f"shard-{index}").to_json()
        assert meta[META_STARTED].endswith("Z")
        assert meta[META_ENVIRONMENT] == "{}"  # the example declares no identity
        assert json.loads(meta[META_SHARES]) == {
            "unit_caps": {"alpha-stub": 1, "beta-stub": 1},
            "budget": {"max_usd": 2.5, "per_model_usd": {}},
        }
        assert meta["fingerprint"] == meta_of(batch.whole)["fingerprint"]


def test_c28_a_shards_export_has_the_shard_object_and_its_runs_are_the_shards_own(batch):
    """C28: `shard` is the last key, holds the facts the store pins, and says nothing of a host."""
    for index, campaign in ((1, batch.one), (2, batch.two)):
        export, meta = campaign.read("export.json"), meta_of(campaign)
        assert list(export) == [*EXPORT_KEYS_OF_AN_ANALYSIS, EXPORT_KEY]
        assert export[EXPORT_KEY] == {
            "spec": json.loads(meta[META_SHARD]),
            "started_utc": meta[META_STARTED],
            "environment_identity": {},
            "shares": json.loads(meta[META_SHARES]),
        }
        assert export[EXPORT_KEY]["spec"]["label"] == f"shard-{index}"
        assert export["planned_runs"] == len(campaign.planned()) == len(export["analysed_attempts"])


def test_c28_the_shards_runs_are_the_runs_of_the_whole_campaign_with_the_same_results(batch):
    """C28: per-run entries are unchanged in a shard's export, and the two shards together give the unsharded campaign's
    runs, scores included: sharding changes where a run is made, not what is made."""
    whole, scores = entries_by_run(batch.whole), batch.whole.read("scores.json")
    joined: dict[str, dict[str, Any]] = {}
    for campaign in (batch.one, batch.two):
        for run_key, entry in entries_by_run(campaign).items():
            assert set(entry) == set(whole[run_key])
            assert {name: entry[name] for name in RUN_FIELDS} == {name: whole[run_key][name] for name in RUN_FIELDS}
            assert campaign.read("scores.json")[run_key] == scores[run_key]
            joined[run_key] = entry
    assert sorted(joined) == sorted(whole)


@pytest.mark.parametrize("which", ["one", "two"])
def test_a_shards_outputs_regenerate_from_its_records_byte_for_byte(batch, which):
    assert regenerate(getattr(batch, which).root) == []
