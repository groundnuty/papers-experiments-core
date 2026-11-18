"""Sharding by run (#154, C32, `expcore-shard/5`): any number of shards per model, up to its runs, every arm balanced.

A campaign that declares `[shards] per_model` with `by = "run"` deals each model's runs, not its pairs, round that
model's own shards: arm by arm (an arm is a condition, known by its declared factor values), so that every shard gets
the floor or the ceiling of each arm's runs over its model's shards, and no shard holds one arm while the counts allow
it more. The tests below check that for every shard count from 1 to a model's runs, on a plan with an arm that runs on
some tasks only, and that the forms of the campaign without `by = "run"` assign exactly what they did before.
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import json

import pytest
from campaign_support import chain_schema, make_fingerprint

from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import build_manifest, plan_problem, shard_manifest
from expcore.campaign.shard import (
    RULE_BY_MODEL,
    RULE_BY_RUN,
    RULES,
    ModelShards,
    ShardError,
    ShardSpec,
    assign,
    assign_by_model,
    by_model,
    layout_of,
    partition,
)
from expcore.campaign.shard_by_run import arm_of, assign_by_run, owners

TASKS = [f"task-{n:02d}" for n in range(7)]
MODELS = ("model-falcon", "model-heron")
SEEDS = (1, 2)
CHAIN = chain_schema(tasks=TASKS[:3])  # three arms; download-write runs on three of the seven tasks
PAIRED = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {"a": {"arm": "a"}, "b": {"arm": "b"}})


def runs_of(schema: FactorSchema, tasks=TASKS, models=MODELS, seeds=SEEDS) -> dict[str, dict]:
    """The plan's runs, by run key text, with their factors."""
    return {key.text: key.factors for key in schema.expand(tasks, models, seeds)}


def per_shard(assignment: dict[str, int], runs: dict[str, dict], model: str) -> dict[int, collections.Counter]:
    """Each shard of `model`'s runs, counted by arm."""
    cells: dict[int, collections.Counter] = {}
    for key, index in assignment.items():
        if runs[key]["model"] == model:
            cells.setdefault(index, collections.Counter())[arm_of(runs[key])] += 1
    return cells


def check_balance(assignment: dict[str, int], runs: dict[str, dict], model: str, first: int, count: int) -> None:
    """The rule's claims for one model's `count` shards, numbered from `first`: see the module docstring."""
    mine = [key for key in runs if runs[key]["model"] == model]
    arms = collections.Counter(arm_of(runs[key]) for key in mine)
    cells = per_shard(assignment, runs, model)
    assert sorted(cells) == list(range(first, first + count))  # every shard of the model gets a run, no other one
    for index, cell in cells.items():
        assert len(mine) // count <= sum(cell.values()) <= -(-len(mine) // count), index
        for arm, n in arms.items():
            assert n // count <= cell[arm] <= -(-n // count), (index, arm)  # the floor or the ceiling of each arm
    for arm, n in arms.items():
        assert sum(1 for cell in cells.values() if cell[arm]) == min(n, count)  # an arm misses a shard only if forced


@pytest.mark.parametrize("count", range(1, 35))
def test_c32_by_run_every_arm_is_balanced_over_any_number_of_shards_up_to_the_models_runs(count):
    """C32: for every shard count from 1 to the model's 34 runs (14, 14 and 6 per arm), each shard gets the floor or the
    ceiling of each arm's runs and of all the model's runs, each run lands in one shard, and an arm is in
    min(its runs, the count) shards."""
    runs = runs_of(CHAIN)
    layout = ModelShards(((MODELS[0], count), (MODELS[1], 3)), by="run")
    assignment = assign_by_run(runs, layout)
    assert set(assignment) == set(runs)  # every run once (a dict holds each key once)
    check_balance(assignment, runs, MODELS[0], 1, count)
    check_balance(assignment, runs, MODELS[1], count + 1, 3)


