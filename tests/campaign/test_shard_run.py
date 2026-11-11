"""A shard of a campaign (#154): its part of the plan, what its store pins, what a resume refuses, its shares."""

import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import pytest
from campaign_support import LATER, NOW, make_fingerprint, make_manifest, make_schema

from expcore.campaign.export import ExportError, export_run_state
from expcore.campaign.gate import Budget
from expcore.campaign.manifest import plan_problem, shard_manifest
from expcore.campaign.resume import FingerprintMismatch, RunSetMismatch, open_campaign
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard import (
    EXPORT_KEY,
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    ShardError,
    ShardSpec,
    assign,
    pair_of,
)
from expcore.campaign.shard_run import ShardMismatch, ShardStart, share_settings

TASKS = ("task-a", "task-b", "task-c", "task-d", "task-e")
MODELS = ("model-falcon", "model-heron")
SEEDS = (1, 2)
SPEC = ShardSpec(1, 2, "shard-1")
DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 32
AS_SPEC = object()  # `start`: the shard whose spec is given
SHARES = {"unit_caps": {"model-falcon": 2}, "budget": {"max_usd": 2.5, "per_model_usd": {}}}


def plan(tasks=TASKS, models=MODELS, seeds=SEEDS):
    """The schema, the whole plan, and the fingerprint of that whole plan."""
    schema = make_schema()
    manifest = make_manifest(schema, 7, tasks=tasks, models=models, seeds=seeds)
    return schema, manifest, make_fingerprint(schema, task_ids=tasks, model_keys=models, seed_ids=seeds)


def shard_start(spec=SPEC, environment=None, shares=None):
    return ShardStart(
        spec,
        {"platform-config": DIGEST} if environment is None else environment,
        SHARES if shares is None else shares,
    )


def start(tmp_path, spec=SPEC, shard=AS_SPEC, now=NOW, allow_growth=False, **plan_arguments):
    """Open the store of shard `spec`, as the pipeline does: its part of the plan, the whole plan's fingerprint."""
    schema, manifest, fingerprint = plan(**plan_arguments)
    if shard is AS_SPEC:
        shard = shard_start(spec)
    part = manifest if shard is None else shard_manifest(manifest, shard.spec)
    return open_campaign(
        tmp_path / "campaign.db",
        schema,
        part,
        fingerprint,
        now=now,
        max_attempts=3,
        allow_growth=allow_growth,
        shard=shard,
    )


def everything(tmp_path):
    """Every row a refused start must leave alone."""
    connection = sqlite3.connect(tmp_path / "campaign.db")
    try:
        return [
            connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in ("meta", "runs", "attempts", "events")
        ]
    finally:
        connection.close()


# --- the shard's plan ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [2, 3])
def test_c28_a_shards_plan_is_its_pairs_of_the_whole_plan_in_the_whole_plans_places_and_order(count):
    """C28: the shards' plans are disjoint and together the whole plan; each run keeps its global seq and the C08
    order, and every run of a pair (every condition of one task, model and seed) is in one shard."""
    _, manifest, _ = plan()
    parts = [shard_manifest(manifest, ShardSpec(i, count, f"shard-{i}")) for i in range(1, count + 1)]
    places = {row.key.text: row.seq for row in manifest.rows}
    seen = [row.key.text for part in parts for row in part.rows]
    assert sorted(seen) == sorted(places)  # disjoint, and nothing missing
    for part in parts:
        assert [row.seq for row in part.rows] == sorted(row.seq for row in part.rows)  # the C08 order
        assert all(places[row.key.text] == row.seq for row in part.rows)  # the whole plan's place, never renumbered
        assert part.order_seed == manifest.order_seed
    owners = {}
    for index, part in enumerate(parts):
        for row in part.rows:
            owners.setdefault(pair_of(row.key.factors), set()).add(index)
    assert all(len(shards) == 1 for shards in owners.values())  # a pair never crosses shards


def test_c28_the_partition_is_assigned_from_the_whole_plan_not_from_the_runs_a_shard_holds():
    """C28: the assignment of a pair depends on the whole plan, so the shard's plan is made from the full manifest."""
    _, manifest, _ = plan()
    assignment = assign({pair_of(row.key.factors) for row in manifest.rows}, 2)
    part = shard_manifest(manifest, SPEC)
    assert {pair_of(row.key.factors) for row in part.rows} == {pair for pair, shard in assignment.items() if shard == 1}


