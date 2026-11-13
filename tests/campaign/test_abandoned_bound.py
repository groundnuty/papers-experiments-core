"""B19: what an abandoned request can cost, from the entry the record keeps and the model's price.

The bound is the prompt bound of the entry (the last reported prompt and completion and the bytes appended since, or
the window) and the reply: the `max_tokens` it carried when the attempt was held to it (C27), the window alone
otherwise. Every case is held against the loose bound this replaces, the window for the prompt and the reply maximum.
"""

import pytest

from expcore.campaign.abandoned_bound import AbandonedBound, abandoned_request_bound, held_to_reply_maximum
from expcore.campaign.attempt_bounds import overshoot_usd
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price, PriceTier, TieredPrice

WINDOW = 1_000_000
REPLY = 8_000
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=REPLY
)
FLAT = Price(3.0, 15.0)  # output dearer
INPUT_DEARER = Price(15.0, 3.0)
CACHED = Price(3.0, 15.0, cache_write_per_mtok=20.0, cache_read_per_mtok=0.3)


def spec(price=FLAT, **overrides):
    return ModelSpec(
        key="m1",
        backend="openai_compat",
        base_url="https://llm.example.invalid/v1",
        api_id="m1",
        status="frozen",
        price=price,
        context_tokens=overrides.pop("context_tokens", WINDOW),
        **overrides,
    )


def entry(prompt=100_000, by="bytes", reply=REPLY, **more):
    return {
        "reason": "read_timeout",
        "usage": "unknown",
        "prompt_bound_tokens": prompt,
        "prompt_bound_by": by,
        "reply_max_tokens": reply,
        "window_tokens": WINDOW,
        "bound_checked": 0,
        "bound_exceeded": 0,
        "bound_unreported": 0,
        **more,
    }


def test_b19_a_request_held_to_its_reply_maximum_costs_the_prompt_bound_and_the_reply_maximum():
    """B19: the issue's case: a 1,000,000-token window and a run that had sent 100,000 tokens. The loose bound adds
    1,000,000 x 3 + 8,000 x 15 = 3.12 dollars; the tight one 100,000 x 3 + 8,000 x 15 = 0.42."""
    bound = abandoned_request_bound(spec(), LIMITS, entry(), reply_capped=True)
    assert bound == AbandonedBound(108_000, pytest.approx(0.42), "bytes", "reply_maximum")
    assert overshoot_usd(spec(), LIMITS, reply_capped=True) == pytest.approx(3.12)


def test_b19_the_reply_maximum_is_the_one_the_request_carried_and_not_the_one_the_limits_say_now():
    """B19: the entry holds the max_tokens the request actually carried, which a model's own lower maximum can set."""
    bound = abandoned_request_bound(spec(), LIMITS, entry(reply=2_000), reply_capped=True)
    assert (bound.request_bound_tokens, bound.request_bound_usd) == (102_000, pytest.approx(0.33))


def test_b19_an_entry_with_no_reply_maximum_falls_back_to_the_models_own():
    """B19: an entry from a backend that recorded none is read with the reply maximum the limits and the model give."""
    entry_without = entry()
    del entry_without["reply_max_tokens"]
    bound = abandoned_request_bound(spec(max_output_tokens=4_000), LIMITS, entry_without, reply_capped=True)
    assert bound.request_bound_tokens == 104_000


def test_b19_a_model_not_held_to_its_reply_maximum_has_the_prompt_and_reply_together_within_the_window():
    """B19: nothing is shown to hold the reply, so only the window holds the request: its tokens are the window, and
    with the output dearer the whole window can be reply, 1,000,000 x 15, as the loose bound says."""
    bound = abandoned_request_bound(spec(), LIMITS, entry(), reply_capped=False)
    assert (bound.request_bound_tokens, bound.reply_by) == (WINDOW, "window")
    assert bound.request_bound_usd == pytest.approx(15.0)


def test_b19_with_the_input_dearer_the_byte_bound_tightens_a_model_not_held_to_its_reply_maximum():
    """B19: a prompt of at most 100,000 tokens at 15 and the rest of the window as reply at 3: 1.5 + 2.7 = 4.2 dollars,
    below the 15 the loose bound charges the whole window at the dearer rate."""
    bound = abandoned_request_bound(spec(INPUT_DEARER), LIMITS, entry(), reply_capped=False)
    assert bound.request_bound_usd == pytest.approx(100_000 * 15 / 1e6 + 900_000 * 3 / 1e6)
    loose = abandoned_request_bound(spec(INPUT_DEARER), LIMITS, entry(prompt=WINDOW, by="window"), reply_capped=False)
    assert loose.request_bound_usd == pytest.approx(15.0)
    assert bound.request_bound_usd < loose.request_bound_usd


