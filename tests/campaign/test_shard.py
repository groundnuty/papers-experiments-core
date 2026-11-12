"""The shard interface every part of #154 builds on: the partition, the spec and the shares."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from expcore.campaign.shard import (
    RULE,
    RULE_BY_MODEL,
    RULES,
    ModelShards,
    ShardError,
    ShardSpec,
    assign,
    assign_by_model,
    budget_shares,
    by_model,
    caps_shares,
    check_identity,
    in_shard,
    is_default_label,
    layout_of,
    partition,
    share,
    share_usd,
)

TASKS = [f"task-{n:03d}" for n in range(40)]
MODELS = ["model-falcon", "model-heron", "model-osprey"]
SEEDS = [1, 2, 3, 4, 5]


PAIRS = list(itertools.product(TASKS, MODELS, SEEDS))


@pytest.mark.parametrize("count", [2, 3, 4, 7, 16])
def test_c28_the_partition_is_disjoint_and_covering_and_never_splits_a_pair(count):
    """C28: every run of one task, model and seed lands in one shard, each run in exactly one, all runs in some."""
    assignment = assign(PAIRS, count)
    conditions = ["cond-plain", "cond-other", "cond-third"]
    seen: dict[tuple, set[int]] = {}
    for task, model, seed, condition in itertools.product(TASKS, MODELS, SEEDS, conditions):
        factors = {"task": task, "model": model, "seed": seed, "condition": condition}
        owners = {i for i in range(1, count + 1) if in_shard(factors, ShardSpec(i, count, f"shard-{i}"), assignment)}
        assert len(owners) == 1  # disjoint and covering
        seen.setdefault((task, model, seed), set()).update(owners)
    assert all(len(owners) == 1 for owners in seen.values())  # a pair never crosses shards


@pytest.mark.parametrize("count", [2, 3, 4, 7, 16])
def test_c28_each_shard_gets_the_floor_or_the_ceiling_of_each_models_pairs(count):
    """C28: per model, shard sizes differ by at most one, so no shard runs much longer than the others."""
    assignment = assign(PAIRS, count)
    for model in MODELS:
        sizes = [sum(1 for p, shard in assignment.items() if p[1] == model and shard == i) for i in range(1, count + 1)]
        assert max(sizes) - min(sizes) <= 1
        assert sum(sizes) == len(TASKS) * len(SEEDS)


def test_c28_the_remainders_do_not_pile_onto_one_shard():
    """C28: when n/N leaves a remainder for several models, the shards that take it differ from model to model."""
    models = [f"model-{n:02d}" for n in range(12)]
    pairs = list(itertools.product(TASKS[:10], models, [1]))  # 10 pairs per model over 4 shards: remainder 2 each
    totals = [0] * 4
    for shard in assign(pairs, 4).values():
        totals[shard - 1] += 1
    assert max(totals) - min(totals) < 12  # every remainder on the same two shards would put them 12 apart


def test_c28_the_assignment_is_the_same_on_every_host_and_in_any_order():
    """C28: the assignment depends only on the plan's pairs and the count, never on their order or the host."""
    assert assign(PAIRS, 4) == assign(list(reversed(PAIRS)), 4)
    assert set(assign(PAIRS, 1).values()) == {1}


def test_c28_a_run_outside_the_assigned_plan_is_refused():
    """C28: a run whose pair the assignment does not hold was not in the plan the shards were made from."""
    with pytest.raises(ShardError):
        in_shard({"task": "task-999", "model": "model-falcon", "seed": 1}, ShardSpec(1, 2, "shard-1"), assign(PAIRS, 2))


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("0/3", None),
        ("4/3", None),
        ("1/1", None),
        ("a/b", None),
        ("2/4", "Host.Example"),
        ("2/4", ""),
        ("2/4", "x" * 65),
    ],
)
def test_c28_a_spec_no_shard_can_have_is_refused(text, label):
    """C28: an index out of range, one shard only, a malformed i/N, or a label that is not a plain lower-case name."""
    with pytest.raises(ShardError):
        ShardSpec.parse(text, label)


