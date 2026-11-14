"""Replay serves the waits on infrastructure of the record, by kind, and the cuts and cleanups of the loop's watch."""

import asyncio
import copy

import pytest
from replay_scenarios import SCENARIOS

from expcore.records.record import RunRecord, decode
from expcore.replay.clock import ReplayClock
from expcore.replay.divergence import Halt, ReplayDivergence
from expcore.replay.replay import replay_record


def record_of(name: str) -> dict:
    return copy.deepcopy(SCENARIOS[name][0]().to_json())


def diverges(data: dict, path: str) -> ReplayDivergence:
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == path
    return caught.value


def test_l04_the_record_holds_the_seconds_waited_by_kind_per_call_and_for_the_run():
    """L04: three kinds in one call, two of them nested; the figures are in the call, the turn and the totals."""
    record = SCENARIOS["waits_by_kind"][0]()
    per_call = [c["infra_wait_by_kind"] for c in record.tool_calls]
    assert per_call == [{"lease_queue": 35, "sandbox_lifecycle": 20, "toolserver_lifecycle": 4}] * 2
    assert record.totals["infra_wait_by_kind"] == {
        "lease_queue": 70,
        "sandbox_lifecycle": 40,
        "toolserver_lifecycle": 8,
    }
    assert record.totals["infra_wait_s"] == 2 * 39  # the union: the nested sandbox wait adds nothing to it
    assert record.totals["agent_s"] == 2 * 2
    labels = [label for label, _ in record.clock]
    assert {"wait_start:lease_queue", "wait_start:sandbox_lifecycle", "wait_end:toolserver_lifecycle"} <= set(labels)


def test_l04_a_model_request_that_waited_has_its_kind_on_its_turn():
    """L04: the backoff of a retry is a `model_retry` wait, kept on the turn whose request made it."""
    record = SCENARIOS["model_wait"][0]()
    assert [t["infra_wait_by_kind"] for t in record.transcript] == [{}, {"model_retry": 7}]
    assert record.totals["infra_wait_by_kind"] == {"model_retry": 7}


def test_l04_replay_serves_the_waits_and_compares_the_seconds_waited_by_kind():
    """L04: the waits are entered again from the recorded readings, so a changed figure is a divergence."""
    for name in ("waits_by_kind", "model_wait", "wait_left_open"):
        assert replay_record(SCENARIOS[name][0]()).totals["infra_wait_by_kind"]
    data = record_of("waits_by_kind")
    data["tool_calls"][0]["infra_wait_by_kind"]["lease_queue"] += 1
    diverges(data, "tool_calls[0].infra_wait_by_kind.lease_queue")
    data = record_of("waits_by_kind")
    data["totals"]["infra_wait_by_kind"]["sandbox_lifecycle"] = 1.5
    diverges(data, "totals.infra_wait_by_kind.sandbox_lifecycle")
    data = record_of("model_wait")
    data["transcript"][1]["infra_wait_by_kind"] = {}
    diverges(data, "transcript[1].infra_wait_by_kind.model_retry")


def test_l04_a_recorded_wait_of_a_kind_the_core_does_not_know_is_a_divergence_not_a_hang():
    """L04: a clock label that names an unknown kind stops the replay, and the schema refuses an unknown kind too."""
    data = record_of("waits_by_kind")
    index = next(i for i, (label, _) in enumerate(data["clock"]) if label == "wait_start:lease_queue")
    data["clock"][index][0] = "wait_start:tool_execution"
    error = diverges(data, "clock")
    assert "tool_execution" in str(error)
    data = record_of("waits_by_kind")
    data["totals"]["infra_wait_by_kind"] = {"data_access": 3.0}
    with pytest.raises(Exception, match="seconds waited are given per kind"):
        RunRecord.from_json(data)


def test_l04_a_wait_the_call_left_open_is_left_open_again_for_the_loop_to_close():
    """L04: the record says the call left one wait open; the replay leaves it open and the loop closes it."""
    record = SCENARIOS["wait_left_open"][0]()
    assert record.tool_calls[0]["waits_left_open"] == 1
    assert replay_record(record).tool_calls[0]["waits_left_open"] == 1


def test_r03_a_cut_at_the_ceiling_replays_as_infrastructure_of_the_ceiling_kind():
    """R03: the call is cut at the recorded watch reading, and the attempt ends as the ceiling's infrastructure."""
    record = SCENARIOS["ceiling_cut"][0]()
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("infrastructure", "ceiling")
    assert record.tool_calls[0]["status"] == "cut"
    replayed = replay_record(record)
    assert replayed.outcome == record.outcome
    assert replayed.totals["infra_wait_by_kind"] == record.totals["infra_wait_by_kind"] != {}


def test_l16_a_call_whose_own_expiry_was_judged_as_the_loops_cut_replays_as_that_cut():
    """L16: the expiry of a tool call's bound at the agent's time left is recorded as the cut it stands for, with the
    watch reading a cut takes, so the replay (which holds the call until that reading) ends the same way."""
    record = SCENARIOS["tool_expiry_cut"][0]()
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("time_cap", None)
    assert record.tool_calls[0]["status"] == "cut"
    assert "watch" in [label for label, _ in record.clock]
    replayed = replay_record(record)
    assert replayed.outcome == record.outcome
    assert replayed.clock == record.clock


def test_r03_a_cleanup_that_timed_out_is_served_from_the_label_of_the_reading_that_ended_it():
    """R03: the replay's cleanup waiter answers as the record says, so a call given up on replays as given up on."""
    record = SCENARIOS["cleanup_timeout"][0]()
    assert record.totals["cleanup_timed_out"] is True
    assert "cleanup_end_timed_out" in [label for label, _ in record.clock]
    assert replay_record(record).totals["cleanup_timed_out"] is True
    in_time = SCENARIOS["cleanup_in_time"][0]()
    assert (in_time.totals["cleanup_timed_out"], in_time.totals["cleanup_s"]) == (False, 3)
    assert replay_record(in_time).totals["cleanup_s"] == 3


def test_r03_a_changed_cleanup_label_is_a_divergence_in_what_the_loop_derives():
    """R03: the cleanup's answer comes from the record, so editing its label changes the totals the replay derives."""
    data = record_of("cleanup_timeout")
    index = next(i for i, (label, _) in enumerate(data["clock"]) if label == "cleanup_end_timed_out")
    data["clock"][index][0] = "cleanup_end"
    diverges(data, "totals.cleanup_timed_out")


def test_r03_a_watch_reading_nobody_takes_stops_the_replay_instead_of_waiting_for_it():
    """R03: a recorded watch reading that the loop's watch never takes is a divergence, not a hang."""

    async def serve():
        await ReplayClock([["watch", 1.0]]).let_watch_read()

    with pytest.raises(Halt) as caught:
        asyncio.run(serve())
    assert caught.value.divergence.path == "clock"
    assert "never taken" in caught.value.divergence.message


def test_l04_a_wait_that_lasted_no_time_is_recorded_as_zero_seconds_and_replays(tmp_path):
    """L04: a declared wait of 0.0 seconds is written, read back and replayed like any other."""
    record = SCENARIOS["zero_wait"][0]()
    assert record.totals["infra_wait_by_kind"] == {"lease_queue": 0.0}
    assert record.tool_calls[0]["infra_wait_by_kind"] == {}
    stored = RunRecord.from_json(decode(record.write(tmp_path).read_bytes()))
    assert replay_record(stored).totals["infra_wait_by_kind"] == {"lease_queue": 0.0}
