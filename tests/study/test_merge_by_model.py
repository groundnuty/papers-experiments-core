"""A campaign split by model (`expcore-shard/3`, #154, C32), its shards run for real and merged into one batch.

The example study declares `[shards] per_model = { alpha-stub = 2, beta-stub = 1 }`: shards 1 and 2 serve alpha-stub,
shard 3 serves beta-stub. Each shard is run by the command line (`shard_support.run_shard`), so its plan, store meta,
caps and budget are what the run side gives; the whole campaign is run once by one driver from the same campaign.toml,
`[shards]` included, so the two have one fingerprint and the merged batch can be compared with it byte for byte.
"""

import json
from pathlib import Path

import pytest
from shard_support import (
    MERGE_TIMES,
    MERGED_ONLY,
    json_meta,
    keys_of_shard,
    make_study,
    merge_into,
    meta_of,
    plant_run,
    run_shard,
    without_run,
    without_shard_section,
)
from study_support import Campaign, clean_environment, cli, finish

from expcore.campaign.deployments import ACCEPTED_META
from expcore.campaign.shard import MERGE_FILE, META_SHARD, META_SHARES, RULE_BY_MODEL
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXTRA = "\n[shards]\nper_model = { alpha-stub = 2, beta-stub = 1 }\n"
SERVES = {1: "alpha-stub", 2: "alpha-stub", 3: "beta-stub"}


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("merge-by-model")


