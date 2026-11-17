"""C32: a campaign split by model (`expcore-shard/3`), each shard on a federation of its own, with a real outage.

`tests/study/test_merge_by_model.py` crosses the shards and the merge only with the example's stand-in treatment, a
failure its script plants. Here the `relay-down` condition's treatment is `provider_outage` on `providers[1]`, taken by
the Onedata layer's `FaultInjector` (`prepared()`, `inject()`, the restore, the fault journal in the federation's root,
the owner of each injection) over the fake Compose services of `fault_support`. Nothing of the outage is scripted: the
stand-in tool reads the fact of `lake` from the provider that holds it, and fails only while that provider is down.

Shards 1 and 2 serve alpha-stub, shard 3 beta-stub (`[shards] per_model`), each against its own federation: its
root, its journal, its services and its Compose project. The whole campaign is run once by one driver against a
fourth. The shards are merged, and the batch is checked, analysed and reported.
"""

import json
from pathlib import Path

import pytest
from faults_shard_support import (
    OTHER_SERVICE,
    TARGET_SERVICE,
    Federation,
    attempts_of,
    make_study,
    outage_attempts,
    run_shard,
    run_whole,
)
from shard_support import json_meta, keys_of_shard, merge_into
from study_support import Campaign, clean_environment, finish

from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import META_ENVIRONMENT

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

EXTRA = "\n[shards]\nper_model = { alpha-stub = 2, beta-stub = 1 }\n"
SERVES = {1: "alpha-stub", 2: "alpha-stub", 3: "beta-stub"}
PROJECTS = {0: "stack-a", 1: "stack-b", 2: "stack-c", 3: "stack-d"}  # the whole campaign's federation is 0
OUTAGE_CONDITION = "relay-down"
OUTAGE_RUNS = 8  # relay-down's four tasks, for each of the two models


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("faults-in-model-shards")


@pytest.fixture(scope="module")
def federations(base) -> dict[int, Federation]:
    return {i: Federation(base / f"federation-{i}", project).make() for i, project in PROJECTS.items()}


@pytest.fixture(scope="module")
def whole(base, clean_environment, federations) -> Campaign:
    """One driver of the whole campaign against one federation: the reference."""
    campaign = run_whole(base, "whole", clean_environment, federations[0], EXTRA)
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def shards(base, clean_environment, federations) -> dict[int, Campaign]:
    return {i: run_shard(base, f"shard-{i}", clean_environment, i, federations[i], extra=EXTRA) for i in SERVES}


