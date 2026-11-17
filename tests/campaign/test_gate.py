"""The budget gate: budgets are enforced, unknown cost holds like over-budget, and a release counts once (C12, C13)."""

import json
import logging
import os
import re

import pytest
from campaign_support import LATER, NOW, finished, planned_store, run_one

from expcore.campaign.gate import ALL, HOLDS_KEY, RELEASED_KEY, Budget, BudgetGate
from expcore.campaign.recovery import recover
from expcore.campaign.release import epoch_of
from expcore.campaign.snapshot import take_snapshot
from expcore.campaign.store import Store
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import Price

# The bound of one attempt of the priced stub is 30 USD: its 1.9M-token cap at 15 USD per million, and the request past
# the cap, its 100,000-token window at 15 too, since nothing caps a stub's reply (C25).
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=60.0, token_budget=1_900_000, output_cap_chars=100, max_output_tokens=20_000
)
PRICED = ModelSpec(
    key="m1", backend="stub", api_id="m1", status="frozen", price=Price(3.0, 15.0), context_tokens=100_000
)
UNPRICED = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen")
FREE = ModelSpec(key="m1", backend="stub", api_id="m1", status="frozen", free=True)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000}  # 4.5 USD


def gate_for(store, spec=PRICED, **budget):
    return BudgetGate(store, {"m1": spec}, Budget(**{"max_usd": 100.0, **budget}), LIMITS)


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


def test_c12_a_reservation_is_the_bound_never_the_mean_of_finished_runs(tmp_path):
    """C12: a $30 ceiling, a $30 bound and a first run at $4.50: the second is held, the mean is only an estimate."""
    store, _, manifest = planned_store(tmp_path)
    assert gate_for(store, max_usd=30.0).assess([], NOW).reason("m1") is None  # the first fits its bound exactly
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    state = gate_for(store, max_usd=30.0).assess([], NOW)
    assert state.spent_usd == pytest.approx(4.5)
    assert state.reserve_usd["m1"] == pytest.approx(30.0)  # what a pending or running run is reserved
    assert state.estimate_usd["m1"] == pytest.approx(4.5)  # for reports and estimates only
    assert "4.50 + in flight 0.00 + this run 30.00" in state.reason("m1")


def test_c12_a_second_run_is_admitted_only_when_the_remaining_budget_covers_the_bound(tmp_path):
    """C12: spent 4.5 and a bound of 30: 34.5 admits the next run and anything under it holds, in flight or not."""
    store, _, manifest = planned_store(tmp_path)
    run_one(store, keys_of(manifest)[0], finished(**TOKENS))
    assert gate_for(store, max_usd=34.5).assess([], NOW).reason("m1") is None
    assert gate_for(store, max_usd=34.4).assess([], NOW).reason("m1") is not None
    assert gate_for(store, max_usd=64.5).assess(["m1"], NOW).reason("m1") is None  # a run in flight is also reserved 30
    assert gate_for(store, max_usd=64.4).assess(["m1"], NOW).reason("m1") is not None


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
    other = ModelSpec(key="m2", backend="stub", api_id="m2", status="frozen", price=Price(1.0, 1.0), context_tokens=8)
    state = BudgetGate(store, {"m2": other}, Budget(max_usd=100.0), LIMITS).assess([], NOW)
    assert "total spent is unknown" in state.reason("m2")


def test_c12_a_ceiling_for_one_model_holds_that_model_only(tmp_path):
    """C12: a model's own ceiling is checked beside the campaign's."""
    store, _, _ = planned_store(tmp_path)
    state = gate_for(store, max_usd=1000.0, per_model_usd={"m1": 10.0}).assess([], NOW)
    assert "model m1" in state.reason("m1")


def serial_of_hold(store, scope=ALL):
    return json.loads(store.get_meta(HOLDS_KEY))[scope]["serial"]


def released_gate(store, path, **budget):
    return gate_for(store, **{"max_usd": 20.0, "release_step_usd": 15.0, "release_file": path, **budget})


