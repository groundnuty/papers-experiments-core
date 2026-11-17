"""A campaign split by model (`expcore-shard/3`, #154, C32), its shards run for real and merged into one batch.

The example study declares `[shards] per_model = { alpha-stub = 2, beta-stub = 1 }`: shards 1 and 2 serve alpha-stub,
shard 3 serves beta-stub. Each shard is run by the command line (`shard_support.run_shard`), so its plan, store meta,
caps and budget are what the run side gives; the whole campaign is run once by one driver from the same campaign.toml,
`[shards]` included, so the two have one fingerprint and the merged batch can be compared with it byte for byte.

A second campaign, built from the example by `one_priced`, declares `per_model = { alpha-stub = 1, beta-stub = 1,
gamma-stub = 1 }` with one priced model and two that cost nothing (beta-stub marked free, gamma-stub priced at 0): the
layout of a pilot that prices a single model. Its shards and its merge are checked the same way, and the last tests
check that shards whose fault settings differ are refused at merge by the campaign fingerprint.
"""

import json
from pathlib import Path

import pytest
from campaign_support import NOW, make_fingerprint, make_schema
from fault_support import SERVICES, write_faults
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
from expcore.campaign.merge import MergeRefused, ShardFacts, check_fingerprints
from expcore.campaign.shard import MERGE_FILE, META_SHARD, META_SHARES, RULE_BY_MODEL, ShardSpec
from expcore.study.config import load_study
from expcore_onedata.config import load_platform

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


def same_as_one_driver(whole: Campaign, merged: Campaign) -> None:
    """The merged batch is one driver's: scores and analysis byte for byte; the export differs only in the fields of a
    shard's own dispatch sequence, the shard tags and what only a merge has (C29: `shard_retries_by_kind`, and each
    analysed entry's `wall_s` and `turn_latency_s`); the report only in the median dispatch position and the
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


def test_c32_a_campaign_split_by_model_and_merged_gives_the_scores_and_analysis_of_one_driver(whole, merged):
    """C32: scores and analysis of the merged batch are byte for byte one driver's; the export differs only in the
    fields of a shard's own dispatch sequence, the shard tags and what only a merge has (C29: `shard_retries_by_kind`,
    and each analysed entry's `wall_s` and `turn_latency_s`), the report only in the median dispatch position and the
    `## Shards` section only a merged batch has."""
    same_as_one_driver(whole, merged)


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


# --- one priced model beside two that cost nothing, one shard each: a pilot that prices a single model ---------------

ONE_PRICED_EXTRA = "\n[shards]\nper_model = { alpha-stub = 1, beta-stub = 1, gamma-stub = 1 }\n"
ONE_PRICED_SERVES = {1: "alpha-stub", 2: "beta-stub", 3: "gamma-stub"}
ONE_PRICED_SHARES = {
    1: {"max_usd": 5.0, "per_model_usd": {"alpha-stub": 3.0}},  # the whole of `max_usd` and of its model's ceiling
    2: {"max_usd": 0.0, "per_model_usd": {}},
    3: {"max_usd": 0.0, "per_model_usd": {}},
}
BETA_PRICED = """[models.beta-stub]
backend = "stub"
status = "frozen"
context_tokens = 8192

[models.beta-stub.price]
input_per_mtok = 0.5
output_per_mtok = 1.5
"""
BETA_FREE = """[models.beta-stub]
backend = "stub"
status = "frozen"
free = true
"""
GAMMA_AT_ZERO = """
[models.gamma-stub]
backend = "stub"
status = "frozen"
context_tokens = 8192