@pytest.fixture(scope="module")
def merged(base, clean_environment, shards) -> Campaign:
    """The shards merged into a study that has no platform file (a merge, a check and an analysis run no attempt)."""
    campaign = Campaign(make_study(base, "merged", EXTRA))
    campaign.codes["merge"], errors = merge_into(clean_environment, campaign, *shards.values())
    assert campaign.codes["merge"] == 0, errors
    finish(clean_environment, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


def outage_keys(campaign: Campaign) -> set[str]:
    """The run keys of the campaign's plan whose condition is the outage."""
    return {key for key, condition, _, _ in campaign.planned() if condition == OUTAGE_CONDITION}


def test_c32_every_shard_injected_a_real_outage_in_its_own_federation_and_restored_it_verified(shards, federations):
    """C32: each shard's journal holds at least one injection of `providers[1]`, seen down and cleared, and none open;
    its services are running again with a new identity (one start more per injection) and the other provider was never
    touched. The shard's attempts that held the federation exclusively are exactly the outage runs it holds."""
    for index, shard in shards.items():
        federation = federations[index]
        injections = federation.injections()
        assert injections, f"shard {index} injected no outage: its journal is empty"
        assert all(entry["target"] == "providers[1]" for entry in injections)
        assert [(entry["down"], entry["cleared"]) for entry in injections] == [(True, True)] * len(injections)
        services = federation.services()
        assert services.running(TARGET_SERVICE)
        assert services.running(OTHER_SERVICE)
        assert services.identity(TARGET_SERVICE).endswith(f"@start-{1 + len(injections)}")
        assert services.identity(OTHER_SERVICE).endswith("@start-1")
        mine = {key for key in keys_of_shard(shard.root, index, 3) if key in outage_keys(shard)}
        assert {row["run_key"] for row in outage_attempts(shard)} == mine
        assert len(injections) == len(outage_attempts(shard))


def test_c32_each_shards_journal_holds_only_the_owners_of_its_own_attempts(shards, federations, whole):
    """C32: every injection's owner is an attempt of the shard's own campaign (one campaign key per shard, none shared
    with another shard) and the attempt id it names is one of that shard's outage attempts, whose run the deal gave
    the shard. The whole campaign's one federation holds the owners of every outage run, once each."""
    campaigns = {}
    for index, shard in shards.items():
        owners = [entry["owner"] for entry in federations[index].injections()]
        assert {owner["process"] for owner in owners} == {owners[0]["process"]}  # one process ran every shard here
        campaigns[index] = {owner["campaign"] for owner in owners}
        assert len(campaigns[index]) == 1
        own = {row["id"]: row["run_key"] for row in outage_attempts(shard)}
        assert sorted(owner["attempt_id"] for owner in owners) == sorted(own)
        assert set(own.values()) <= set(keys_of_shard(shard.root, index, 3))
    assert len(set.union(*campaigns.values())) == len(SERVES)  # no two shards' injections share a campaign
    assert sum(len(federations[i].injections()) for i in SERVES) == OUTAGE_RUNS
    assert len(federations[0].injections()) == OUTAGE_RUNS
    assert len({entry["owner"]["campaign"] for entry in federations[0].injections()}) == 1


def test_c32_the_outage_not_a_script_decides_the_one_run_that_reads_from_the_down_provider(shards, whole):
    """C32: under `relay-down` only the task whose fact the down provider holds fails, as a treatment attempt of kind
    `provider_outage` that is scored; every other `relay-down` run reads from the provider that stayed up and
    finishes."""
    for campaign in (*shards.values(), whole):
        for row in attempts_of(campaign):
            factors = json.loads(campaign.sql("SELECT factors FROM runs WHERE run_key = ?", row["run_key"])[0][0])
            struck = factors["task"] == "lake" and row["admission"] == "exclusive"
            assert (row["treatment"], row["infra_kind"], row["state"]) == (
                (1, "provider_outage", "done") if struck else (0, None, "done")
            ), (row, factors)


def test_c32_the_shards_agree_on_one_fingerprint_that_holds_the_faults_and_no_federation(shards, whole, federations):
    """C32: every shard, and the whole campaign, record one campaign fingerprint, whose platform half has `[faults]`:
    the sanction, the target labels and the bounds. Four federations with four roots and four Compose projects are one
    campaign, since neither is in it; and the shards' environment identity is one too."""
    one = json_meta(whole, FINGERPRINT_KEY)
    assert one["platform"]["faults"]["sanctioned"] is True
    assert set(one["platform"]["faults"]["targets"]) == {"providers[0]", "providers[1]"}
    assert {i: json_meta(shard, FINGERPRINT_KEY) for i, shard in shards.items()} == dict.fromkeys(SERVES, one)
    assert len({federations[i].project for i in federations}) == len(federations)
    identities = {i: json_meta(shard, META_ENVIRONMENT) for i, shard in shards.items()}
    assert len({json.dumps(value, sort_keys=True) for value in identities.values()}) == 1


def test_c32_merged_shards_with_real_outages_give_the_scores_and_analysis_of_one_driver(whole, merged):
    """C32: the merged batch's `scores.json` and `analysis.json` are byte for byte those of one driver of the whole
    campaign against one federation."""
    for name in ("scores.json", "analysis.json"):
        assert (merged.root / "out" / name).read_bytes() == (whole.root / "out" / name).read_bytes(), name


def test_c32_a_shard_whose_federation_renders_other_faults_is_refused_at_the_merge_naming_it(
    base, clean_environment, shards, federations
):
    """C32: shard 2 is run again against a federation whose `[faults]` waits another poll; its fingerprint differs from
    the others', and the merge refuses, naming the shard and the key of the platform's faults that differs."""
    other = Federation(base / "federation-other", "stack-e", {"poll_s": 2}).make()
    changed = run_shard(base, "shard-2-other", clean_environment, 2, other, extra=EXTRA)
    target = Campaign(make_study(base, "refused", EXTRA))
    code, errors = merge_into(clean_environment, target, shards[1], changed, shards[3])
    assert code == 1, errors
    assert "shard 2 (shard-2): ran another campaign than shard 1 (shard-1); its fingerprint differs" in errors
    assert "platform.faults.settings.poll_s" in errors
    assert not (target.root / "out" / "merge.json").exists()
