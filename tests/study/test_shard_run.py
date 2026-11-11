"""`run_study` as one shard of a campaign (#154): its part of the plan, its shares, its environment identity.

Every campaign here is held by a budget too small for any run, so a start opens the store, pins and returns at once:
what is under test is what the start does, and no model is run.
"""

import dataclasses
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.manifest import fingerprint_sha256, shard_manifest
from expcore.campaign.shard import (
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    ShardSpec,
    assign,
    pair_of,
)
from expcore.campaign.shard_run import environment_block
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study import pipeline
from expcore.study.config import load_study
from expcore.study.pipeline import Refused, run_study
from expcore.study.plugin import StudyContext, _check

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]

FIRST, SECOND = ShardSpec(1, 2, "shard-1"), ShardSpec(2, 2, "shard-2")
DIGEST = "ab" * 32
OTHER = "cd" * 32


def stamped_copy(base: Path, name: str, **replaced: str) -> Path:
    """A stamped copy of the example whose budget holds every run, with `campaign.toml` text replaced as given."""
    copy = base / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    text = (copy / "campaign.toml").read_text(encoding="utf-8").replace("max_usd = 5.0", "max_usd = 0.00001")
    for old, new in replaced.items():
        text = text.replace(old, new)
    (copy / "campaign.toml").write_text(text, encoding="utf-8")
    write_stamp(copy, "a" * 40, load_study(copy).scope)
    return copy


@pytest.fixture
def study(tmp_path) -> Path:
    """The example with two of every unit's runs in flight, so that two shards each get a share of one or more."""
    return stamped_copy(tmp_path, "study", **{"alpha-stub = 1": "alpha-stub = 2", "beta-stub = 1": "beta-stub = 2"})


def store_of(study: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(study / "out" / "state.sqlite")
    connection.row_factory = sqlite3.Row
    return connection


def meta_of(study: Path) -> dict[str, str]:
    connection = store_of(study)
    try:
        return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
    finally:
        connection.close()


def with_identity(monkeypatch, identity, calls=None):
    """Make every plugin the pipeline loads declare `identity` as its environment identity; count the calls."""
    real = pipeline.load_plugin

    def spy():
        if calls is not None:
            calls.append(1)
        return identity() if callable(identity) else identity

    def load(context):
        return dataclasses.replace(real(context), environment_identity=spy)

    monkeypatch.setattr(pipeline, "load_plugin", load)


def test_c28_a_shard_runs_only_its_part_of_the_plan_and_the_store_holds_those_runs_in_the_whole_plans_places(study):
    """C28: shard 1 of 2 of the example's plan: its pairs of the whole plan, at their global seq."""
    whole = load_study(study).manifest
    part = shard_manifest(whole, FIRST)
    summary = run_study(study, shard=FIRST)
    assert 0 < len(part.rows) < len(whole.rows)
    assert summary.statuses == {"pending": len(part.rows)}  # held by the budget, none started
    connection = store_of(study)
    held = {row["run_key"]: row["seq"] for row in connection.execute("SELECT run_key, seq FROM runs")}
    connection.close()
    assert held == {row.key.text: row.seq for row in part.rows}
    assignment = assign({pair_of(row.key.factors) for row in whole.rows}, 2)
    assert {pair_of(row.key.factors) for row in part.rows} == {pair for pair, shard in assignment.items() if shard == 1}


def test_c28_the_two_shards_of_one_campaign_have_the_fingerprint_of_the_whole_plan_and_the_shares_come_after_it(
    tmp_path,
):
    """C28, C30: the totals are fingerprinted, so every shard and an unsharded start share one fingerprint; the caps and
    budget the scheduler runs with are the shard's shares of them."""
    caps = {"alpha-stub = 1": "alpha-stub = 3", "beta-stub = 1": "beta-stub = 5"}
    stores = {}
    for name, spec in (("whole", None), ("one", FIRST), ("two", SECOND)):
        copy = stamped_copy(tmp_path, name, **caps)
        run_study(copy, shard=spec)
        stores[name] = meta_of(copy)
    assert stores["one"]["fingerprint"] == stores["two"]["fingerprint"] == stores["whole"]["fingerprint"]
    total = json.loads(stores["whole"]["fingerprint"])["caps"]["budget"]["max_usd"]
    assert total == 0.00001  # the whole budget, not a share of it
    shares = {name: json.loads(stores[name][META_SHARES]) for name in ("one", "two")}
    assert shares["one"]["unit_caps"] == {"alpha-stub": 2, "beta-stub": 3}  # the remainder goes to the first shard
    assert shares["two"]["unit_caps"] == {"alpha-stub": 1, "beta-stub": 2}
    assert shares["one"]["budget"] == {"max_usd": 0.000005, "per_model_usd": {}}
    assert shares["one"]["budget"]["max_usd"] + shares["two"]["budget"]["max_usd"] <= total


def test_c30_the_scheduler_runs_on_the_shards_shares_and_the_shards_part_of_the_plan(study, monkeypatch):
    """C30: the settings the scheduler gets are the shard's shares; the fingerprint it gets is the totals'."""
    seen = {}
    real = pipeline.Scheduler

    class Spy(real):
        def __init__(self, *args, **kwargs):
            seen.update(settings=kwargs["settings"], manifest=args[2], fingerprint=args[3], shard=kwargs["shard"])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pipeline, "Scheduler", Spy)
    run_study(study, shard=SECOND)
    assert seen["settings"].unit_caps == {"alpha-stub": 1, "beta-stub": 1}
    assert seen["settings"].budget.max_usd == 0.000005
    assert seen["fingerprint"]["caps"]["budget"]["max_usd"] == 0.00001
    assert seen["shard"].spec == SECOND
    assert [row.key.text for row in seen["manifest"].rows] == [
        row.key.text for row in shard_manifest(load_study(study).manifest, SECOND).rows
    ]


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"alpha-stub = 2": "alpha-stub = 1"}, "[scheduler.unit_caps] alpha-stub"),
        ({"max_usd = 0.00001": "max_usd = 0.000001"}, "[budget] max_usd"),
    ],
    ids=["unit cap", "budget"],
)
def test_c30_a_total_below_the_number_of_shards_is_refused_at_start_naming_its_key(tmp_path, changes, key):
    """C30: the campaign's own total cannot be shared among more shards than it has units."""
    copy = stamped_copy(tmp_path, "small", **{"alpha-stub = 1": "alpha-stub = 2", "beta-stub = 1": "beta-stub = 2"})
    text = (copy / "campaign.toml").read_text(encoding="utf-8")
    for old, new in changes.items():
        text = text.replace(old, new)
    (copy / "campaign.toml").write_text(text, encoding="utf-8")
    write_stamp(copy, "a" * 40, load_study(copy).scope)
    with pytest.raises(Refused, match=f"{re.escape(key)} is .*less than the 2 shards"):
        run_study(copy, shard=FIRST)
    assert not (copy / "out" / "state.sqlite").exists()  # refused before the store was opened


