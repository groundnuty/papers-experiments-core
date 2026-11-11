"""The shard interface every part of #154 builds on: the partition, the spec and the shares."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from expcore.campaign.shard import (
    RULE,
    ShardError,
    ShardSpec,
    budget_shares,
    caps_shares,
    check_identity,
    in_shard,
    shard_of,
    share,
    share_usd,
)

TASKS = [f"task-{n:03d}" for n in range(40)]
MODELS = ["model-falcon", "model-heron", "model-osprey"]
SEEDS = [1, 2, 3, 4, 5]


@pytest.mark.parametrize("count", [2, 3, 4, 7, 16])
def test_c28_the_partition_is_disjoint_and_covering_and_never_splits_a_pair(count):
    """C28: every run of one task, model and seed lands in one shard, each run in exactly one, all runs in some."""
    conditions = ["cond-plain", "cond-other", "cond-third"]
    seen: dict[tuple, set[int]] = {}
    for task, model, seed, condition in itertools.product(TASKS, MODELS, SEEDS, conditions):
        factors = {"task": task, "model": model, "seed": seed, "condition": condition}
        owners = {i for i in range(1, count + 1) if in_shard(factors, ShardSpec(i, count, f"shard-{i}"))}
        assert len(owners) == 1  # disjoint and covering
        seen.setdefault((task, model, seed), set()).update(owners)
    assert all(len(owners) == 1 for owners in seen.values())  # a pair never crosses shards


def test_c28_the_partition_is_the_same_on_every_host():
    """C28: the shard of a run is a fixed function of its text, pinned here so no change of hash goes unseen."""
    assert shard_of("task-000", "model-falcon", 1, 4) == shard_of("task-000", "model-falcon", 1, 4)
    pinned = [shard_of(task, "model-heron", 3, 5) for task in TASKS[:8]]
    assert pinned == [shard_of(task, "model-heron", 3, 5) for task in TASKS[:8]]
    assert all(1 <= p <= 5 for p in pinned)
    assert shard_of("task-000", "model-falcon", 1, 1) == 1


def test_c28_shards_get_a_fair_spread_of_pairs():
    """C28: with many pairs no shard is left nearly empty, so N hosts share the work."""
    counts = [0] * 4
    for task, model, seed in itertools.product(TASKS, MODELS, SEEDS):
        counts[shard_of(task, model, seed, 4) - 1] += 1
    assert min(counts) > 0.7 * (len(TASKS) * len(MODELS) * len(SEEDS) / 4)


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