def test_c28_a_start_is_checked_against_the_shards_part_of_the_plan_its_fingerprint_declares():
    """C28: `plan_problem` compares a shard's manifest with that part of the declared plan: neither the whole plan
    nor another shard's part is accepted as shard 1's."""
    schema, manifest, fingerprint = plan()
    assert plan_problem(schema, shard_manifest(manifest, SPEC), fingerprint, SPEC) is None
    assert "does not declare" in plan_problem(schema, manifest, fingerprint, SPEC)
    other = plan_problem(schema, shard_manifest(manifest, ShardSpec(2, 2, "shard-2")), fingerprint, SPEC)
    assert other is not None
    assert "does not declare" in other
    assert "omits" in other
    assert plan_problem(schema, manifest, fingerprint) is None  # without a shard, the whole plan, as before


def test_c28_a_shards_store_holds_its_runs_at_their_places_in_the_whole_plan(tmp_path):
    """C28: the store keeps each run's global seq, so a merge can take the union in the whole plan's order."""
    _, manifest, _ = plan()
    store, _ = start(tmp_path)
    held = {row["run_key"]: row["seq"] for row in store.q("SELECT run_key, seq FROM runs")}
    assert held == {row.key.text: row.seq for row in shard_manifest(manifest, SPEC).rows}
    assert 0 < len(held) < len(manifest.rows)
    assert sorted(held.values()) != list(range(len(held)))  # places in the whole plan, so with gaps


# --- what the first start pins ------------------------------------------------------------------------------------


def test_c28_the_first_start_pins_the_spec_the_time_the_identity_and_the_shares(tmp_path):
    """C28: four facts in the meta table, as sorted JSON; the fingerprint is the whole plan's, shared by every shard."""
    store, _ = start(tmp_path)
    assert json.loads(store.get_meta(META_SHARD)) == SPEC.to_json()
    assert store.get_meta(META_STARTED) == NOW
    assert store.get_meta(META_ENVIRONMENT) == f'{{"platform-config":"{DIGEST}"}}'  # sorted, compact
    assert json.loads(store.get_meta(META_SHARES)) == SHARES
    (tmp_path / "second").mkdir()
    second, _ = start(tmp_path / "second", spec=ShardSpec(2, 2, "shard-2"))
    assert second.get_meta("fingerprint") == store.get_meta("fingerprint")


def test_c28_a_resume_keeps_every_pin_and_never_moves_the_first_start(tmp_path):
    """C28: a later start of the same shard changes nothing, the time of the first start included."""
    store, _ = start(tmp_path)
    store.close()
    before = everything(tmp_path)
    again, _ = start(tmp_path, now=LATER)
    again.close()
    assert everything(tmp_path) == before


def test_an_unsharded_start_pins_the_order_and_the_fingerprint_and_nothing_of_a_shard(tmp_path):
    """Without a shard the store's meta is what it was before shards existed, and the plan is the whole plan."""
    _, manifest, _ = plan()
    store, _ = start(tmp_path, shard=None)
    assert sorted(row["key"] for row in store.q("SELECT key FROM meta")) == ["fingerprint", "order_rule", "order_seed"]
    assert len(store.run_keys()) == len(manifest.rows)


# --- what a resume refuses ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "difference"),
    [
        (ShardSpec(2, 2, "shard-1"), "index: recorded 1, now 2"),
        (ShardSpec(1, 2, "shard-other"), "label: recorded 'shard-1', now 'shard-other'"),
        (ShardSpec(1, 3, "shard-1"), "count: recorded 2, now 3"),
    ],
    ids=["index", "label", "count"],
)
def test_c28_a_resume_as_another_shard_or_label_or_count_is_refused_and_the_store_left_alone(
    tmp_path, spec, difference
):
    """C28: a store resumes only as the shard it started as."""
    start(tmp_path)[0].close()
    before = everything(tmp_path)
    with pytest.raises(ShardMismatch, match="spec, label or rule") as refused:
        start(tmp_path, spec=spec, now=LATER)
    assert difference in str(refused.value)
    assert everything(tmp_path) == before


def test_c28_a_store_made_under_another_partition_rule_is_refused(tmp_path):
    """C28: the rule is kept with the shard; a store that recorded another rule is not resumed under this one."""
    store, _ = start(tmp_path)
    old = json.dumps({**SPEC.to_json(), "rule": "expcore-shard/1: an older rule"}, sort_keys=True)
    store.put_meta(META_SHARD, old, NOW)
    store.close()
    with pytest.raises(ShardMismatch, match="rule: recorded 'expcore-shard/1: an older rule'"):
        start(tmp_path, now=LATER)