def test_c28_a_shard_with_no_run_is_refused_before_the_store_is_opened(tmp_path):
    """C28: more shards than pairs leaves a shard with nothing to run; it is refused, saying to use fewer shards."""
    copy = stamped_copy(tmp_path, "wide", **{"alpha-stub = 1": "alpha-stub = 9", "beta-stub = 1": "beta-stub = 9"})
    whole = load_study(copy).manifest
    assignment = assign({pair_of(row.key.factors) for row in whole.rows}, 9)
    empty = next(i for i in range(1, 10) if i not in set(assignment.values()))
    with pytest.raises(Refused, match=f"shard {empty} of 9 gets no run of this plan.*fewer shards"):
        run_study(copy, shard=ShardSpec(empty, 9, "shard-x"))
    assert not (copy / "out" / "state.sqlite").exists()


def test_c29_the_environment_identity_is_read_once_at_the_start_validated_and_recorded(study, monkeypatch):
    """C29: the plugin's `environment_identity` is called once per start of a shard; the store keeps it sorted."""
    calls: list[int] = []
    with_identity(monkeypatch, {"space-manifest": OTHER, "platform-config": DIGEST}, calls)
    run_study(study, shard=FIRST)
    assert calls == [1]
    meta = meta_of(study)
    assert meta[META_ENVIRONMENT] == f'{{"platform-config":"{DIGEST}","space-manifest":"{OTHER}"}}'
    assert json.loads(meta[META_SHARD]) == FIRST.to_json()
    assert meta[META_STARTED].endswith("Z")


def test_c29_a_plugin_without_an_identity_gives_an_empty_one(study):
    run_study(study, shard=FIRST)
    assert meta_of(study)[META_ENVIRONMENT] == "{}"


@pytest.mark.parametrize(
    "identity",
    [{"platform": "not a digest"}, {"Platform Config": DIGEST}, ["platform-config"]],
    ids=["not a digest", "not a name", "not a mapping"],
)
def test_c29_an_environment_identity_that_is_not_names_mapped_to_sha256_digests_is_refused_at_start(
    study, monkeypatch, identity
):
    """C29: names and digests only, since a package is public; refused before the store is opened."""
    with_identity(monkeypatch, identity)
    with pytest.raises(Refused, match="identity"):
        run_study(study, shard=FIRST)
    assert not (study / "out" / "state.sqlite").exists()


