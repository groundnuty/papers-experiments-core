"""The budget of a campaign that shards by model (`expcore-shard/3`, #154): who gets which dollars (C30, C32)."""

import re
from decimal import Decimal
from fractions import Fraction

import pytest
from campaign_support import make_manifest, make_schema

from expcore.campaign.attempt_bounds import bound_usd
from expcore.campaign.gate import Budget
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard import RULE_BY_MODEL, ModelShards, ShardError, ShardSpec, micro_usd
from expcore.campaign.shard_budget import planned_runs, reservation_usd, shard_budgets
from expcore.campaign.shard_run import share_settings_by_model
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
    """C32: the five priced shards together need one run's reservation each; the refusal lists each of them with its
    model, and not the two shards of the free model."""
    need = 3 * reserve("model-falcon") + 2 * reserve("model-heron")
    with pytest.raises(
        ShardError, match=r"less than one run's reservation for each of the 5 shards of priced models"
    ) as r:
        shard_budgets(Budget(need * 0.99), LAYOUT, plan(), MODELS, LIMITS)
    assert str(r.value).endswith(
        "The priced shards: shard 1 (model model-falcon), shard 2 (model model-falcon), shard 3 (model model-falcon), "
        "shard 4 (model model-heron), shard 5 (model model-heron)"
    )


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


# --- at a main study's scale: 4 priced models, 150 pairs each, 21 or 27 shards ---------------------------------------

SCALE_MODELS = {
    "model-falcon": model("model-falcon"),
    "model-heron": model("model-heron", Price(0.5, 1.5)),
    "model-kestrel": model("model-kestrel", Price(3.0, 6.0)),
    "model-plover": model("model-plover", Price(0.25, 1.0)),
}
SCALE_LAYOUTS = {
    21: ModelShards((("model-falcon", 6), ("model-heron", 5), ("model-kestrel", 5), ("model-plover", 5))),
    27: ModelShards((("model-falcon", 7), ("model-heron", 7), ("model-kestrel", 7), ("model-plover", 6))),
}


def scale_plan():
    tasks = tuple(f"task-{n:03d}" for n in range(75))
    return make_manifest(make_schema(), 7, tasks=tasks, models=tuple(SCALE_MODELS), seeds=SEEDS)


def reserved_bounds(layout: ModelShards) -> dict[int, Fraction]:
    """Each shard's planned runs times one run's reservation of its model: what `max_usd` is split by."""
    runs = planned_runs(scale_plan(), layout)
    return {i: runs[i] * Fraction(reservation_usd(SCALE_MODELS[layout.at(i)[0]], LIMITS)) for i in runs}


@pytest.mark.parametrize("count", sorted(SCALE_LAYOUTS))
@pytest.mark.parametrize("fraction", [Fraction(1, 10), Fraction(1, 2), Fraction(1), Fraction(5, 2)])
def test_c32_at_21_and_27_shards_the_parts_sum_within_max_usd_and_each_pays_at_least_one_run(count, fraction):
    """C32: a main study's plan split by model into 21 or 27 shards: for a `max_usd` from a tenth of what the plan
    reserves to more than all of it, the parts sum to at most it, in micro-dollars, and each pays a run of its model."""
    layout = SCALE_LAYOUTS[count]
    max_usd = float(sum(reserved_bounds(layout).values()) * fraction)
    parts = shard_budgets(Budget(max_usd), layout, scale_plan(), SCALE_MODELS, LIMITS)
    assert sorted(parts) == list(range(1, count + 1))
    assert micro_usd(max_usd) - count <= total_micro(parts) <= micro_usd(max_usd)
    for index, part in parts.items():
        assert part["max_usd"] >= reservation_usd(SCALE_MODELS[layout.at(index)[0]], LIMITS), index