@pytest.mark.parametrize("count", range(1, 31))
def test_c32_by_run_no_shard_coincides_with_an_arm_of_a_paired_contrast(count):
    """C32: with two arms of 15 runs each, a shard holds both whenever there are at most 15 shards, and its two counts
    are equal when it holds an even number of runs and differ by one when odd; past 15 shards, half the one-run
    shards hold each arm. The even counts catch a deal that alternates the arms round an even number of shards."""
    runs = runs_of(PAIRED, tasks=TASKS[:5], seeds=(1, 2, 3))
    layout = ModelShards(((MODELS[0], count), (MODELS[1], 1)), by="run")
    cells = per_shard(assign_by_run(runs, layout), runs, MODELS[0])
    a, b = (("arm", "a"),), (("arm", "b"),)
    for cell in cells.values():
        held = cell[a] + cell[b]
        assert abs(cell[a] - cell[b]) == held % 2
        if count <= 15:
            assert cell[a]
            assert cell[b]
    singles = [cell for cell in cells.values() if sum(cell.values()) == 1]
    assert sum(1 for cell in singles if cell[a]) == sum(1 for cell in singles if cell[b])


def test_c32_by_run_the_assignment_is_the_same_on_every_host_from_the_plan_alone():
    """C32: the deal depends on the runs and the layout only: their order and a condition's name change nothing, and
    the plan the fingerprint declares gives every shard the runs its manifest has."""
    runs = runs_of(CHAIN)
    layout = ModelShards(((MODELS[0], 5), (MODELS[1], 4)), by="run")
    assignment = assign_by_run(runs, layout)
    assert assign_by_run(dict(reversed(list(runs.items()))), layout) == assignment
    renamed = FactorSchema(CHAIN.declared, CHAIN.order, {f"c-{n}": v for n, v in enumerate(CHAIN.conditions.values())},
                           tasks={"c-2": TASKS[:3]})  # fmt: skip
    assert assign_by_run(runs_of(renamed), layout) == assignment
    manifest = build_manifest(list(CHAIN.expand(TASKS, MODELS, SEEDS)), 11)
    fingerprint = make_fingerprint(CHAIN, TASKS, MODELS, SEEDS)
    fingerprint["caps"]["shards"] = layout.to_json()
    held = []
    for index in range(1, layout.count + 1):
        spec = by_model(ShardSpec(index, layout.count, f"shard-{index}"), layout)
        part = shard_manifest(manifest, spec, layout)
        assert plan_problem(CHAIN, part, fingerprint, spec) is None
        assert [row.seq for row in part.rows] == sorted(row.seq for row in part.rows)  # the plan's order, kept
        assert {assignment[row.key.text] for row in part.rows} == {index}
        held += [row.key.text for row in part.rows]
    assert sorted(held) == sorted(runs)


def test_c32_by_run_a_layout_declares_its_rule_and_reads_back_only_in_its_one_form():
    """C32: `by = "run"` is in the fingerprint's layout and takes `expcore-shard/5`; without it, the layout's JSON and
    rule are those of `expcore-shard/3`, and no other form of the key reads back."""
    per_model = ((MODELS[0], 3), (MODELS[1], 1))
    by_pair, by_run = ModelShards(per_model), ModelShards(per_model, by="run")
    assert ModelShards(per_model, by="pair") == by_pair
    assert by_pair.to_json() == {"per_model": [[MODELS[0], 3], [MODELS[1], 1]]}
    assert by_run.to_json() == {"per_model": [[MODELS[0], 3], [MODELS[1], 1]], "by": "run"}
    assert ModelShards.from_json(by_run.to_json()) == by_run
    assert layout_of({"caps": {"shards": by_run.to_json()}}) == by_run
    assert (by_pair.rule, by_run.rule) == (RULE_BY_MODEL, RULE_BY_RUN)
    assert RULE_BY_RUN in RULES
    assert by_model(ShardSpec(2, 4, "shard-2"), by_run) == ShardSpec(2, 4, "shard-2", RULE_BY_RUN)
    assert ShardSpec.from_json(ShardSpec(2, 4, "shard-2", RULE_BY_RUN).to_json()).rule == RULE_BY_RUN
    for wrong in ({"per_model": [[MODELS[0], 2]], "by": "pair"}, {"per_model": [[MODELS[0], 2]], "by": "task"}):
        with pytest.raises(ShardError):
            ModelShards.from_json(wrong)
    with pytest.raises(ShardError, match="by"):
        ModelShards(per_model, by="task")