def test_c29_a_shard_resumed_against_another_environment_identity_is_refused(study, monkeypatch):
    """C29: the identity is read at every start and must be the one the shard started with."""
    with_identity(monkeypatch, {"platform-config": DIGEST})
    run_study(study, shard=FIRST)
    run_study(study, shard=FIRST)  # the same identity resumes
    with_identity(monkeypatch, {"platform-config": OTHER})
    with pytest.raises(ResumeRefused, match=f"platform-config: recorded {DIGEST[:12]}, now {OTHER[:12]}"):
        run_study(study, shard=FIRST)


def test_c28_a_shard_resumes_only_as_itself_through_the_pipeline(study):
    """C28: another shard, no shard and a store that began unsharded are each refused."""
    run_study(study, shard=FIRST)
    with pytest.raises(ResumeRefused, match="another spec, label or rule"):
        run_study(study, shard=SECOND)
    with pytest.raises(ResumeRefused, match="gives no shard"):
        run_study(study)
    assert run_study(study, shard=FIRST).statuses == {
        "pending": len(shard_manifest(load_study(study).manifest, FIRST).rows)
    }


def test_c28_a_store_that_began_unsharded_is_not_resumed_as_a_shard(study):
    run_study(study)
    with pytest.raises(ResumeRefused, match="holds an unsharded campaign"):
        run_study(study, shard=FIRST)


def test_an_unsharded_start_never_asks_for_an_identity_nor_pins_a_shards_facts(study, monkeypatch):
    """Without `--shard` nothing of a shard is consulted: the plugin's identity is not called, and the meta table holds
    no fact of a shard."""

    def refuse():
        raise AssertionError("an unsharded start asked for an environment identity")

    with_identity(monkeypatch, refuse)
    summary = run_study(study)
    assert summary.statuses == {"pending": len(load_study(study).manifest.rows)}  # the whole plan
    assert not {META_SHARD, META_STARTED, META_ENVIRONMENT, META_SHARES} & set(meta_of(study))


def test_a_plugins_environment_identity_must_be_a_function(tmp_path):
    """The plugin is refused at load, as a platform that is not a mapping is, and the function is not called there."""

    copy = stamped_copy(tmp_path, "study")
    study = load_study(copy)
    plugin = pipeline.load_plugin(StudyContext(study, {}))
    assert plugin.environment_identity is None  # the example declares none
    declared = dataclasses.replace(plugin, environment_identity={"x": DIGEST})
    with pytest.raises(ConfigError, match="environment_identity must be a function"):
        _check(copy / "plugin.py", study, declared)


def test_c29_a_shard_says_the_environment_part_of_its_fingerprint_as_it_starts_and_an_unsharded_start_says_none(
    study, tmp_path, capsys
):
    """C29: one block on stderr with the environment's digest and its distributions, so a host that would not merge
    shows before it runs; no path of this machine in it, and nothing at all without `--shard`."""
    run_study(study, shard=FIRST)
    said = capsys.readouterr().err
    environment = json.loads(meta_of(study)["fingerprint"])["environment"]
    block = environment_block(FIRST, environment)
    assert block in said
    assert f"environment sha256 {fingerprint_sha256(environment)}" in said
    assert "papers-experiments-core" in block
    assert not any(private in block for private in (str(tmp_path), str(Path.home())))
    run_study(study, shard=FIRST)  # a resume says it again
    assert capsys.readouterr().err.count("environment sha256") == 1
    whole = stamped_copy(tmp_path, "whole")
    run_study(whole)
    assert "environment sha256" not in capsys.readouterr().err


def test_c29_a_host_whose_environment_differs_is_visible_before_its_store_refuses_it(study, monkeypatch, capsys):
    """C29: a resume under another environment is refused by the store; the shard has already said that environment,
    so the operator sees which digest this host has."""
    run_study(study, shard=FIRST)
    recorded = json.loads(meta_of(study)["fingerprint"])["environment"]
    capsys.readouterr()
    other = {**recorded, "distributions": {**recorded["distributions"], "papers-experiments-core": "9.9.9"}}
    monkeypatch.setattr(pipeline, "study_environment", lambda study: other)
    with pytest.raises(ResumeRefused):
        run_study(study, shard=FIRST)
    said = capsys.readouterr().err
    assert f"environment sha256 {fingerprint_sha256(other)}" in said
    assert fingerprint_sha256(other) != fingerprint_sha256(recorded)
    assert "papers-experiments-core 9.9.9" in said


def test_c29_a_shard_refused_before_its_store_opens_says_no_environment(tmp_path, capsys):
    """C29: the block is the start of a shard that goes on, not of one the command refuses first."""
    copy = stamped_copy(
        tmp_path, "small", **{"alpha-stub = 1": "alpha-stub = 2", "max_usd = 0.00001": "max_usd = 0.000001"}
    )
    with pytest.raises(Refused):
        run_study(copy, shard=FIRST)
    assert "environment sha256" not in capsys.readouterr().err