def test_c28_a_sharded_store_resumed_without_a_shard_is_refused_and_says_how_to_resume_it(tmp_path):
    """C28: a store that began as a shard is not continued as the whole campaign."""
    start(tmp_path)[0].close()
    before = everything(tmp_path)
    with pytest.raises(ShardMismatch, match=r"gives no shard: resume it with --shard 1/2 --shard-label shard-1"):
        start(tmp_path, shard=None, now=LATER)
    assert everything(tmp_path) == before


def test_c28_an_unsharded_store_resumed_as_a_shard_is_refused(tmp_path):
    """C28: nor is a store that began as the whole campaign continued as one of its shards."""
    start(tmp_path, shard=None)[0].close()
    before = everything(tmp_path)
    with pytest.raises(ShardMismatch, match="holds an unsharded campaign"):
        start(tmp_path, now=LATER)
    assert everything(tmp_path) == before


def open_with(tmp_path, shard):
    return start(tmp_path, shard=shard, now=LATER)


def test_c29_a_resume_under_another_environment_identity_is_refused_naming_what_changed(tmp_path):
    """C29: the identity a shard started with is kept; a shard resumed against other data would not be one batch."""
    start(tmp_path)[0].close()
    before = everything(tmp_path)
    for changed, shown in (
        ({"platform-config": OTHER_DIGEST}, f"platform-config: recorded {DIGEST[:12]}, now {OTHER_DIGEST[:12]}"),
        ({"platform-config": DIGEST, "ingest": DIGEST}, f"ingest: recorded absent, now {DIGEST[:12]}"),
        ({}, f"platform-config: recorded {DIGEST[:12]}, now absent"),
    ):
        with pytest.raises(ShardMismatch, match="environment identity changed") as refused:
            open_with(tmp_path, shard_start(environment=changed))
        assert shown in str(refused.value)
        assert everything(tmp_path) == before


def test_c30_a_resume_with_other_shares_is_refused(tmp_path):
    """C30: the shares are fixed at the first start, so that the shards' shares never sum past the totals."""
    start(tmp_path)[0].close()
    before = everything(tmp_path)
    other = {"unit_caps": {"model-falcon": 3}, "budget": {"max_usd": 2.5, "per_model_usd": {}}}
    with pytest.raises(ShardMismatch, match="shares of the caps and the budget changed"):
        open_with(tmp_path, shard_start(shares=other))
    assert everything(tmp_path) == before


@pytest.mark.parametrize("allow_growth", [False, True])
def test_c28_a_sharded_stores_plan_cannot_grow_on_resume_whatever_allow_growth_says(tmp_path, allow_growth):
    """C28: the assignment of pairs to shards depends on the whole plan, so a plan that grew is refused, naming why."""
    start(tmp_path)[0].close()
    before = everything(tmp_path)
    with pytest.raises(FingerprintMismatch, match="a sharded campaign's plan cannot grow") as refused:
        start(tmp_path, now=LATER, allow_growth=allow_growth, tasks=(*TASKS, "task-f"))
    assert "tasks.task-f: not in the recorded experiment" in refused.value.differences
    assert "pass allow_growth" not in str(refused.value)
    assert everything(tmp_path) == before


def test_an_unsharded_plan_still_grows_when_the_caller_allows_it(tmp_path):
    """Growth stays what it was for a campaign that is not a shard."""
    start(tmp_path, shard=None)[0].close()
    store, _ = start(tmp_path, shard=None, now=LATER, allow_growth=True, tasks=(*TASKS, "task-f"))
    assert len(store.run_keys()) == len(
        make_manifest(make_schema(), 7, tasks=(*TASKS, "task-f"), models=MODELS, seeds=SEEDS).rows
    )


def test_c28_a_shards_manifest_that_is_not_its_part_of_the_plan_is_refused_before_the_store_is_touched(tmp_path):
    """C28: the whole plan given to a shard, or another shard's part, is a different experiment."""
    schema, manifest, fingerprint = plan()
    for wrong in (manifest, shard_manifest(manifest, ShardSpec(2, 2, "shard-2"))):
        with pytest.raises(RunSetMismatch, match="does not declare"):
            open_campaign(
                tmp_path / "campaign.db", schema, wrong, fingerprint, now=NOW, max_attempts=3, shard=shard_start()
            )
    assert not (tmp_path / "campaign.db").exists()