@pytest.mark.parametrize("held", [True, False], ids=["held", "not-held"])
@pytest.mark.parametrize("by", ["window", "none"])
def test_b19_an_entry_bounded_by_the_window_is_the_loose_bound_exactly(held, by):
    """B19: where the byte assumption does not hold, the answer is the window's and says so; for a flat price and a
    model held to its maximum it is what `overshoot_usd` has always charged."""
    bound = abandoned_request_bound(
        spec(), LIMITS, entry(prompt=WINDOW if by == "window" else None, by=by), reply_capped=held
    )
    assert bound.prompt_by == "window"
    assert bound.request_bound_usd == pytest.approx(overshoot_usd(spec(), LIMITS, reply_capped=held))


@pytest.mark.parametrize("old", [{"reason": "read_timeout", "usage": "unknown"}, {}])
def test_b19_an_entry_from_before_the_bound_was_recorded_is_bounded_by_the_window(old):
    """B19: a record written earlier, or by a backend of a study's own, has no bound in its entry: the window's."""
    bound = abandoned_request_bound(spec(), LIMITS, old, reply_capped=True)
    assert bound.prompt_by == "window"
    assert bound.request_bound_usd == pytest.approx(overshoot_usd(spec(), LIMITS, reply_capped=True))


def test_b19_a_byte_bound_above_the_windows_is_clamped_to_it_and_never_exceeds_the_loose_bound():
    """B19: an entry that names a bound above this model's window (a table that changed) cannot cost more than the
    window's own bound."""
    bound = abandoned_request_bound(spec(context_tokens=50_000), LIMITS, entry(prompt=90_000), reply_capped=True)
    assert bound.request_bound_tokens == 50_000 + REPLY


def test_b19_the_dearest_input_rate_is_a_cache_price_when_that_is_higher():
    """B19: a cached prompt is bounded at the dearest of the input and the cache prices: here the cache write's 20."""
    bound = abandoned_request_bound(spec(CACHED), LIMITS, entry(), reply_capped=True)
    assert bound.request_bound_usd == pytest.approx(100_000 * 20 / 1e6 + 8_000 * 15 / 1e6)


TIERED = TieredPrice((PriceTier(200_000, Price(1.0, 4.0)), PriceTier(None, Price(2.0, 8.0))))


@pytest.mark.parametrize(
    ("prompt", "usd"),
    [
        (200_000, 200_000 * 1.0 / 1e6 + 8_000 * 4.0 / 1e6),  # a prompt of exactly the boundary is still the lower tier
        (200_001, 200_001 * 2.0 / 1e6 + 8_000 * 8.0 / 1e6),  # one token more can fall in the upper tier
        (50_000, 50_000 * 1.0 / 1e6 + 8_000 * 4.0 / 1e6),
        (0, 8_000 * 4.0 / 1e6),
    ],
)
def test_b19_a_tiered_price_takes_the_dearest_tier_the_prompt_bound_can_reach(prompt, usd):
    """B19: a prompt of at most 200,000 tokens falls in the lower tier and is priced at its rates, reply included; a
    prompt bound past the boundary can fall in the upper tier, and the request is priced at that tier's."""
    bound = abandoned_request_bound(spec(TIERED), LIMITS, entry(prompt=prompt), reply_capped=True)
    assert bound.request_bound_usd == pytest.approx(usd)


def test_b19_a_tiered_price_with_the_window_bound_reaches_every_tier_and_is_the_loose_bound():
    """B19: a bound by the window reaches the upper tier, as the loose bound's dearest rates do."""
    bound = abandoned_request_bound(spec(TIERED), LIMITS, entry(prompt=WINDOW, by="window"), reply_capped=True)
    assert bound.request_bound_usd == pytest.approx(overshoot_usd(spec(TIERED), LIMITS, reply_capped=True))


def test_b19_a_tiered_price_not_held_prices_the_window_at_each_reachable_tier_and_takes_the_dearest():
    """B19: with the input dearer than the output in the lower tier, the lower tier's request (prompt up to the bound,
    the rest of the window reply) can cost more than the upper's: the maximum over tiers is taken, not the last."""
    price = TieredPrice((PriceTier(200_000, Price(20.0, 1.0)), PriceTier(None, Price(2.0, 8.0))))
    bound = abandoned_request_bound(spec(price), LIMITS, entry(prompt=300_000), reply_capped=False)
    lower = 200_000 * 20.0 / 1e6 + 800_000 * 1.0 / 1e6
    upper = 1_000_000 * 8.0 / 1e6  # output dearer in the upper tier: the whole window as reply
    assert bound.request_bound_usd == pytest.approx(max(lower, upper))