def test_c13_a_stale_or_reused_release_does_not_resume_dispatch(tmp_path):
    """C13: a bare scope written long before the stop is refused and stays; the line the hold names lifts it once and
    is used up, so the same file lifts nothing again."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    path.write_text(f"{ALL}\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) - 1000, epoch_of(NOW) - 1000))  # written long before the stop
    gate = released_gate(store, path)
    reason = gate.assess([], NOW).reason("m1")  # the stop begins here
    serial = serial_of_hold(store)
    assert reason.endswith(f"(stop {serial}; to end it, write the line 'ALL {serial}' in the budget's release file)")
    assert gate.assess([], LATER).reason("m1") is not None  # the stale file did not lift it
    assert path.read_text(encoding="utf-8") == "ALL\n"
    line = re.search(r"write the line '([^']+)'", reason).group(1)
    path.write_text(f"{line}\n", encoding="utf-8")
    os.utime(path, (epoch_of(NOW) - 1000, epoch_of(NOW) - 1000))  # its time, behind the stop's, changes nothing
    assert gate.assess([], LATER).reason("m1") is None  # the ceiling is now 20 + 15 = 35, and a run expects 30
    assert not path.exists()
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}


def test_c13_a_release_raises_the_ceiling_by_its_step_and_a_later_stop_needs_a_new_release(tmp_path):
    """C13: a release lets the campaign go on by a stated amount and no more; a later stop has a serial of its own,
    which the earlier stop's line does not name."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate = released_gate(store, path)
    assert gate.assess([], NOW).reason("m1") is not None
    first = serial_of_hold(store)
    path.write_text(f"ALL {first}\n", encoding="utf-8")
    assert gate.assess([], LATER).reason("m1") is None  # the ceiling is 35 and the run expects 30
    assert not path.exists()
    assert json.loads(store.get_meta(HOLDS_KEY)) == {}
    assert gate.assess(["m1"], LATER).reason("m1") is not None  # one in flight more is over 35 again: a new stop
    second = serial_of_hold(store)
    assert int(second.split(".")[0]) == int(first.split(".")[0]) + 1
    path.write_text(f"ALL {first}\n", encoding="utf-8")
    assert gate.assess(["m1"], LATER).reason("m1") is not None
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}
    kinds = [e["kind"] for e in store.q("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["budget_hold", "budget_released", "budget_hold", "budget_release_refused"]
    details = [e["detail"] for e in store.q("SELECT detail FROM events WHERE kind='budget_hold' ORDER BY id")]
    assert [f"(stop {first})" in details[0], f"(stop {second})" in details[1]] == [True, True]


@pytest.mark.parametrize("offset", [-3600.0, -0.0015, 0.0, 1.0], ids=["behind", "a-tick-behind", "same-tick", "ahead"])
def test_c13_the_budget_holds_own_line_lifts_it_whatever_time_the_file_carries(tmp_path, offset):
    """C13: a coarse clock, a server behind or ahead, or the stop's own tick: the file's time changes nothing."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate = released_gate(store, path)
    assert gate.assess([], NOW, exact="2026-01-01T00:00:00.800000Z").reason("m1") is not None
    since = json.loads(store.get_meta(HOLDS_KEY))[ALL]["since_epoch"]
    assert since == pytest.approx(epoch_of(NOW) + 0.8)  # kept at full precision, for display
    path.write_text(f"ALL {serial_of_hold(store)}\n", encoding="utf-8")
    os.utime(path, (since + offset, since + offset))
    assert gate.assess([], LATER).reason("m1") is None
    assert not path.exists()


@pytest.mark.parametrize("ahead", [0.0, 1.0, 3600.0])
def test_c13_a_file_written_before_the_budget_stop_lifts_nothing_even_stamped_after_it(tmp_path, ahead):
    """C13: a bare scope written before the stop by a writer whose clock is ahead raises no ceiling."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    path.write_text(f"{ALL}\n", encoding="utf-8")
    gate = released_gate(store, path)
    gate.assess([], NOW, exact="2026-01-01T00:00:00.800000Z")
    stamp = json.loads(store.get_meta(HOLDS_KEY))[ALL]["since_epoch"] + ahead
    os.utime(path, (stamp, stamp))
    assert gate.assess([], LATER).reason("m1") is not None
    assert path.exists()
    assert store.get_meta(RELEASED_KEY) is None


def test_c13_a_hold_recorded_before_serials_is_given_one_and_a_bare_scope_is_refused_naming_it(tmp_path, caplog):
    """C13: a store from before serials: the hold in force is given a serial at the next assessment, kept across a
    restart; the bare scope, the old form, is refused with the line to write, which then lifts it once."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(HOLDS_KEY, json.dumps({ALL: {"since_utc": NOW, "reason": "over"}}), NOW)
    path = tmp_path / "release"
    path.write_text(f"{ALL}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="expcore.campaign.release"):
        reason = released_gate(store, path).assess([], LATER).reason("m1")
    serial = serial_of_hold(store)
    assert f"write the line 'ALL {serial}'" in reason
    [numbered] = store.q("SELECT detail FROM events WHERE kind='budget_hold_numbered'")
    assert numbered["detail"] == f"ALL: the hold, recorded without a serial, is stop {serial}"
    [warned] = [r.getMessage() for r in caplog.records]
    assert "'<name> <serial>'" in warned
    assert f"the line 'ALL {serial}' ends it" in warned
    restarted = released_gate(store, path)
    assert restarted.assess([], LATER).reason("m1") is not None
    assert serial_of_hold(store) == serial  # numbered once, never again
    path.write_text(f"ALL {serial}\n", encoding="utf-8")
    assert restarted.assess([], LATER).reason("m1") is None
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}


def test_c13_a_budget_hold_lost_in_a_restore_gives_its_line_to_no_later_hold(tmp_path):
    """C13: a store copied back from a snapshot taken before a hold gives the next hold the same count, and another
    random part: the lost hold's exact line, still in the file, raises no ceiling; the new hold's own line does."""
    store, schema, _ = planned_store(tmp_path)
    live = tmp_path / "campaign.db"
    take_snapshot(live, tmp_path / "snapshot.db", in_place=True)
    path = tmp_path / "release"
    released_gate(store, path).assess([], NOW)
    lost = serial_of_hold(store)
    path.write_text(f"ALL {lost}\n", encoding="utf-8")  # written for the hold the restore will lose
    store.close()
    for leftover in tmp_path.glob("campaign.db*"):
        leftover.unlink()
    (tmp_path / "snapshot.db").rename(live)
    store = Store(live, schema)
    gate = released_gate(store, path)
    assert gate.assess([], LATER).reason("m1") is not None
    new = serial_of_hold(store)
    assert new.split(".")[0] == lost.split(".")[0]
    assert new != lost
    assert gate.assess([], LATER).reason("m1") is not None  # the lost hold's line ends nothing
    assert store.get_meta(RELEASED_KEY) is None
    [refused] = store.q("SELECT detail FROM events WHERE kind='budget_release_refused'")
    assert f"the release line 'ALL {lost}' ends nothing" in refused["detail"]
    with path.open("a", encoding="utf-8") as file:
        file.write(f"ALL {new}\n")
    assert gate.assess([], LATER).reason("m1") is None
    assert path.read_text(encoding="utf-8").splitlines() == [f"ALL {lost}"]


def test_c13_a_release_names_what_it_ends_and_leaves_other_stops(tmp_path):
    """C13: a model's line ends that model's ceiling; the total's stop is not ended by it."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    gate = released_gate(store, path, per_model_usd={"m1": 20.0})
    assert "model m1" in gate.assess([], NOW).reason("m1")
    path.write_text(f"m1 {serial_of_hold(store, 'm1')}\n", encoding="utf-8")
    state = gate.assess([], LATER)
    assert "the campaign" in state.reason("m1")  # the total is still over its 20
    assert "model m1" not in state.reason("m1")
    assert set(json.loads(store.get_meta(HOLDS_KEY))) == {ALL}


def test_c13_the_stops_and_the_releases_survive_a_restart(tmp_path):
    """C13: they are kept in the store, so a new gate over the same store sees what the old one recorded."""
    store, _, _ = planned_store(tmp_path)
    path = tmp_path / "release"
    released_gate(store, path).assess([], NOW)
    path.write_text(f"ALL {serial_of_hold(store)}\n", encoding="utf-8")
    restarted = released_gate(store, path)
    assert restarted.assess([], LATER).reason("m1") is None
    assert json.loads(store.get_meta(RELEASED_KEY)) == {ALL: 15.0}


def test_c13_a_budget_hold_without_a_release_file_names_its_stop_and_a_top_up_refusal_names_none(tmp_path):
    """C13: without a release file the hold names its serial and no line; a top-up's refusal, which a run record
    keeps and replays, carries no serial."""
    store, _, _ = planned_store(tmp_path)
    gate = gate_for(store, max_usd=20.0)
    reason = gate.assess([], NOW).reason("m1")
    assert reason.endswith(f"(stop {serial_of_hold(store)})")
    assert "stop" not in gate.refusal("m1", [], 25.0)


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
