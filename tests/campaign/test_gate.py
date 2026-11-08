"""The budget gate: budgets are enforced, unknown cost holds like over-budget, and a release counts once (C12, C13)."""

import json
import os

import pytest
from campaign_support import LATER, NOW, finished, planned_store, run_one

from expcore.campaign.gate import ALL, HOLDS_KEY, RELEASED_KEY, Budget, BudgetGate
from expcore.campaign.recovery import recover
from expcore.campaign.release import epoch_of
from expcore.config.models import ModelSpec
from expcore.config.prices import Price

TOKEN_BUDGET = 2_000_000  # the bound of one attempt of the priced model is 2M tokens at 15 USD per million: 30 USD
PRICED = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0))
UNPRICED = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen")
FREE = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", free=True)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000}  # 4.5 USD


def gate_for(store, spec=PRICED, **budget):
    return BudgetGate(store, {"m1": spec}, Budget(**{"max_usd": 100.0, **budget}), TOKEN_BUDGET)


def keys_of(manifest):
    return [row.key.text for row in manifest.rows]


def test_c12_a_model_without_a_price_is_held_at_the_budget_gate_and_a_free_one_is_not(tmp_path):
    """C12: a model whose price is unknown is held as if over budget; one marked free is not held."""
    store, _, _ = planned_store(tmp_path)
    held = gate_for(store, UNPRICED).assess([], NOW)
    assert "no price" in held.reason("m1")
    assert gate_for(store, FREE).assess([], NOW).reason("m1") is None
    assert gate_for(store, PRICED).assess([], NOW).reason("m1") is None


def test_c12_a_run_that_does_not_fit_the_budget_is_held_even_before_any_run_has_finished(tmp_path):
    """C12: with no finished run to average, a run is expected to cost its bound, and is held when even that is over."""
    store, _, _ = planned_store(tmp_path)
    state = gate_for(store, max_usd=20.0).assess([], NOW)
    assert state.reserve_usd["m1"] == pytest.approx(30.0)
    assert "over the ceiling of 20.00 USD" in state.reason("m1")
    assert gate_for(store, max_usd=30.0).assess([], NOW).reason("m1") is None  # exactly the bound fits


def test_c12_the_runs_in_flight_count_before_their_cost_is_recorded(tmp_path):
    """C12: the dispatcher names the runs in flight, because their attempts cost nothing in the store until they end."""
    store, _, _ = planned_store(tmp_path)
    gate = gate_for(store, max_usd=100.0)
    assert gate.assess(["m1", "m1"], NOW).reason("m1") is None  # 30 + 30 + 30
    assert "in flight 90.00" in gate.assess(["m1", "m1", "m1"], NOW).reason("m1")


def test_c12_a_finished_runs_mean_replaces_the_bound_as_the_expected_cost(tmp_path):
    """C12: once a run has finished, the next is expected to cost what finished runs cost."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    gate = gate_for(store, max_usd=20.0)
    state = gate.assess([], NOW)
    assert state.reserve_usd["m1"] == pytest.approx(4.5)
    assert state.spent_usd == pytest.approx(4.5)
    assert state.reason("m1") is None
    assert gate.assess(["m1"] * 3, NOW).reason("m1") is not None  # 4.5 + 13.5 + 4.5 is over 20


def test_c12_an_interrupted_attempt_counts_its_bound_toward_the_spend(tmp_path):
    """C12 and C05: the bound of an interrupted attempt is spent, so it can hold the next run."""
    store, _, manifest = planned_store(tmp_path)
    store.begin_attempt(keys_of(manifest)[0], LATER, 1, "")
    recover(store, LATER, 3)
    state = gate_for(store, max_usd=50.0).assess([], NOW)
    assert state.spent_usd == pytest.approx(30.0)
    assert "over the ceiling" in state.reason("m1")  # 30 spent + 30 expected


def test_c12_once_the_spend_is_unknown_every_model_is_held(tmp_path):
    """C12: an attempt of a model the table cannot price leaves the total unknown, which holds like over budget."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    other = ModelSpec(key="m2", backend="stub", api_id="m2", status="frozen", price=Price(1.0, 1.0))
    state = BudgetGate(store, {"m2": other}, Budget(max_usd=100.0), TOKEN_BUDGET).assess([], NOW)
    assert "total spent is unknown" in state.reason("m2")


