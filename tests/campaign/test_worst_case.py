"""What one attempt is reserved: the most its caps let it cost, one request past the token cap included (C25)."""

import dataclasses
import random

import pytest
from campaign_support import NOW, planned_store

from expcore.campaign.attempt_bounds import bound_usd, cap_bound_usd, overshoot_usd
from expcore.campaign.gate import Budget, BudgetGate
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price, PriceTier, TieredPrice

LIMITS = Limits(
    max_tool_rounds=30, time_cap_s=600.0, token_budget=10_000_000, output_cap_chars=8000, max_output_tokens=8000
)
TIERED = TieredPrice(
    (PriceTier(128_000, Price(0.25, 1.25, 0.3125, 0.025)), PriceTier(None, Price(0.5, 2.5, 0.625, 0.05)))
)


def model(backend: str = "openai_compat", price=TIERED, context: int | None = 200_000, **extra) -> ModelSpec:
    """An invented model of a core backend, or the stub, with a price and a context window."""
    endpoint = {} if backend == "stub" else {"base_url": "https://llm.example.invalid/v1", "key_env": "TEST_KEY"}
    return ModelSpec(
        key="m1", backend=backend, api_id="vendor/m-1", status="frozen", price=price, context_tokens=context,
        **endpoint, **extra,
    )  # fmt: skip


def old_bound(spec: ModelSpec, limits: Limits) -> float:
    """The bound before C25: the whole token cap at the model's dearest rate."""
    return limits.token_budget * spec.price.dearest_per_token()


def test_c25_a_capped_reply_bounds_the_output_and_the_rest_of_the_cap_is_input_at_the_upper_tier():
    """C25: with every reply capped at 8,000 tokens over 30 rounds, at most 240,000 of the 10M-token cap are output,
    at the upper tier's $2.50; the rest is input at its dearest input rate, the $0.625 of a cache write. One more
    request past the cap adds a prompt within the 200,000-token context window and one capped reply."""
    spec = model()
    assert cap_bound_usd(spec, LIMITS) == pytest.approx(240_000 * 2.5e-6 + 9_760_000 * 0.625e-6)  # 6.70 USD
    assert overshoot_usd(spec, LIMITS) == pytest.approx(200_000 * 0.625e-6 + 8_000 * 2.5e-6)  # 0.145 USD
    assert bound_usd(spec, LIMITS) == pytest.approx(6.845)
    assert old_bound(spec, LIMITS) == pytest.approx(25.0)


def test_c25_a_run_that_ends_one_full_request_past_its_cap_is_within_its_reservation():
    """C25: the loop checks the cap before each request, so 30 rounds that leave the run 10,000 tokens short of its
    1M-token cap still send a 31st request, whose 200,000-token prompt and 8,000-token reply then count. The run costs
    more than the cap alone allows, and its reservation covers it."""
    spec, limits = model(price=Price(0.5, 2.5, 0.625)), dataclasses.replace(LIMITS, token_budget=1_000_000)
    rounds = [(25_000, 8_000, 25_000, 0)] * 30  # every prompt token a cache write, the dearest input
    last = (200_000, 8_000, 200_000, 0)
    assert sum(prompt + reply for prompt, reply, _, _ in rounds) == 990_000  # under the cap: the 31st request goes out
    cost = sum(spec.price.request_usd(*counts) for counts in [*rounds, last])
    assert cost == pytest.approx(1.21375)
    assert cap_bound_usd(spec, limits) == pytest.approx(1.075)
    assert cap_bound_usd(spec, limits) < cost  # the cap alone does not cover the run
    assert bound_usd(spec, limits) == pytest.approx(1.075 + 0.145)
    assert cost <= bound_usd(spec, limits)


def test_c25_the_reply_maximum_is_the_lower_of_the_studys_and_the_models_own():
    """C25 and B11: a model whose own maximum is below the study's sends that, so its output is bounded by it."""
    spec = model(max_output_tokens=4000)
    assert cap_bound_usd(spec, LIMITS) == pytest.approx(120_000 * 2.5e-6 + 9_880_000 * 0.625e-6)
    assert overshoot_usd(spec, LIMITS) == pytest.approx(200_000 * 0.625e-6 + 4_000 * 2.5e-6)


@pytest.mark.parametrize("backend", ["openai_compat", "anthropic"])
def test_c25_output_dearer_than_input_and_more_output_allowed_than_the_cap_is_the_whole_cap_at_the_output_rate(backend):
    """C25: when the replies could fill the whole cap, the cap is all output at worst; when input is dearer than
    output, it is all input."""
    rounds = dataclasses.replace(LIMITS, max_tool_rounds=5000)
    assert cap_bound_usd(model(backend), rounds) == pytest.approx(10_000_000 * 2.5e-6)
    dear_input = model(backend, price=Price(4.0, 1.0))
    assert cap_bound_usd(dear_input, LIMITS) == pytest.approx(10_000_000 * 4.0e-6)