@pytest.mark.parametrize("count", sorted(SCALE_LAYOUTS))
def test_c32_a_max_usd_above_what_the_plan_reserves_pays_every_shards_reservation_of_every_one_of_its_runs(count):
    """C32: shares are never pooled, and a study's hold is not released, so a shard whose share runs out keeps its
    runs unfinished for good. With a `max_usd` of a little more than the plan's reserved total, every shard's part of
    `max_usd` pays the reservation of every run it plans once, whatever those runs cost. Only that part: a
    `per_model_usd` ceiling is held on its own, and probes, top-ups, retries and interrupted attempts take more."""
    layout = SCALE_LAYOUTS[count]
    bounds = reserved_bounds(layout)
    parts = shard_budgets(
        Budget(float(sum(bounds.values()) * Fraction(1001, 1000))), layout, scale_plan(), SCALE_MODELS, LIMITS
    )
    assert all(Fraction(parts[index]["max_usd"]) >= bound for index, bound in bounds.items())


def test_c32_a_campaign_of_free_models_only_gives_every_shard_nothing_and_is_not_refused():
    """C32: no shard is priced, so none needs a reservation and none gets dollars."""
    models = {"model-osprey": MODELS["model-osprey"], "model-plover": model("model-plover", None, free=True)}
    layout = ModelShards((("model-osprey", 1), ("model-plover", 1)))
    parts = shard_budgets(Budget(1.0), layout, plan(tuple(models)), models, LIMITS)
    assert [part["max_usd"] for part in parts.values()] == [0.0, 0.0]


# --- one priced model beside two that cost nothing, one shard each: a pilot that prices a single model ---------------

ONE_PRICED = ModelShards((("model-falcon", 1), ("model-osprey", 1), ("model-plover", 1)))
FREE_WAYS = {
    "marked free": (model("model-osprey", None, free=True), model("model-plover", None, free=True)),
    "priced at zero": (model("model-osprey", Price(0.0, 0.0)), model("model-plover", Price(0.0, 0.0))),
}
"""The two ways a model costs nothing: marked free, or priced at 0 with its context window."""


def one_priced(way: str) -> dict[str, ModelSpec]:
    """falcon is priced; osprey and plover cost nothing, in the way `way` names."""
    osprey, plover = FREE_WAYS[way]
    return {"model-falcon": MODELS["model-falcon"], "model-osprey": osprey, "model-plover": plover}


def one_priced_parts(way: str, budget: Budget) -> dict[int, dict]:
    models = one_priced(way)
    return shard_budgets(budget, ONE_PRICED, plan(tuple(models)), models, LIMITS)


@pytest.mark.parametrize("way", FREE_WAYS)
@pytest.mark.parametrize(
    ("max_usd", "ceiling", "whole_max", "whole_ceiling"),
    [
        (50.0, 20.0, 50.0, 20.0),
        (1234.567891, 617.283945, 1234.567891, 617.283945),
        (7.1234567, 3.5617283, 7.123456, 3.561728),  # past the micro-dollar: rounded down, as every part is
    ],
    ids=["whole dollars", "micro-dollars", "past a micro-dollar"],
)
def test_c32_the_one_priced_models_only_shard_takes_the_whole_of_max_usd_and_its_ceiling_and_free_shards_get_none(
    way, max_usd, ceiling, whole_max, whole_ceiling
):
    """C32: one priced model with one shard, beside two models that cost nothing with one shard each: the priced
    shard's part is the whole `max_usd` and the whole `per_model_usd` ceiling of its model, in whole micro-dollars; each
    free shard's part is 0 and has no ceiling, whether its model is marked free or priced at 0."""
    parts = one_priced_parts(way, Budget(max_usd, {"model-falcon": ceiling}))
    assert parts == {
        1: {"max_usd": whole_max, "per_model_usd": {"model-falcon": whole_ceiling}},
        2: {"max_usd": 0.0, "per_model_usd": {}},
        3: {"max_usd": 0.0, "per_model_usd": {}},
    }