def test_b19_a_free_model_costs_nothing_and_an_unpriced_one_has_no_dollar_bound_though_it_has_a_token_bound():
    """B19: a free model's bound is 0, and a model whose price is unknown has `None` dollars, never 0, since the
    budget gate holds it (C12); the tokens are known either way."""
    free = abandoned_request_bound(spec(None, free=True), LIMITS, entry(), reply_capped=True)
    unknown = abandoned_request_bound(spec(None), LIMITS, entry(), reply_capped=True)
    assert (free.request_bound_usd, free.request_bound_tokens) == (0.0, 108_000)
    assert (unknown.request_bound_usd, unknown.request_bound_tokens) == (None, 108_000)


def test_b19_a_window_bounded_entry_of_a_model_without_a_window_is_refused_and_a_byte_bound_held_is_not():
    """B19: like the bound past the token cap, the window's bound needs the window; a byte bound with the reply held
    to its maximum needs none, since the prompt and the reply are each bounded."""
    no_window = spec(context_tokens=None)
    with pytest.raises(ValueError, match="context_tokens"):
        abandoned_request_bound(no_window, LIMITS, entry(prompt=None, by="none"), reply_capped=True)
    with pytest.raises(ValueError, match="context_tokens"):
        abandoned_request_bound(no_window, LIMITS, entry(), reply_capped=False)
    assert abandoned_request_bound(no_window, LIMITS, entry(), reply_capped=True).request_bound_tokens == 108_000


@pytest.mark.parametrize(
    "bad",
    [
        entry(by="guess"),
        entry(prompt=-1),
        entry(prompt=True),
        entry(prompt="100"),
        entry(prompt=None, by="bytes"),
        entry(reply=-5),
        entry(reply="many"),
    ],
    ids=["basis", "negative", "bool", "text", "bytes-without-a-count", "negative-reply", "text-reply"],
)
def test_b19_an_entry_that_is_not_what_the_adapters_write_is_refused_naming_the_field(bad):
    """B19: the entry is read from a record, so it is checked at the boundary and not trusted."""
    with pytest.raises(ValueError, match="abandoned_request"):
        abandoned_request_bound(spec(), LIMITS, bad, reply_capped=True)


def test_b19_a_non_mapping_entry_is_refused():
    """B19: a record whose request was not abandoned has no entry (`None`); asking for its bound is a mistake."""
    with pytest.raises(ValueError, match="abandoned_request"):
        abandoned_request_bound(spec(), LIMITS, None, reply_capped=True)  # type: ignore[arg-type]


def test_b19_the_tight_bound_is_never_above_the_loose_one_for_any_held_flat_entry():
    """B19: soundness against the old bound, as a sweep: for every prompt bound up to the window and either way of
    holding the reply, the tight bound is at most the window's."""
    for held in (True, False):
        for price in (FLAT, INPUT_DEARER, CACHED, TIERED):
            loose = abandoned_request_bound(spec(price), LIMITS, entry(prompt=WINDOW, by="window"), reply_capped=held)
            for prompt in (0, 1, 1_000, 199_999, 200_000, 200_001, 500_000, WINDOW - 1, WINDOW):
                tight = abandoned_request_bound(spec(price), LIMITS, entry(prompt=prompt), reply_capped=held)
                assert tight.request_bound_usd <= loose.request_bound_usd + 1e-12, (held, price, prompt)


@pytest.mark.parametrize(
    ("totals", "held"),
    [
        ({"reserved_capped": True, "reply_cap_unheld": False}, True),
        ({"reserved_capped": True}, True),
        ({"reserved_capped": True, "reply_cap_unheld": True}, False),
        ({"reserved_capped": False, "reply_cap_unheld": False}, False),
        ({"reserved_capped": False}, False),
        ({}, False),
        ({"reply_cap_unheld": False}, False),
    ],
)
def test_b19_whether_an_attempt_was_held_to_its_reply_maximum_is_read_from_its_totals_and_never_assumed(totals, held):
    """B19 and C27: the attempt was held only when the gate reserved it by the reply maximum and the campaign had not
    taken its model off it; a record that says neither is not held, which can only charge more."""
    assert held_to_reply_maximum(totals) is held