@pytest.mark.parametrize("label", ["node1.lab.example.org", "10.0.0.5", "node_one", "lab.rack-b"])
def test_c28_a_label_with_a_dot_or_an_underscore_is_refused_by_default_and_accepted_when_wide(label):
    """C28: a fully qualified host name or an IPv4 address cannot be used as a label by accident; the wider form, the
    one that allows '.' and '_', is asked for by name."""
    with pytest.raises(ShardError, match=r"no '\.'") as refused:
        ShardSpec.parse("2/4", label)
    assert label not in str(refused.value)
    with pytest.raises(ShardError, match=r"no '\.'"):
        ShardSpec(2, 4, label)
    spec = ShardSpec.parse("2/4", label, wide_label=True)
    assert spec.label == label
    assert spec.to_json() == {"rule": RULE, "index": 2, "count": 4, "label": label}


@pytest.mark.parametrize("label", ["north", "rack-b", "shard-2", "a", "9-lab", "x" * 64])
def test_c28_a_default_label_is_lower_case_letters_digits_and_hyphens(label):
    """C28: what the default rule keeps: 1 to 64 of `[a-z0-9-]`, starting with a letter or digit."""
    assert ShardSpec.parse("1/2", label).label == label
    assert is_default_label(label)


@pytest.mark.parametrize("label", ["-north", "North", "", "x" * 65, "node one", ".node"])
def test_c28_the_wide_form_still_checks_the_start_the_length_and_the_case(label):
    """C28: the override allows '.' and '_' and nothing more."""
    with pytest.raises(ShardError, match="lower-case"):
        ShardSpec.parse("1/2", label, wide_label=True)


def test_c28_a_spec_read_back_from_a_store_keeps_the_wide_label_it_pinned():
    """C28: a store started with a wide label still resumes and merges: `from_json` reads it, equal to the spec made
    with the override, and its json is the four keys it always had."""
    spec = ShardSpec.parse("3/5", "rack.b_2", wide_label=True)
    assert ShardSpec.from_json(spec.to_json()) == spec
    assert set(spec.to_json()) == {"rule", "index", "count", "label"}
    with pytest.raises(ShardError, match="lower-case"):
        ShardSpec.from_json({**spec.to_json(), "label": "Rack.B"})


def test_c28_a_spec_round_trips_and_defaults_its_label():
    """C28: the spec the store keeps reads back equal, and the label defaults to shard-i."""
    spec = ShardSpec.parse("2/4")
    assert spec.label == "shard-2"
    assert spec.rule == RULE
    assert ShardSpec.from_json(spec.to_json()) == spec
    with pytest.raises(ShardError):
        ShardSpec.from_json({**spec.to_json(), "rule": "another rule"})
    with pytest.raises(ShardError):
        ShardSpec.from_json({**spec.to_json(), "extra": 1})


@pytest.mark.parametrize(("total", "count"), [(20, 3), (20, 20), (7, 2), (100, 7), (5, 5)])
def test_c30_whole_number_shares_sum_to_the_total(total, count):
    """C30: the N shares of a cap sum to it exactly and differ by at most one."""
    shares = [share(total, ShardSpec(i, count, f"shard-{i}"), what="cap") for i in range(1, count + 1)]
    assert sum(shares) == total
    assert max(shares) - min(shares) <= 1


def test_c30_a_total_below_the_shard_count_is_refused():
    """C30: a cap of 3 over 4 shards would leave a shard with none, so it is refused, naming what."""
    with pytest.raises(ShardError, match=r"unit_caps\.relay-falcon"):
        share(3, ShardSpec(1, 4, "shard-1"), what="unit_caps.relay-falcon")


@pytest.mark.parametrize(("total", "count"), [(15.0, 4), (0.1, 3), (25.37, 7), (1.000001, 2)])
def test_c30_dollar_shares_never_sum_past_the_total(total, count):
    """C30: dollar shares are whole micro-dollars rounded down, so together they never exceed the budget."""
    shares = [share_usd(total, ShardSpec(i, count, f"shard-{i}"), what="max_usd") for i in range(1, count + 1)]
    assert sum(Decimal(str(s)) for s in shares) <= Decimal(str(total))
    assert Decimal(str(total)) - sum(Decimal(str(s)) for s in shares) < Decimal("0.000001") * count


def test_c29_an_environment_identity_is_names_to_digests_only():
    """C29: an identity is sorted names mapped to SHA-256 hex digests; a path, a host name or a short value is
    refused."""
    digest = "a" * 64
    assert list(check_identity({"layout": digest, "data.space-one": digest})) == ["data.space-one", "layout"]
    for bad in ({"layout": "abc"}, {"Layout": digest}, {"/home/someone": digest}, {"layout": digest.upper()}):
        with pytest.raises(ShardError):
            check_identity(bad)
    assert check_identity({}) == {}