@pytest.fixture(scope="module")
def whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign, from the campaign.toml the shards have: the reference."""
    campaign = Campaign(make_study(base, "whole", EXTRA))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def shards(base, clean_environment) -> dict[int, Campaign]:
    return {i: run_shard(base, f"shard-{i}", clean_environment, i, count=3, extra=EXTRA) for i in SERVES}


@pytest.fixture(scope="module")
def merged(base, clean_environment, shards) -> Campaign:
    campaign = Campaign(make_study(base, "merged", EXTRA))
    campaign.codes["merge"], errors = merge_into(clean_environment, campaign, *shards.values())
    assert campaign.codes["merge"] == 0, errors
    finish(clean_environment, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


def read(campaign: Campaign, name: str):
    return json.loads((campaign.root / "out" / name).read_text(encoding="utf-8"))


def test_c32_each_shard_by_model_ran_its_models_runs_only_one_at_a_time(shards):
    """C32: shard i ran the runs of its model that the deal gave it, all of them, under expcore-shard/3, with a cap of
    one run on its model's unit; the alpha-stub shards split that model's pairs."""
    held = {}
    for index, shard in shards.items():
        assert json_meta(shard, META_SHARD)["rule"] == RULE_BY_MODEL
        assert json_meta(shard, META_SHARES)["unit_caps"] == {SERVES[index]: 1}
        rows = shard.sql("SELECT run_key, factors FROM runs")
        runs = {row["run_key"]: json.loads(row["factors"])["model"] for row in rows}
        assert set(runs.values()) == {SERVES[index]}
        assert sorted(runs) == sorted(keys_of_shard(shard.root, index, 3))
        held[index] = set(runs)
    assert held[1]
    assert held[2]
    assert not held[1] & held[2]
    plan = load_study(shards[1].root).manifest
    assert {row.key.text for row in plan.rows} == held[1] | held[2] | held[3]


def test_c32_a_campaign_split_by_model_and_merged_gives_the_scores_and_analysis_of_one_driver(whole, merged):
    """C32: scores and analysis of the merged batch are byte for byte one driver's; the export differs only in the
    fields of a shard's own dispatch sequence, the shard tags and what only a merge has (C29: `shard_retries_by_kind`,
    and each analysed entry's `wall_s` and `turn_latency_s`), the report only in the median dispatch position and the
    `## Shards` section only a merged batch has."""
    assert (merged.root / "out" / "scores.json").read_bytes() == (whole.root / "out" / "scores.json").read_bytes()
    assert (merged.root / "out" / "analysis.json").read_bytes() == (whole.root / "out" / "analysis.json").read_bytes()
    one, many = read(whole, "export.json"), read(merged, "export.json")
    own = {"shard", "dispatch_seq", "started_at", "lease_wait_s", *MERGE_TIMES}
    for key in one:
        if key != "analysed_attempts":
            assert many[key] == one[key], key
    plain = lambda entries: [{k: v for k, v in e.items() if k not in own} for e in entries]  # noqa: E731
    assert plain(many["analysed_attempts"]) == plain(one["analysed_attempts"])
    assert set(many) - set(one) == set(MERGED_ONLY)
    lines_one, lines_many = whole.report.splitlines(), without_shard_section(merged.report).splitlines()
    assert len(lines_one) == len(lines_many)
    for a, b in zip(lines_many, lines_one, strict=True):
        assert a == b or (a.startswith("| ") and a.rsplit(" | ", 1)[0] == b.rsplit(" | ", 1)[0])


def test_c32_the_merge_names_the_rule_by_model_and_checks_each_run_by_its_assignment(
    base, clean_environment, merged, shards
):
    """C32: merge.json names expcore-shard/3 and 3 shards; a run of beta-stub's shard moved into an alpha-stub shard is
    refused, naming the shard it belongs to under the deal by model."""
    document = json.loads((merged.root / "out" / MERGE_FILE).read_text(encoding="utf-8"))
    assert (document["rule"], document["count"]) == (RULE_BY_MODEL, 3)
    foreign = keys_of_shard(shards[3].root, 3, 3)[0]
    moved = plant_run(shards[1], shards[3], foreign, base, "shard-1-plus")
    lacking = without_run(shards[3], foreign, base, "shard-3-minus")
    target = Campaign(make_study(base, "refused", EXTRA))
    code, errors = merge_into(clean_environment, target, moved, shards[2], lacking)
    assert code == 1, errors
    assert f"shard 1 (shard-1): run {foreign} belongs to shard 3 (shard-3), not to this shard" in errors


# --- only some models declare accepted fingerprints (B18): the shards' exports differ in `deployment_shares` ----------


@pytest.fixture(scope="module")
def declared_whole(base, clean_environment) -> Campaign:
    """One driver of the campaign in which alpha-stub declares a set of accepted fingerprints and beta-stub does not."""
    campaign = Campaign(make_study(base, "declared-whole", EXTRA, declare=["alpha-stub"]))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def declared_shards(base, clean_environment) -> dict[int, Campaign]:
    return {
        i: run_shard(base, f"declared-shard-{i}", clean_environment, i, count=3, extra=EXTRA, declare=["alpha-stub"])
        for i in SERVES
    }


@pytest.fixture(scope="module")
def declared_merged(base, clean_environment, declared_shards) -> tuple[Campaign, dict[str, str]]:
    """The shards merged and the batch checked, analysed and reported, with each command's stderr: a refusal is
    for the test to name, so no step here is asserted."""
    campaign = Campaign(make_study(base, "declared-merged", EXTRA, declare=["alpha-stub"]))
    errors = {}
    campaign.codes["merge"], errors["merge"] = merge_into(clean_environment, campaign, *declared_shards.values())
    for name, target in {
        "check": str(campaign.runs),
        "analyze": str(campaign.root),
        "report": str(campaign.root),
    }.items():
        campaign.codes[name], errors[name] = cli(clean_environment, name, target)
    return campaign, errors


def test_c32_shards_by_model_where_only_some_models_declare_accepted_fingerprints_merge_and_analyse(
    declared_whole, declared_shards, declared_merged
):
    """C32: a shard of a model without a set of accepted fingerprints (B18) keeps no record of one, so its export has
    no `deployment_shares`, and a shard of a model with one does; yet the batch merges, the analysis and the report
    are made, and the deployment shares are one driver's."""
    declares = {index: ACCEPTED_META in meta_of(shard) for index, shard in declared_shards.items()}
    assert declares == {1: True, 2: True, 3: False}  # so shard 3's export has no `deployment_shares`
    merged_campaign, errors = declared_merged
    assert merged_campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}, errors
    one, many = read(declared_whole, "export.json"), read(merged_campaign, "export.json")
    assert [share["model"] for share in one["deployment_shares"]] == ["alpha-stub"]
    assert many["deployment_shares"] == one["deployment_shares"]
    assert json.dumps(many["deployment_shares"]) == json.dumps(one["deployment_shares"])
    assert list(many) == [*one, *MERGED_ONLY]  # the export's bytes: the place of the keys too
    assert (merged_campaign.root / "out" / "analysis.json").read_bytes() == (
        declared_whole.root / "out" / "analysis.json"
    ).read_bytes()