[models.gamma-stub.price]
input_per_mtok = 0.0
output_per_mtok = 0.0
"""


def one_priced(root: Path) -> None:
    """Make the example's campaign one priced model and two that cost nothing, in both ways a model can: alpha-stub
    keeps its price and gets a ceiling of its own, beta-stub is marked free, gamma-stub is added at a price of 0."""
    edits = {
        "campaign.toml": (
            ('models = ["alpha-stub", "beta-stub"]', 'models = ["alpha-stub", "beta-stub", "gamma-stub"]'),
            ("max_usd = 5.0", "max_usd = 5.0\nper_model_usd = { alpha-stub = 3.0 }"),
        ),
        "models.toml": ((BETA_PRICED, BETA_FREE),),
    }
    for name, replacements in edits.items():
        text = (root / name).read_text(encoding="utf-8")
        for old, new in replacements:
            assert old in text, f"the example's {name} no longer has {old!r}"
            text = text.replace(old, new)
        (root / name).write_text(text, encoding="utf-8")
    with (root / "models.toml").open("a", encoding="utf-8") as models:
        models.write(GAMMA_AT_ZERO)


@pytest.fixture(scope="module")
def one_whole(base, clean_environment) -> Campaign:
    """One driver of the whole campaign of one priced model and two free ones, from its shards' campaign.toml."""
    campaign = Campaign(make_study(base, "one-priced-whole", ONE_PRICED_EXTRA, edit=one_priced))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture(scope="module")
def one_shards(base, clean_environment) -> dict[int, Campaign]:
    return {
        i: run_shard(base, f"one-priced-{i}", clean_environment, i, count=3, extra=ONE_PRICED_EXTRA, edit=one_priced)
        for i in ONE_PRICED_SERVES
    }


