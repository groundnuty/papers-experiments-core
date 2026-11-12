"""The budget of a campaign that shards by model (`expcore-shard/3`, #154): who gets which dollars (C30, C32)."""

from decimal import Decimal
from fractions import Fraction

import pytest
from campaign_support import make_manifest, make_schema

from expcore.campaign.attempt_bounds import bound_usd
from expcore.campaign.gate import Budget
from expcore.campaign.shard import ModelShards, ShardError, micro_usd
from expcore.campaign.shard_budget import planned_runs, reservation_usd, shard_budgets
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price

LIMITS = Limits(max_tool_rounds=4, time_cap_s=30.0, token_budget=20_000, output_cap_chars=500, max_output_tokens=256)
TASKS = tuple(f"task-{n:02d}" for n in range(10))
SEEDS = (1, 2)


DEAR = Price(1.0, 2.0)


def model(key: str, price: Price | None = DEAR, *, free: bool = False) -> ModelSpec:
    """An invented stub model with a context window: priced, free, or with no price at all."""
    return ModelSpec(key=key, backend="stub", api_id=key, status="frozen", price=price, free=free, context_tokens=8192)


MODELS = {
    "model-falcon": model("model-falcon"),
    "model-heron": model("model-heron", Price(0.5, 1.5)),
    "model-osprey": model("model-osprey", None, free=True),
}
LAYOUT = ModelShards((("model-falcon", 3), ("model-heron", 2), ("model-osprey", 2)))


def plan(models=tuple(MODELS)):
    return make_manifest(make_schema(), 7, tasks=TASKS, models=models, seeds=SEEDS)


def reserve(name: str) -> float:
    return reservation_usd(MODELS[name], LIMITS)


def total_micro(parts) -> int:
    return sum(micro_usd(part["max_usd"]) for part in parts.values())


def test_c32_a_runs_reservation_is_the_larger_of_its_bounds_with_and_without_the_reply_maximum():
    """C32: which bound the gate uses is known only after admission, so a share is held to the larger one."""
    spec = MODELS["model-falcon"]
    both = [bound_usd(spec, LIMITS, reply_capped=capped) for capped in (False, True)]
    assert reservation_usd(spec, LIMITS) == max(both) > 0
    assert reservation_usd(MODELS["model-osprey"], LIMITS) == 0.0
    assert reservation_usd(model("model-plover", None), LIMITS) is None


def test_c32_max_usd_is_split_over_the_priced_shards_by_their_reserved_bound_and_free_shards_get_nothing():
    """C32: each priced shard's part of `max_usd` follows its planned runs times its model's reservation, in whole
    micro-dollars rounded down, so the parts sum to at most the total; the free model's shards get 0."""
    parts = shard_budgets(Budget(50.0), LAYOUT, plan(), MODELS, LIMITS)
    runs = planned_runs(plan(), LAYOUT)
    assert sorted(parts) == list(range(1, 8))
    assert total_micro(parts) <= micro_usd(50.0)
    assert micro_usd(50.0) - total_micro(parts) < 7  # at most one micro-dollar lost to each shard's rounding
    assert [parts[i]["max_usd"] for i in (6, 7)] == [0.0, 0.0]
    owner = {i: LAYOUT.at(i)[0] for i in parts}
    for index in range(1, 6):
        assert parts[index]["max_usd"] >= reserve(owner[index])
    rate = {i: Fraction(parts[i]["max_usd"]) / (runs[i] * Fraction(reserve(owner[i]))) for i in range(1, 6)}
    assert max(rate.values()) - min(rate.values()) < Fraction(1, 10**4)  # the same dollars per reserved dollar


