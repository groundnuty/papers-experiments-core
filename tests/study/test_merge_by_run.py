"""A campaign split by run (`expcore-shard/5`, #154, C32), its shards run for real and merged into one batch.

The example study declares `[shards] per_model = { alpha-stub = 5, beta-stub = 2 }` with `by = "run"`: shards 1 to 5
serve alpha-stub, which has 4 pairs and 11 runs over its three arms (one arm runs on three of the four tasks), so the
rule by pair would leave one of them with nothing; shards 6 and 7 serve beta-stub. alpha-stub's unit cap is raised to 5
in every copy, since each shard runs one run at a time. Each shard is run by the command line, the whole campaign once
by one driver from the same campaign.toml, and the merged batch is compared with it.
"""

import collections
import json
from pathlib import Path

import pytest
from shard_support import json_meta, keys_of_shard, make_study, merge_into, run_shard
from study_support import Campaign, clean_environment, cli, finish

from expcore.campaign.shard import MERGE_FILE, META_SHARD, META_SHARES, RULE_BY_RUN
from expcore.campaign.shard_by_run import arm_of
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXTRA = '\n[shards]\nper_model = { alpha-stub = 5, beta-stub = 2 }\nby = "run"\n'
SERVES = {1: "alpha-stub", 2: "alpha-stub", 3: "alpha-stub", 4: "alpha-stub", 5: "alpha-stub"} | {
    6: "beta-stub",
    7: "beta-stub",
}


def five_sessions(root: Path) -> None:
    """Raise alpha-stub's unit cap to its 5 shards, each of which runs one run at a time."""
    path = root / "campaign.toml"
    text = path.read_text(encoding="utf-8")
    cap = "[scheduler.unit_caps]\nalpha-stub = 2\n"
    assert cap in text, "the example's copy no longer caps alpha-stub at 2"
    path.write_text(text.replace(cap, "[scheduler.unit_caps]\nalpha-stub = 5\n"), encoding="utf-8")


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("merge-by-run")


@pytest.fixture(scope="module")
def whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign, from the campaign.toml the shards have: the reference."""
    campaign = Campaign(make_study(base, "whole", EXTRA, edit=five_sessions))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def shards(base, clean_environment) -> dict[int, Campaign]:
    return {
        i: run_shard(base, f"shard-{i}", clean_environment, i, count=7, extra=EXTRA, edit=five_sessions) for i in SERVES
    }


@pytest.fixture(scope="module")
def merged(base, clean_environment, shards) -> Campaign:
    campaign = Campaign(make_study(base, "merged", EXTRA, edit=five_sessions))
    campaign.codes["merge"], errors = merge_into(clean_environment, campaign, *shards.values())
    assert campaign.codes["merge"] == 0, errors
    finish(clean_environment, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


def read(campaign: Campaign, name: str):
    return json.loads((campaign.root / "out" / name).read_text(encoding="utf-8"))


def test_c32_each_shard_by_run_ran_its_dealt_runs_one_at_a_time_with_every_arm_balanced(shards):
    """C32: shard i ran exactly the runs the deal by run gave it, of its model only, under expcore-shard/5, with a cap
    of one run on its model's unit; alpha-stub's five shards each hold 2 or 3 of its 11 runs and at most one of each
    arm, so each of its arms is in as many shards as it has runs (4, 3 and 4)."""
    held, arms = {}, collections.defaultdict(set)
    for index, shard in shards.items():
        assert json_meta(shard, META_SHARD)["rule"] == RULE_BY_RUN
        assert json_meta(shard, META_SHARES)["unit_caps"] == {SERVES[index]: 1}
        runs = {row["run_key"]: json.loads(row["factors"]) for row in shard.sql("SELECT run_key, factors FROM runs")}
        assert {factors["model"] for factors in runs.values()} == {SERVES[index]}
        assert sorted(runs) == sorted(keys_of_shard(shard.root, index, 7))
        if SERVES[index] == "alpha-stub":
            assert len(runs) in (2, 3)
            counted = collections.Counter(arm_of(factors) for factors in runs.values())
            assert max(counted.values()) == 1
            for arm in counted:
                arms[arm].add(index)
        held[index] = set(runs)
    assert sorted(len(shards_of_arm) for shards_of_arm in arms.values()) == [3, 4, 4]
    plan = load_study(shards[1].root).manifest
    assert sorted(key for keys in held.values() for key in keys) == sorted(row.key.text for row in plan.rows)


def test_c32_shards_by_run_merge_into_one_campaign_with_each_runs_shard_recorded(whole, merged, shards):
    """C32: the merge takes the 7 shards as one campaign under expcore-shard/5; the batch's scores and analysis are
    byte for byte one driver's, and its export tags every run with the shard that ran it, whose label merge.json's
    list gives, so an analysis can look for an effect of the federation."""
    assert (merged.root / "out" / "scores.json").read_bytes() == (whole.root / "out" / "scores.json").read_bytes()
    assert (merged.root / "out" / "analysis.json").read_bytes() == (whole.root / "out" / "analysis.json").read_bytes()
    document = read(merged, MERGE_FILE)
    assert (document["rule"], document["count"]) == (RULE_BY_RUN, 7)
    assert [(entry["index"], entry["label"]) for entry in document["shards"]] == [(i, f"shard-{i}") for i in SERVES]
    export = read(merged, "export.json")
    assert [(entry["index"], entry["label"]) for entry in export["shards"]] == [(i, f"shard-{i}") for i in SERVES]
    ran = {key: index for index, shard in shards.items() for key in keys_of_shard(shard.root, index, 7)}
    tagged = {entry["run_key"]: entry["shard"] for entry in export["analysed_attempts"]}
    assert tagged == ran
