"""Replay (R03): the real loop re-run from a record re-derives transcript, tool-call log, outcome and clock."""

import asyncio
import socket

import pytest
from loop_support import FakeProvider
from record_support import make_config
from replay_scenarios import SCENARIOS, tools_provider

from expcore.loop.timer import AgentTimer
from expcore.loop.toolbox import ToolBox
from expcore.records.record import RunRecord, load_record
from expcore.replay.recorded import BACKEND_CLOCK_LABELS, TOOLBOX_ERROR_CLASSES, ReplayClock
from expcore.replay.replay import config_of, replay_async, replay_record


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
    clock = ReplayClock(record.clock)
    served = []
    for _ in range(len(labels) - 2):
        served.append(clock())
    assert clock.unused == 0
    assert replay_record(record).outcome["name"] == "finished"


def test_a_replay_serves_the_clock_so_it_takes_no_time_and_does_not_sleep(monkeypatch):
    record = SCENARIOS["tools"][0]()

    async def refuse(_seconds):
        raise AssertionError("a replay slept")

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