@pytest.mark.parametrize("backend", ["stub", "plugin_backend"])
def test_c25_a_backend_whose_reply_maximum_the_core_does_not_send_keeps_the_whole_cap_at_the_dearest_rate(backend):
    """C25: the stub's synthetic counts ignore the output limit and a study's own backend may not send one, so the cap
    is bounded as before, at the dearest rate, with the request past it added."""
    spec = model(backend)
    assert cap_bound_usd(spec, LIMITS) == pytest.approx(old_bound(spec, LIMITS))
    assert bound_usd(spec, LIMITS) == pytest.approx(old_bound(spec, LIMITS) + overshoot_usd(spec, LIMITS))


def test_c25_a_free_model_costs_nothing_an_unpriced_one_is_unknown_and_one_without_a_window_cannot_be_bounded():
    """C25 and C12: free is 0, no price is None; a priced model with no context window has no bound on the request past
    the cap, which is refused naming the model and what to add."""
    assert bound_usd(model(price=None, free=True, context=None), LIMITS) == 0.0
    assert bound_usd(model(price=None, context=None), LIMITS) is None
    with pytest.raises(ValueError, match=r"model 'm1'.*context_tokens"):
        bound_usd(model(context=None), LIMITS)


def test_c25_the_budget_gate_refuses_a_priced_model_whose_request_past_the_cap_nothing_bounds(tmp_path):
    """C25: a gate built for a priced model with no context window would reserve too little, so it is not built."""
    store, _, _ = planned_store(tmp_path)
    with pytest.raises(ValueError, match="m1 have a price and no context_tokens"):
        BudgetGate(store, {"m1": model(context=None)}, Budget(max_usd=10.0), LIMITS)
    assert BudgetGate(store, {"m1": model()}, Budget(max_usd=10.0), LIMITS).assess([], NOW).reserve_usd == {
        "m1": pytest.approx(6.845)
    }


def random_price(rng: random.Random) -> Price | TieredPrice:
    """A flat or a tiered price with random rates, cache prices given or left out, input or output the dearer."""

    def flat() -> Price:
        cache = [rng.choice([None, rng.uniform(0, 8)]) for _ in range(2)]
        return Price(rng.uniform(0, 8), rng.uniform(0, 8), *cache)

    if rng.random() < 0.4:
        return flat()
    bounds = sorted(rng.sample(range(1, 300_000), rng.randint(1, 3)))
    return TieredPrice((*(PriceTier(b, flat()) for b in bounds), PriceTier(None, flat())))


def random_limits(rng: random.Random) -> Limits:
    return Limits(
        max_tool_rounds=rng.randint(0, 60),
        time_cap_s=60.0,
        token_budget=rng.randint(1, 3_000_000),
        output_cap_chars=100,
        max_output_tokens=rng.randint(1, 64_000),
    )


def reachable_cost(spec: ModelSpec, limits: Limits, rng: random.Random) -> float:
    """The cost of one run the loop allows: a request is sent while the tokens used are below the cap, at most one
    more than the tool rounds, each prompt within the context window and each reply within the maximum it carries.
    Counts are drawn toward their extremes, where the worst case lies; the last request may pass the cap."""
    reply_max, window = spec.output_tokens(limits.max_output_tokens), spec.context_tokens

    def draw(top: int) -> int:
        return rng.choice([0, top, top, rng.randint(0, top)])

    used, cost = 0, 0.0
    for _ in range(limits.max_tool_rounds + 1):
        if used >= limits.token_budget:
            break
        prompt, reply = draw(window), draw(reply_max)
        written = rng.randint(0, prompt)
        read = rng.randint(0, prompt - written)
        cost += spec.price.request_usd(prompt, reply, written, read)
        used += prompt + reply
    return cost


@pytest.mark.parametrize("backend", ["openai_compat", "anthropic", "stub"])
def test_c25_no_run_the_caps_allow_costs_more_than_its_bound_and_the_cap_part_is_never_above_the_old_bound(backend):
    """C25: over random prices, caps and runs, the request past the cap included (up to a whole context window and a
    reply past it), what a run costs is within its bound, and the part for the cap is never above the whole cap at the
    dearest rate, the bound before C25. The premise is the loop's: a request starts only below the cap, a run makes
    at most one request more than its tool rounds, and the endpoint applies the reply maximum it is sent."""
    rng = random.Random(20261110)
    for _ in range(400):
        limits = random_limits(rng)
        window = rng.randint(1_000, 400_000)
        spec = model(backend, price=random_price(rng), context=window, max_output_tokens=rng.choice([None, 500]))
        bound = bound_usd(spec, limits)
        assert cap_bound_usd(spec, limits) <= old_bound(spec, limits) * (1 + 1e-12)
        for _ in range(25):
            assert reachable_cost(spec, limits, rng) <= bound * (1 + 1e-9)