@pytest.mark.parametrize("max_usd", [3.0, 7.25, 50.0, 1234.567891])
def test_c32_the_parts_of_max_usd_never_sum_past_it_and_no_priced_shard_is_below_one_run(max_usd):
    """C32: whatever the total, the parts are within it, and each priced shard can pay for at least one run."""
    parts = shard_budgets(Budget(max_usd), LAYOUT, plan(), MODELS, LIMITS)
    assert sum(Decimal(str(part["max_usd"])) for part in parts.values()) <= Decimal(str(max_usd))
    for index, part in parts.items():
        name = LAYOUT.at(index)[0]
        assert part["max_usd"] == 0.0 if name == "model-osprey" else part["max_usd"] >= reserve(name)


def test_c32_a_max_usd_too_small_for_every_priced_shard_to_run_once_is_refused():
    """C32: the five priced shards together need one run's reservation each."""
    need = 3 * reserve("model-falcon") + 2 * reserve("model-heron")
    with pytest.raises(ShardError, match=r"less than one run's reservation for each of the 5 shards of priced models"):
        shard_budgets(Budget(need * 0.99), LAYOUT, plan(), MODELS, LIMITS)


def test_c32_a_priced_shard_below_one_runs_reservation_is_refused_even_when_the_total_would_cover_every_shard():
    """C32: a model of 20 shards of one pair each beside a model of one shard of 20 pairs: a total of exactly one run
    per shard goes mostly to the large shard, by its reserved bound, and leaves each small one less than a run."""
    layout = ModelShards((("model-falcon", 20), ("model-heron", 1)))
    models = {name: MODELS[name] for name in ("model-falcon", "model-heron")}
    need = 20 * reserve("model-falcon") + reserve("model-heron")
    with pytest.raises(ShardError, match=r"shard \d+ \(model model-falcon\) gets .* of \[budget\] max_usd, less than"):
        shard_budgets(Budget(need + 0.001), layout, plan(tuple(models)), models, LIMITS)


def test_c32_a_model_whose_price_is_unknown_is_refused_naming_it():
    """C32: what its runs reserve is unknown, so no part of the money can be set for its shards."""
    models = {**MODELS, "model-osprey": model("model-osprey", None)}
    with pytest.raises(ShardError, match="model model-osprey has no price and is not marked free"):
        shard_budgets(Budget(50.0), LAYOUT, plan(), models, LIMITS)


def test_c32_a_models_ceiling_is_split_over_its_own_shards_and_a_ceiling_of_zero_holds_each_of_them():
    """C32: a shard keeps only its own model's ceiling, its part of it; 0 holds the model in every one of its shards."""
    budget = Budget(50.0, {"model-falcon": 20.0, "model-heron": 0})
    parts = shard_budgets(budget, LAYOUT, plan(), MODELS, LIMITS)
    falcon = [parts[i]["per_model_usd"] for i in (1, 2, 3)]
    assert all(set(ceiling) == {"model-falcon"} for ceiling in falcon)
    assert sum(Decimal(str(c["model-falcon"])) for c in falcon) <= Decimal("20.0")
    assert [parts[i]["per_model_usd"] for i in (4, 5)] == [{"model-heron": 0.0}] * 2
    assert [parts[i]["per_model_usd"] for i in (6, 7)] == [{}, {}]


def test_c32_a_models_ceiling_whose_part_cannot_pay_for_one_run_is_refused():
    """C32: a ceiling above 0 whose part is below one run would hold that shard before its first run."""
    budget = Budget(50.0, {"model-heron": reserve("model-heron") * 1.5})
    with pytest.raises(ShardError, match=r"\(model model-heron\) gets .* of \[budget.per_model_usd\] model-heron"):
        shard_budgets(budget, LAYOUT, plan(), MODELS, LIMITS)


def test_c32_a_campaign_of_free_models_only_gives_every_shard_nothing_and_is_not_refused():
    """C32: no shard is priced, so none needs a reservation and none gets dollars."""
    models = {"model-osprey": MODELS["model-osprey"], "model-plover": model("model-plover", None, free=True)}
    layout = ModelShards((("model-osprey", 1), ("model-plover", 1)))
    parts = shard_budgets(Budget(1.0), layout, plan(tuple(models)), models, LIMITS)
    assert [part["max_usd"] for part in parts.values()] == [0.0, 0.0]