def test_c32_by_run_a_spec_of_another_rule_than_the_layouts_is_refused():
    """C32: a shard made under `/3` does not fit a layout by run, nor one under `/5` a layout by pair, and the pairs'
    partition is never asked for runs dealt one by one."""
    runs = runs_of(CHAIN)
    per_model = ((MODELS[0], 3), (MODELS[1], 1))
    by_pair, by_run = ModelShards(per_model), ModelShards(per_model, by="run")
    three, five = ShardSpec(1, 4, "shard-1", RULE_BY_MODEL), ShardSpec(1, 4, "shard-1", RULE_BY_RUN)
    with pytest.raises(ShardError, match="expcore-shard/5"):
        owners(runs, three, by_run)
    with pytest.raises(ShardError, match="expcore-shard/3"):
        owners(runs, five, by_pair)
    with pytest.raises(ShardError, match="declares no"):
        owners(runs, five, None)
    with pytest.raises(ShardError, match="does not fit"):
        owners(runs, ShardSpec(1, 5, "shard-1", RULE_BY_RUN), by_run)
    with pytest.raises(ShardError):
        partition({(f["task"], f["model"], f["seed"]) for f in runs.values()}, five, by_run)
    with pytest.raises(ShardError, match=r"model-heron.*no count"):
        assign_by_run(runs, ModelShards(((MODELS[0], 2),), by="run"))


def test_c32_by_run_the_owner_of_each_run_follows_the_specs_rule():
    """C32: `owners` gives each run its shard under any rule: by its pair under `/2` and `/3`, by itself under `/5`."""
    runs = runs_of(CHAIN)
    pairs = {(f["task"], f["model"], f["seed"]) for f in runs.values()}
    per_model = ((MODELS[0], 3), (MODELS[1], 1))
    by_pair = ModelShards(per_model)
    under = {
        ShardSpec(1, 4, "shard-1"): (None, assign(pairs, 4)),
        ShardSpec(1, 4, "shard-1", RULE_BY_MODEL): (by_pair, assign_by_model(pairs, by_pair)),
    }
    for spec, (layout, dealt) in under.items():
        owner = owners(runs, spec, layout)
        assert {key: owner.of(key, f) for key, f in runs.items()} == {
            key: dealt[(f["task"], f["model"], f["seed"])] for key, f in runs.items()
        }
    layout = ModelShards(per_model, by="run")
    owner = owners(runs, ShardSpec(1, 4, "shard-1", RULE_BY_RUN), layout)
    assert {key: owner.of(key, f) for key, f in runs.items()} == assign_by_run(runs, layout)
    assert owner.of("task=elsewhere", next(iter(runs.values()))) is None  # a run outside the plan has no owner


def _digest(assignment: dict) -> str:
    return hashlib.sha256(json.dumps(sorted([list(k), v] for k, v in assignment.items())).encode()).hexdigest()


def test_c32_the_forms_without_by_run_assign_byte_for_byte_what_they_did_before():
    """C32: the digests of `assign` and `assign_by_model` over a fixed plan, recorded from the core before the rule by
    run existed, and the layout's JSON without `by`: `expcore-shard/2` and `/3` are unchanged."""
    tasks, models = [f"task-{n:03d}" for n in range(40)], ["model-falcon", "model-heron", "model-osprey"]
    pairs = list(itertools.product(tasks, models, [1, 2, 3, 4, 5]))
    layout = ModelShards((("model-heron", 5), ("model-falcon", 3), ("model-osprey", 1)))
    assert _digest(assign(pairs, 7)) == "259b35b8e91741af5838307d82807764d24b1cdd763eaf97cdc03c7c31234ca8"
    assert _digest(assign_by_model(pairs, layout)) == "84db6aa133bfdb758bc5880c60a61c4367b65fb06eefd1d3a8ebc4405ad0fcff"
    assert json.dumps(layout.to_json(), sort_keys=True) == (
        '{"per_model": [["model-heron", 5], ["model-falcon", 3], ["model-osprey", 1]]}'
    )
