"""Replay (R03): the real loop re-run from a record re-derives transcript, tool-call log, outcome and clock."""

import asyncio
import dataclasses
import socket

import pytest
from loop_support import FakeProvider
from record_support import make_config
from replay_scenarios import SCENARIOS, tools_provider

from expcore.loop.timer import AgentTimer
from expcore.loop.toolbox import ToolBox
from expcore.records.record import RunRecord, load_record
from expcore.replay.clock import BACKEND_CLOCK_LABELS, ReplayClock
from expcore.replay.recorded import TOOLBOX_ERROR_CLASSES
from expcore.replay.replay import ReplayDivergence, config_of, replay_async, replay_record


def blank_stack(outcome):
    """The stack hash of a harness defect comes from a live traceback and is the one thing replay cannot reproduce."""
    defect = outcome["defect"]
    return {**outcome, "defect": None if defect is None else {**defect, "stack_sha256": None}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A replay needs no model and no platform: any attempt to open a connection fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("a replay opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_R03_a_recorded_attempt_replays_through_the_real_loop_to_the_same_record(name, tmp_path):
    """R03: write, load, replay; the transcript, tool-call log, outcome, totals and clock come back byte for byte."""
    build, expected = SCENARIOS[name]
    record = build()
    assert record.outcome["name"] == expected
    stored = load_record(record.write(tmp_path))
    replayed = replay_record(stored)
    assert replayed.transcript == stored.transcript
    assert replayed.tool_calls == stored.tool_calls
    assert blank_stack(replayed.outcome) == blank_stack(stored.outcome)
    assert replayed.totals == stored.totals
    assert replayed.failed_request == stored.failed_request
    assert [r for r in stored.clock if r[0] not in BACKEND_CLOCK_LABELS] == replayed.clock
    assert replayed.dumps().encode() == replay_record(replayed).dumps().encode()


def test_R03_a_replay_of_a_dict_validates_it_first():
    """R03: the decoded JSON of a record is accepted, and checked against its schema before anything runs."""
    record = SCENARIOS["tools"][0]()
    assert replay_record(record.to_json()).outcome == record.outcome


def test_the_scenarios_cover_every_status_a_tool_call_can_have():
    statuses = set()
    for build, _ in SCENARIOS.values():
        statuses |= {c["status"] for c in build().tool_calls}
    assert statuses == {"ok", "error", "malformed", "skipped", "cut", "failed"}


def test_the_tool_box_gives_its_own_answers_the_error_classes_replay_does_not_serve_from_a_provider():
    """The replay treats these classes as answered by the box itself; this fails if the box renames them."""
    from loop_support import call

    box = ToolBox([tools_provider()], cap_chars=200)
    unknown = asyncio.run(box.run(call("c", "nosuch"), timeout_s=1))
    invalid = asyncio.run(box.run(call("c", "echo", {}), timeout_s=1))
    assert {unknown.error_class, invalid.error_class} == set(TOOLBOX_ERROR_CLASSES)


def test_the_label_replay_skips_is_the_label_the_timer_gives_the_backends_reads():
    timer = AgentTimer(10, clock=lambda: 1.0)
    timer.start()
    timer.time_left()
    assert {label for label, _ in timer.readings[1:]} == set(BACKEND_CLOCK_LABELS)


def test_R03_the_clock_readings_the_backend_took_are_recorded_and_not_served():
    """R03: a retry policy reads the loop's time inside a request; replay has no request to retry, so skips those."""
    record = SCENARIOS["time_left_readings"][0]()
    labels = [label for label, _ in record.clock]
    assert labels.count("time_left") == 2

    async def serve_all():
        clock = ReplayClock(record.clock)
        for _ in range(len(labels) - 2):
            clock()
        return clock.unused

    assert asyncio.run(serve_all()) == 0
    assert replay_record(record).outcome["name"] == "finished"


def test_a_replay_serves_the_clock_so_it_takes_no_time_and_does_not_sleep(monkeypatch):
    record = SCENARIOS["tools"][0]()

    real_sleep = asyncio.sleep

    async def refuse(seconds, *args):
        if seconds:
            raise AssertionError("a replay slept")
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", refuse)
    assert replay_record(record).outcome["name"] == "finished"


def test_replay_async_runs_on_the_callers_event_loop():
    record = SCENARIOS["malformed"][0]()
    assert asyncio.run(replay_async(record)).totals["malformed_calls"] == 1


def test_the_configuration_comes_from_the_record_not_from_the_defaults():
    record = SCENARIOS["turn_cap"][0]()
    config = config_of(record)
    assert config == make_config(max_tool_rounds=1)
    assert config.limits.max_tool_rounds == 1


def test_a_replay_leaves_the_record_it_was_given_unchanged():
    record = SCENARIOS["tools"][0]()
    before = record.dumps()
    replay_record(record)
    assert record.dumps() == before
    assert isinstance(record, RunRecord)


def test_a_replay_does_not_run_the_tools_it_serves_their_recorded_results():
    """Replay does not re-execute tools: a handler that would fail is never reached."""
    from replay_scenarios import HANDLERS

    def poisoned(_args):
        raise AssertionError("the tool ran")

    record = SCENARIOS["tools"][0]()
    live = FakeProvider({**HANDLERS, "echo": poisoned, "fail": poisoned, "submit": poisoned})
    replay_record(record)
    assert live.calls == []


def test_R03_a_treatment_attempt_replays_and_re_derives_its_flag_from_the_recorded_kinds():
    """R03: adds that the treatment flag is derived again from the recorded kinds and the replayed outcome."""
    record = SCENARIOS["infra_treatment"][0]()
    assert record.outcome["treatment"] is True
    replayed = replay_record(record)
    assert replayed.outcome == record.outcome
    assert replayed.treatment_kinds == ["endpoint"]
    assert replayed.set_aside is False


def test_R03_a_flipped_treatment_flag_is_a_divergence():
    """R03: adds that a flag turned either way after the run, past the load checks, no longer matches the replay."""
    treatment = SCENARIOS["infra_treatment"][0]()
    unflagged = dataclasses.replace(treatment, outcome={**treatment.outcome, "treatment": False})
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(unflagged)
    assert caught.value.path == "outcome.treatment"
    aside = SCENARIOS["infra_turn_three"][0]()
    flagged = dataclasses.replace(aside, outcome={**aside.outcome, "treatment": True})
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(flagged)
    assert caught.value.path == "outcome.treatment"


def test_R03_declared_kinds_edited_away_from_the_flag_are_a_divergence():
    """R03: adds that removing the recorded kinds while the flag stays set makes the replayed outcome differ."""
    treatment = SCENARIOS["infra_treatment"][0]()
    edited = dataclasses.replace(treatment, treatment_kinds=[])
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(edited)
    assert caught.value.path == "outcome.treatment"