def test_c12_a_ceiling_for_one_model_holds_that_model_only(tmp_path):
    """C12: a model's own ceiling is checked beside the campaign's."""
    store, _, _ = planned_store(tmp_path)
    state = gate_for(store, max_usd=1000.0, per_model_usd={"m1": 10.0}).assess([], NOW)
    assert "model m1" in state.reason("m1")


def test_c13_a_stale_or_reused_release_does_not_resume_dispatch(tmp_path):
    """C13: a release written before the stop is ignored and stays; one written after lifts it once and is used up."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    path.write_text(f"{ALL}\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) - 1000, epoch_of(NOW) - 1000))  # written long before the stop
    gate = gate_for(store, max_usd=20.0, release_step_usd=15.0, release_file=path)
    assert gate.assess([], NOW).reason("m1") is not None  # the stop begins here
    assert gate.assess([], LATER).reason("m1") is not None  # the stale file did not lift it
    assert path.exists()
    os.utime(path, (epoch_of(LATER) + 5, epoch_of(LATER) + 5))  # now written after the stop
    state = gate.assess([], LATER)
    assert state.reason("m1") is None  # the ceiling is now 20 + 15 = 35, and a run is expected to cost 30
    assert not path.exists()


def test_c13_a_release_raises_the_ceiling_by_its_step_and_a_later_stop_needs_a_new_release(tmp_path):
    """C13: a release lets the campaign go on by a stated amount and no more; the file is used up."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate = gate_for(store, max_usd=20.0, release_step_usd=15.0, release_file=path)
    assert gate.assess([], NOW).reason("m1") is not None
    path.write_text(f"{ALL}\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) + 5, epoch_of(NOW) + 5))
    assert gate.assess([], LATER).reason("m1") is None  # the ceiling is 35 and the run expects 30
    assert not path.exists()
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}
    assert json.loads(store.get_meta(HOLDS_KEY)) == {}
    assert gate.assess(["m1"], LATER).reason("m1") is not None  # one in flight more is over 35 again: a new stop
    kinds = [e["kind"] for e in store.q("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["budget_hold", "budget_released", "budget_hold"]


def test_c13_a_release_names_what_it_ends_and_leaves_other_stops(tmp_path):
    """C13: a model's name ends that model's ceiling; the total's stop is not ended by it."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate = gate_for(store, max_usd=20.0, per_model_usd={"m1": 20.0}, release_step_usd=15.0, release_file=path)
    assert "model m1" in gate.assess([], NOW).reason("m1")
    path.write_text("m1\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) + 5, epoch_of(NOW) + 5))
    state = gate.assess([], LATER)
    assert "the campaign" in state.reason("m1")  # the total is still over its 20
    assert "model m1" not in state.reason("m1")
    assert set(json.loads(store.get_meta(HOLDS_KEY))) == {ALL}


def test_c13_the_stops_and_the_releases_survive_a_restart(tmp_path):
    """C13: they are kept in the store, so a new gate over the same store sees what the old one recorded."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate_for(store, max_usd=20.0, release_step_usd=15.0, release_file=path).assess([], NOW)
    path.write_text(f"{ALL}\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) + 5, epoch_of(NOW) + 5))
    restarted = gate_for(store, max_usd=20.0, release_step_usd=15.0, release_file=path)
    assert restarted.assess([], LATER).reason("m1") is None
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_usd": 0}, "holds every run"),
        ({"max_usd": -1}, "not below zero"),
        ({"max_usd": float("inf")}, "finite"),
        ({"per_model_usd": {"ALL": 1.0}}, "cannot be called that"),
        ({"release_step_usd": 5.0}, "needs a release_file"),
        ({"release_file": "r"}, "needs a release_step_usd"),
    ],
)
def test_c12_a_budget_that_cannot_work_is_refused(changes, message):
    """C12: a ceiling that is zero, negative or infinite, or a release that adds nothing, is refused at once."""
    with pytest.raises(ValueError, match=message):
        Budget(**{"max_usd": 10.0, **changes})