@pytest.fixture(scope="module")
def one_merged(base, clean_environment, one_shards) -> Campaign:
    campaign = Campaign(make_study(base, "one-priced-merged", ONE_PRICED_EXTRA, edit=one_priced))
    campaign.codes["merge"], errors = merge_into(clean_environment, campaign, *one_shards.values())
    assert campaign.codes["merge"] == 0, errors
    finish(clean_environment, campaign)
    assert campaign.codes == {"merge": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


def treatments(campaign: Campaign) -> list[tuple[str, str, str | None]]:
    """The (condition, model, kind of fault) of each attempt the batch's export scored as its condition's treatment."""
    export = read(campaign, "export.json")
    return sorted(
        (entry["condition"], entry["factors"]["model"], entry["infra_kind"])
        for entry in export["analysed_attempts"]
        if entry["treatment"]
    )


def shard_treatments(shard: Campaign) -> list[tuple[str, str, str | None]]:
    """The same, from a shard's own store: a shard keeps its attempts there, and writes no export of its own."""
    planned = {key: (condition, model) for key, condition, _, model in shard.planned()}
    rows = shard.sql("SELECT run_key, infra_kind FROM attempts WHERE treatment = 1")
    return sorted((*planned[row["run_key"]], row["infra_kind"]) for row in rows)


def test_c32_with_one_priced_model_its_shard_ran_on_the_whole_budget_and_the_free_shards_on_none(one_shards):
    """C32: three models, alpha-stub priced and the other two costing nothing, one shard each: shard 1 recorded the
    whole `max_usd` and its model's whole ceiling, shards 2 and 3 recorded 0 and ran without a budget gate (a gate of 0
    dollars would hold every run, so every run of theirs finished); each ran its own model's runs only, one at a time,
    and held that model's relay-down run, the fault condition's treatment attempt."""
    for index, shard in one_shards.items():
        model = ONE_PRICED_SERVES[index]
        assert json_meta(shard, META_SHARD)["rule"] == RULE_BY_MODEL
        assert json_meta(shard, META_SHARES) == {"unit_caps": {model: 1}, "budget": ONE_PRICED_SHARES[index]}
        rows = shard.sql("SELECT run_key, factors, status FROM runs")
        assert {json.loads(row["factors"])["model"] for row in rows} == {model}
        assert sorted(row["run_key"] for row in rows) == sorted(keys_of_shard(shard.root, index, 3))
        assert {row["status"] for row in rows} == {"done"}
        assert shard_treatments(shard) == [("relay-down", model, "knowledge")]


def test_c32_one_priced_and_two_free_models_one_shard_each_merged_give_the_scores_and_analysis_of_one_driver(
    one_whole, one_merged
):
    """C32: the same split, merged, with the fault condition (relay-down: the tool server failing is its treatment)
    among its conditions: scores and analysis are byte for byte one driver's, the export and the report differ only as
    the first merge test says, and the batch holds one treatment attempt of relay-down for each of the three models."""
    document = json.loads((one_merged.root / "out" / MERGE_FILE).read_text(encoding="utf-8"))
    assert (document["rule"], document["count"]) == (RULE_BY_MODEL, 3)
    same_as_one_driver(one_whole, one_merged)
    expected = [("relay-down", model, "knowledge") for model in sorted(ONE_PRICED_SERVES.values())]
    assert treatments(one_whole) == treatments(one_merged) == expected


# --- shards by model whose fault settings differ (C06): the merge refuses by the campaign fingerprint ---------------


def faults_shard(index: int, config) -> ShardFacts:
    """Shard `index` of 3 by model, with the campaign fingerprint of a campaign whose platform is `config`."""
    return ShardFacts(
        out=None, snapshot=None, snapshot_sha256="0" * 64, spec=ShardSpec(index, 3, f"shard-{index}", RULE_BY_MODEL),
        started_utc=NOW, environment={}, shares={}, runs={},
        fingerprint=make_fingerprint(make_schema(), platform=config.fingerprint()),
    )  # fmt: skip


def faults_federation(tmp_path: Path, name: str, **faults):
    """The platform of a federation whose platform.toml has a `[faults]` table with `faults` changed."""
    (tmp_path / name).mkdir()
    return load_platform(write_faults(tmp_path / name, **faults), require_secrets=False)


def test_c32_three_shards_by_model_of_federations_with_the_same_fault_settings_pass_the_fingerprint_check(tmp_path):
    """C32: each shard's federation has a Compose project and a journal of its own, which the fingerprint leaves out,
    and the same fault settings: the check passes."""
    configs = [
        faults_federation(tmp_path, f"federation-{i}", project=f"stack-{i}", journal=f"journal-{i}") for i in (1, 2, 3)
    ]
    check_fingerprints([faults_shard(index, config) for index, config in enumerate(configs, 1)])


@pytest.mark.parametrize(
    ("changes", "difference"),
    [
        ({"poll_s": 2}, "platform.faults.settings.poll_s: recorded 1.0, now 2.0"),
        ({"heal": "all_links"}, "platform.faults.settings.heal: recorded 'off', now 'all_links'"),
        ({"sanctioned": False}, "platform.faults.sanctioned: recorded True, now False"),
        (
            {"services": {"alpha": SERVICES["alpha"], "beta": "store-three"}},
            "platform.faults.targets.providers[1]: recorded",
        ),
    ],
    ids=["a wait", "the heal", "the sanction", "a target"],
)
def test_c32_a_shard_by_model_whose_fault_settings_differ_is_refused_at_merge_naming_it(tmp_path, changes, difference):
    """C32: the third shard's federation differs from the others' in one fault setting, so its fingerprint's faults do:
    the check refuses the set, naming shard 3 and the faults entry that differs, and not the shards that agree."""
    same = faults_federation(tmp_path, "same")
    other = faults_federation(tmp_path, "other", **changes)
    with pytest.raises(MergeRefused) as refused:
        check_fingerprints([faults_shard(1, same), faults_shard(2, same), faults_shard(3, other)])
    message = str(refused.value)
    assert message.startswith(
        "shard 3 (shard-3): ran another campaign than shard 1 (shard-1); its fingerprint differs:"
    )
    assert difference in message
    assert "shard 2" not in message


def test_c32_three_shards_by_model_with_three_fault_settings_are_refused_naming_the_first_that_differs(tmp_path):
    """C32: each shard is compared with shard 1, so three federations of three settings are refused at shard 2."""
    configs = [
        faults_federation(tmp_path, "one"),
        faults_federation(tmp_path, "two", poll_s=2),
        faults_federation(tmp_path, "three", sanctioned=False),
    ]
    with pytest.raises(MergeRefused, match=r"^shard 2 \(shard-2\): ran another campaign than shard 1 \(shard-1\)"):
        check_fingerprints([faults_shard(index, config) for index, config in enumerate(configs, 1)])