@pytest.mark.parametrize("way", FREE_WAYS)
def test_c32_in_the_one_priced_layout_the_free_shards_run_with_no_budget_and_the_priced_one_on_the_whole_of_it(way):
    """C32: a shard whose part of `max_usd` is 0 runs with no `Budget` (one of 0 dollars would hold every run) and
    records its 0; the priced shard's `Budget` holds the whole `max_usd` and ceiling; each has a cap of one run."""
    budget = Budget(50.0, {"model-falcon": 20.0})
    parts = one_priced_parts(way, budget)
    campaign = SchedulerConfig(unit_caps={"unit-falcon": 3, "unit-osprey": 3, "unit-plover": 3}, budget=budget)
    shared = {}
    for index, unit in ((1, "unit-falcon"), (2, "unit-osprey"), (3, "unit-plover")):
        spec = ShardSpec(index, 3, f"shard-{index}", RULE_BY_MODEL)
        shared[index], recorded = share_settings_by_model(campaign, spec, ONE_PRICED, unit, parts[index])
        assert (shared[index].unit_caps, recorded) == ({unit: 1}, {"unit_caps": {unit: 1}, "budget": parts[index]})
    assert (shared[1].budget.max_usd, dict(shared[1].budget.per_model_usd)) == (50.0, {"model-falcon": 20.0})
    assert [shared[2].budget, shared[3].budget] == [None, None]


@pytest.mark.parametrize("way", FREE_WAYS)
def test_c32_a_max_usd_below_one_runs_reservation_of_the_one_priced_model_is_refused_naming_its_shard_and_model(way):
    """C32: the one priced shard needs one run's reservation of `max_usd` before its first run; a total below it is
    refused, with the count of priced shards, the reservation, and the priced shard with its model, and not the free
    ones; a total a little above it is not refused, and the shard holds all of it."""
    need = reservation_usd(one_priced(way)["model-falcon"], LIMITS)
    refusal = (
        r"\[budget\] max_usd .* less than one run's reservation for each of the 1 shards of priced models together "
        + re.escape(f"({need:.6f} USD)")
    )
    with pytest.raises(ShardError, match=refusal) as refused:
        one_priced_parts(way, Budget(need * 0.99))
    assert str(refused.value).endswith("The priced shards: shard 1 (model model-falcon)")
    barely = need * 1.01
    assert micro_usd(one_priced_parts(way, Budget(barely))[1]["max_usd"]) == micro_usd(barely)


def test_c32_a_max_usd_below_the_reservations_of_two_priced_shards_is_refused_naming_both_and_not_the_free_one():
    """C32: two priced models with one shard each, a free one between them: the refusal lists shards 1 and 3 with their
    models, in index order, and not shard 2, which costs nothing and needs no reservation."""
    layout = ModelShards((("model-falcon", 1), ("model-osprey", 1), ("model-heron", 1)))
    need = reserve("model-falcon") + reserve("model-heron")
    with pytest.raises(ShardError, match=r"for each of the 2 shards of priced models together") as refused:
        shard_budgets(Budget(need * 0.99), layout, plan(), MODELS, LIMITS)
    assert str(refused.value).endswith("The priced shards: shard 1 (model model-falcon), shard 3 (model model-heron)")


@pytest.mark.parametrize("way", FREE_WAYS)
def test_c32_a_ceiling_of_the_one_priced_model_below_one_runs_reservation_is_refused_naming_shard_and_model(way):
    """C32: a `per_model_usd` ceiling above 0 and below one run's reservation would hold the priced shard before its
    first run: refused, naming the shard, the model, the ceiling's key and both figures; a little above it is not."""
    need = reservation_usd(one_priced(way)["model-falcon"], LIMITS)
    refusal = r"shard 1 \(model model-falcon\) gets .* of \[budget.per_model_usd\] model-falcon, " + re.escape(
        f"less than one run's reservation of {need:.6f} USD"
    )
    with pytest.raises(ShardError, match=refusal):
        one_priced_parts(way, Budget(50.0, {"model-falcon": need * 0.5}))
    barely = need * 1.01
    part = one_priced_parts(way, Budget(50.0, {"model-falcon": barely}))[1]["per_model_usd"]["model-falcon"]
    assert micro_usd(part) == micro_usd(barely)
