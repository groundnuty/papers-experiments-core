"""An interrupted attempt costs its bound, never 0, and is left out of the mean cost of a run (C05)."""

import dataclasses

import pytest
from campaign_support import LATER, finished, planned_store, run_one

from expcore.campaign.attempt_bounds import attempt_costs, bound_usd, mean_run_usd, spent_usd
from expcore.campaign.recovery import recover
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price

# One attempt's bound is 30 USD: a 1.9M-token cap at the stub's dearest 15 USD per million, and the request past the
# cap, its 100,000-token window at 15 too, since nothing caps a stub's reply (C25).
BUDGET = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
SPEC = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000}


def interrupted(store, key):
    """Leave an attempt of a run running, then let recovery settle it: no record, no counts."""
    store.begin_attempt(key, LATER, 1, "")
    recover(store, LATER, 3)


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


def test_c05_an_interrupted_attempt_is_charged_its_bound_and_not_zero(tmp_path):
    """C05: an interrupted attempt with no record is charged the most its caps let one attempt cost."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    interrupted(store, key)
    [cost] = attempt_costs(store, {"m1": SPEC}, BUDGET)
    assert cost.state == "interrupted"
    assert cost.bounded
    assert cost.usd == pytest.approx(30.0)
    assert cost.usd == bound_usd(SPEC, BUDGET)
    assert cost.usd > 0


def test_c05_a_measured_attempt_is_charged_by_its_counts_and_a_running_one_is_not_yet_charged(tmp_path):
    """C05: a complete attempt costs what its counts say; an attempt still running has no cost yet."""
    store, _, manifest = planned_store(tmp_path)
    first, second = keys_of(manifest)[:2]
    run_one(store, first, finished(**TOKENS))
    store.begin_attempt(second, LATER, 1, "")  # running
    [cost] = attempt_costs(store, {"m1": SPEC}, BUDGET)
    assert (cost.usd, cost.bounded) == (pytest.approx(3.0 + 1.5), False)


def test_c05_the_mean_cost_of_a_run_leaves_the_interrupted_attempt_out(tmp_path):
    """C05: a bounded attempt is part of what was spent and no part of what a run costs."""
    store, _, manifest = planned_store(tmp_path)
    first, second, third = keys_of(manifest)[:3]
    run_one(store, first, finished(**TOKENS))  # 4.5
    interrupted(store, second)  # bound 30
    run_one(store, second, finished(input_tokens=500_000, output_tokens=100_000))  # 1.5 + 1.5, after the kill
    interrupted(store, third)  # bound 30, run not done
    costs = attempt_costs(store, {"m1": SPEC}, BUDGET)
    assert [c.bounded for c in costs] == [False, True, False, True]
    assert mean_run_usd(store, costs, "m1") == pytest.approx((4.5 + 3.0) / 2)
    assert spent_usd(costs) == pytest.approx(4.5 + 30 + 3.0 + 30)
    assert spent_usd(costs, "other") == 0.0


def test_c05_an_attempt_whose_usage_was_not_reported_is_bounded_not_counted_as_zero(tmp_path):
    """C05: a count the endpoint never reported is unknown, so the attempt costs its bound."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished())  # no counts at all
    [cost] = attempt_costs(store, {"m1": SPEC}, BUDGET)
    assert cost.bounded
    assert cost.usd == pytest.approx(30.0)
    assert mean_run_usd(store, [cost], "m1") is None  # no measured attempt: no sample


def test_c05_an_attempt_flagged_incomplete_is_bounded_whatever_it_recorded(tmp_path):
    """C05: totals known to be short (a request given up) are a lower bound, so the attempt is charged its bound."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(usage_complete=False, **TOKENS))
    [cost] = attempt_costs(store, {"m1": SPEC}, BUDGET)
    assert cost.bounded
    assert cost.usd == pytest.approx(30.0)


def test_c05_a_free_model_costs_nothing_and_an_unpriced_model_has_no_cost(tmp_path):
    """C05: free is a known zero, not a bound; with no price the cost is unknown, never 0, and the total is unknown."""
    store, _, manifest = planned_store(tmp_path)
    key = keys_of(manifest)[0]
    interrupted(store, key)
    free = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", free=True)
    unpriced = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen")
    [costed] = attempt_costs(store, {"m1": free}, BUDGET)
    assert (costed.usd, costed.bounded) == (0.0, False)
    [unknown] = attempt_costs(store, {"m1": unpriced}, BUDGET)
    assert unknown.usd is None
    assert spent_usd([unknown]) is None
    [absent] = attempt_costs(store, {}, BUDGET)  # a model the table does not hold
    assert absent.usd is None


def test_c05_the_budget_of_an_attempt_is_a_positive_whole_number():
    """C05: a bound from a missing or silly budget, reply maximum or round count is refused rather than computed."""
    for name, bad in (("token_budget", 0), ("token_budget", -5), ("token_budget", 1.5), ("token_budget", True),
                      ("max_output_tokens", 0), ("max_tool_rounds", -1)):  # fmt: skip
        with pytest.raises(ValueError, match=f"the {name} of an attempt is an integer of at least"):
            bound_usd(SPEC, dataclasses.replace(BUDGET, **{name: bad}))