# --- the shares ---------------------------------------------------------------------------------------------------


def settings(**changes):
    return dataclasses.replace(
        SchedulerConfig(
            unit_caps={"unit-b": 7, "unit-a": 4},
            budget=Budget(10.0, {"model-falcon": 6.0, "model-heron": 3.0}, 1.0, Path("releases.txt")),
        ),
        **changes,
    )


@pytest.mark.parametrize("index", [1, 2, 3])
def test_c30_a_shard_runs_on_its_share_of_each_unit_cap_and_ceiling_and_records_it(index):
    """C30: each unit's cap and each ceiling becomes the shard's share of the total; the release step is the host's."""
    spec = ShardSpec(index, 3, f"shard-{index}")
    shared, recorded = share_settings(settings(), spec)
    assert shared.unit_caps == recorded["unit_caps"] == {"unit-a": 1 + (index <= 1), "unit-b": 2 + (index <= 1)}
    assert shared.budget.max_usd == recorded["budget"]["max_usd"] == pytest.approx(3.333333 + (index == 1) * 0.000001)
    assert (
        shared.budget.per_model_usd == recorded["budget"]["per_model_usd"] == {"model-falcon": 2.0, "model-heron": 1.0}
    )
    assert shared.budget.release_step_usd == 1.0
    assert shared.workers == settings().workers


def test_c30_the_shares_of_all_shards_sum_to_the_totals_and_never_past_them():
    """C30: caps sum exactly; dollars, shared in whole micro-dollars rounded down, sum to at most the total."""
    parts = [share_settings(settings(), ShardSpec(i, 3, f"shard-{i}"))[0] for i in (1, 2, 3)]
    assert [sum(p.unit_caps[unit] for p in parts) for unit in ("unit-a", "unit-b")] == [4, 7]
    assert sum(p.budget.max_usd for p in parts) <= 10.0
    assert sum(p.budget.per_model_usd["model-falcon"] for p in parts) <= 6.0


def test_c30_without_a_budget_only_the_caps_are_shared_and_the_record_says_so():
    shared, recorded = share_settings(settings(budget=None), SPEC)
    assert shared.budget is None
    assert recorded == {"unit_caps": {"unit-a": 2, "unit-b": 4}, "budget": None}


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"unit_caps": {"unit-a": 1}}, "[scheduler.unit_caps] unit-a"),
        ({"budget": Budget(0.000001)}, "[budget] max_usd"),
        ({"budget": Budget(10.0, {"model-falcon": 0.000001})}, "[budget.per_model_usd] model-falcon"),
    ],
    ids=["unit cap", "max_usd", "per-model ceiling"],
)
def test_c30_a_total_below_the_number_of_shards_is_refused_naming_its_key(changes, key):
    """C30: a total smaller than the shard count would leave a shard with none."""
    with pytest.raises(ShardError, match=f"{re.escape(key)} is .*less than the 2 shards"):
        share_settings(settings(**changes), SPEC)


# --- the export ----------------------------------------------------------------------------------------------------


def test_c28_a_shards_export_has_the_shard_key_with_its_facts_and_the_runs_are_what_they_are(tmp_path):
    """C28: `shard` holds spec, started_utc, environment_identity and shares; the key comes after every other."""
    store, _ = start(tmp_path)
    export = export_run_state(store)
    assert list(export)[-1] == EXPORT_KEY
    assert export[EXPORT_KEY] == {
        "spec": SPEC.to_json(),
        "started_utc": NOW,
        "environment_identity": {"platform-config": DIGEST},
        "shares": SHARES,
    }
    assert export["planned_runs"] == len(store.run_keys())  # the shard's own runs, not the whole plan's


def test_an_unsharded_export_has_no_shard_key_and_the_keys_it_always_had(tmp_path):
    """Without a shard the export is what it was: the same keys in the same order, none of a shard's."""
    store, _ = start(tmp_path, shard=None)
    assert list(export_run_state(store)) == [
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
    ]
    assert EXPORT_KEY not in export_run_state(store, excluded={})


def test_a_store_that_records_a_shard_without_its_other_facts_is_not_exported_as_one(tmp_path):
    store, _ = start(tmp_path, shard=None)
    store.put_meta(META_SHARD, json.dumps(SPEC.to_json()), NOW)
    with pytest.raises(ExportError, match="records a shard but not"):
        export_run_state(store)