def test_c29_names_of_two_types_are_a_refusal_and_not_a_type_error():
    """C29: sorting names of different types raised a TypeError that came out as a traceback."""
    digest = "a" * 64
    for bad in ({1: digest, "layout": digest}, {"layout": digest, None: digest}, {3: digest}):
        with pytest.raises(ShardError, match="identity name"):
            check_identity(bad)


@pytest.mark.parametrize("count", [2, 3, 7])
def test_c30_a_model_ceiling_of_zero_holds_it_entirely_so_every_shard_shares_zero(count):
    """C30: a `per_model_usd` ceiling of 0 is not "less than the N shards": it is zero for every shard, beside the
    shares of the other ceilings."""
    for spec in (ShardSpec(i, count, f"shard-{i}") for i in range(1, count + 1)):
        parts = budget_shares(15.0, {"model-falcon": 0, "model-heron": 5.0}, spec)
        assert parts["per_model_usd"]["model-falcon"] == 0.0
        assert 0.0 < parts["per_model_usd"]["model-heron"] <= 5.0 / count + 0.000001
        assert parts["max_usd"] > 0.0


def test_c30_a_zero_total_is_refused_everywhere_but_a_model_ceiling():
    """C30: a zero `max_usd` or a zero whole-number cap still leaves a shard nothing, and the message says the total."""
    spec = ShardSpec(1, 2, "shard-1")
    with pytest.raises(ShardError, match=r"\[budget\] max_usd is 0, less than the 2 shards"):
        budget_shares(0.0, {}, spec)
    with pytest.raises(ShardError, match="less than the 2 shards"):
        share(0, spec, what="cap")
    assert share_usd(0, spec, what="ceiling", allow_zero=True) == 0.0


def test_c30_caps_and_budget_shares_over_all_shards_sum_within_the_totals():
    """C30: each unit's cap is shared exactly; the budget and each model's ceiling never in sum exceed their totals."""
    caps = {"relay-falcon": 20, "relay-heron": 9}
    specs = [ShardSpec(i, 3, f"shard-{i}") for i in (1, 2, 3)]
    assert [sum(caps_shares(caps, s)[unit] for s in specs) for unit in caps] == [20, 9]
    budgets = [budget_shares(15.0, {"model-falcon": 5.0}, s) for s in specs]
    assert sum(Decimal(str(b["max_usd"])) for b in budgets) <= Decimal("15.0")
    assert sum(Decimal(str(b["per_model_usd"]["model-falcon"])) for b in budgets) <= Decimal("5.0")
    with pytest.raises(ShardError, match=r"relay-heron"):
        caps_shares({"relay-heron": 2}, specs[0])


# --- expcore-shard/3: shards by model ---------------------------------------------------------------------------------

LAYOUT = ModelShards((("model-osprey", 3), ("model-falcon", 1), ("model-heron", 2)))


def test_c31_shard_i_maps_to_a_model_and_its_place_among_that_models_shards_in_declared_order():
    """C31: the models in declared order, never sorted, each taking as many shards as its count."""
    assert LAYOUT.count == 6
    assert [LAYOUT.at(i) for i in range(1, 7)] == [
        ("model-osprey", 1, 3),
        ("model-osprey", 2, 3),
        ("model-osprey", 3, 3),
        ("model-falcon", 1, 1),
        ("model-heron", 1, 2),
        ("model-heron", 2, 2),
    ]
    assert ModelShards.from_json(LAYOUT.to_json()) == LAYOUT
    assert LAYOUT.to_json() == {"per_model": [["model-osprey", 3], ["model-falcon", 1], ["model-heron", 2]]}
    with pytest.raises(ShardError):
        LAYOUT.at(7)


@pytest.mark.parametrize(
    "per_model",
    [(), (("model-falcon", 1),), (("model-falcon", 0), ("model-heron", 2)), (("model-falcon", 1), ("model-falcon", 1))],
    ids=["empty", "one shard", "a zero count", "a model twice"],
)
def test_c31_a_layout_no_campaign_can_shard_by_is_refused(per_model):
    """C31: at least 2 shards, each model once with at least one shard."""
    with pytest.raises(ShardError, match="per_model"):
        ModelShards(per_model)


def test_c31_a_shard_serves_its_own_model_only_and_a_models_shards_differ_by_at_most_one_pair():
    """C31: every pair of a model lands in one of that model's shards, balanced as expcore-shard/2 balances them."""
    assignment = assign_by_model(PAIRS, LAYOUT)
    assert set(assignment) == set(PAIRS)
    for index in range(1, LAYOUT.count + 1):
        model, _, _ = LAYOUT.at(index)
        held = [pair for pair, shard in assignment.items() if shard == index]
        assert held
        assert {pair[1] for pair in held} == {model}
    for model, count in LAYOUT.per_model:
        first = next(i for i in range(1, 7) if LAYOUT.at(i)[0] == model)
        sizes = [sum(1 for s in assignment.values() if s == first + k) for k in range(count)]
        assert max(sizes) - min(sizes) <= 1
        assert sum(sizes) == len(TASKS) * len(SEEDS)
        mine = [pair for pair in PAIRS if pair[1] == model]
        assert {p: assignment[p] - first + 1 for p in mine} == assign(mine, count)  # the deal of expcore-shard/2


def test_c31_a_pair_never_crosses_shards_under_the_rule_by_model():
    """C31: every condition of one task, model and seed lands in one shard, each run in exactly one."""
    assignment = assign_by_model(PAIRS, LAYOUT)
    specs = [ShardSpec(i, LAYOUT.count, f"shard-{i}", RULE_BY_MODEL) for i in range(1, LAYOUT.count + 1)]
    for task, model, seed, condition in itertools.product(TASKS, MODELS, SEEDS, ["cond-plain", "cond-other"]):
        factors = {"task": task, "model": model, "seed": seed, "condition": condition}
        assert sum(in_shard(factors, spec, assignment) for spec in specs) == 1
    assert assign_by_model(list(reversed(PAIRS)), LAYOUT) == assignment  # the same on every host, in any order


def test_c31_a_model_of_the_plan_without_a_count_is_refused():
    """C31: its pairs would be in no shard."""
    with pytest.raises(ShardError, match=r"model-heron.*no count"):
        assign_by_model(PAIRS, ModelShards((("model-osprey", 1), ("model-falcon", 1))))


def test_c31_the_partition_follows_the_specs_rule_and_refuses_a_spec_that_does_not_fit_the_layout():
    """C31: expcore-shard/2 assigns as it did; /3 needs the layout, with N its sum; /2 is refused beside a layout."""
    two, three = ShardSpec(1, 6, "shard-1"), ShardSpec(1, 6, "shard-1", RULE_BY_MODEL)
    assert partition(PAIRS, two, None) == assign(PAIRS, 6)
    assert partition(PAIRS, three, LAYOUT) == assign_by_model(PAIRS, LAYOUT)
    with pytest.raises(ShardError, match="declares no"):
        partition(PAIRS, three, None)
    with pytest.raises(ShardError, match="made under expcore-shard/2"):
        partition(PAIRS, two, LAYOUT)
    with pytest.raises(ShardError, match=r"--shard 1/5 does not fit .* declares 6 shards: give --shard i/6"):
        partition(PAIRS, ShardSpec(1, 5, "shard-1", RULE_BY_MODEL), LAYOUT)


def test_c31_a_spec_of_either_rule_round_trips_and_the_command_lines_spec_takes_the_rule_by_model():
    """C31: a store pinned under expcore-shard/2 still reads back; `by_model` keeps the index, the count and the label,
    a wide one included, and refuses an N that is not the layout's sum."""
    for rule in RULES:
        spec = ShardSpec(2, 6, "rack.b", rule, wide_label=True)
        assert ShardSpec.from_json(spec.to_json()) == spec
    wide = ShardSpec.parse("2/6", "rack.b", wide_label=True)
    assert by_model(wide, LAYOUT) == ShardSpec(2, 6, "rack.b", RULE_BY_MODEL, wide_label=True)
    with pytest.raises(ShardError, match="declares 6 shards"):
        by_model(ShardSpec.parse("2/4"), LAYOUT)


def test_c31_a_fingerprint_without_a_layout_declares_none():
    """C31: the layout is read from the fingerprint's caps, where a campaign without [shards] has none."""
    assert layout_of({"caps": {"max_attempts": 3}}) is None
    assert layout_of({"caps": {"max_attempts": 3, "shards": LAYOUT.to_json()}}) == LAYOUT
    with pytest.raises(ShardError):
        layout_of({"caps": {"shards": {"per_model": {"model-falcon": 2}}}})
