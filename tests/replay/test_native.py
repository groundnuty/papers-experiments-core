"""The neutral turn that follows from a native message, for each shape a backend writes (R03)."""

import copy
import json

import pytest
from record_support import record_run
from replay_scenarios import HANDLERS, SCENARIOS

from expcore.records.record import RunRecord
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.native import (
    anthropic_neutral,
    malformed_flags,
    neutral_of,
    normalised,
    openai_neutral,
    register_native_parser,
)
from expcore.replay.replay import replay_record


def call(call_id, name, sent):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": sent}}


def test_R03_a_chat_completions_message_gives_its_text_reasoning_and_calls():
    """R03: text and reasoning as written, each call with the arguments it was sent."""
    native = {
        "role": "assistant",
        "content": "looking",
        "_reasoning": "because",
        "tool_calls": [call("call-1", "echo", '{"x": 1}'), call("call-2", "echo", "")],
    }
    derived = openai_neutral(native)
    assert derived["text"] == "looking"
    assert derived["reasoning"] == "because"
    assert derived["tool_calls"] == [
        {"id": "call-1", "name": "echo", "arguments": {"x": 1}, "parse_error": None, "raw_arguments": None},
        {"id": "call-2", "name": "echo", "arguments": {}, "parse_error": None, "raw_arguments": None},
    ]


def test_R03_arguments_that_were_not_an_object_follow_from_the_raw_text_the_history_keeps():
    """R03: the history keeps {"_raw": text}; the error is the adapter's own parse of that text."""
    native = {
        "role": "assistant",
        "content": None,
        "tool_calls": [call("call-1", "echo", json.dumps({"_raw": '{"a":'}))],
    }
    (derived,) = openai_neutral(native, [True])["tool_calls"]
    assert derived["arguments"] == {}
    assert derived["raw_arguments"] == '{"a":'
    assert derived["parse_error"].startswith("arguments are not valid JSON")
    assert openai_neutral(native, [True])["text"] == ""


def test_R03_a_valid_call_with_a_raw_key_and_a_malformed_call_are_told_apart_by_the_record_not_by_the_key():
    """R03: {"_raw": "[1]"} is one history entry for a valid and for a malformed call; the record's flag decides."""
    native = {"role": "assistant", "content": None, "tool_calls": [call("call-1", "echo", json.dumps({"_raw": "[1]"}))]}
    (valid,) = openai_neutral(native, [False])["tool_calls"]
    assert (valid["arguments"], valid["parse_error"], valid["raw_arguments"]) == ({"_raw": "[1]"}, None, None)
    (broken,) = openai_neutral(native, [True])["tool_calls"]
    assert (broken["arguments"], broken["raw_arguments"]) == ({}, "[1]")
    assert broken["parse_error"] == "arguments are not a JSON object"
    block = {"type": "tool_use", "id": "use-1", "name": "echo", "input": {"_raw": "[1]"}}
    (valid,) = anthropic_neutral({"role": "assistant", "content": [block]}, [False])["tool_calls"]
    assert (valid["arguments"], valid["parse_error"], valid["raw_arguments"]) == ({"_raw": "[1]"}, None, None)
    (broken,) = anthropic_neutral({"role": "assistant", "content": [block]}, [True])["tool_calls"]
    assert (broken["arguments"], broken["raw_arguments"], broken["parse_error"]) == (
        {},
        "[1]",
        "arguments are not a JSON object",
    )


def test_R03_a_call_recorded_as_malformed_whose_raw_text_is_really_an_object_does_not_follow_from_its_native_message():
    """R03: the flag must agree with the native message: raw text that parses as an object is not a malformed call."""
    native = {
        "role": "assistant",
        "content": None,
        "tool_calls": [call("call-1", "echo", json.dumps({"_raw": '{"a": 1}'}))],
    }
    (derived,) = openai_neutral(native, [True])["tool_calls"]
    assert derived["parse_error"] is None
    block = {"type": "tool_use", "id": "use-1", "name": "echo", "input": {"_raw": '{"a": 1}'}}
    (derived,) = anthropic_neutral({"role": "assistant", "content": [block]}, [True])["tool_calls"]
    assert "holds no raw text" in derived["parse_error"]


def test_R03_a_valid_call_with_arguments_that_are_a_raw_key_replays_faithfully():
    """R03: the stub writes the chat-completions shape; the call is valid, so it replays as it ran."""
    record = record_run([[("echo", {"_raw": "[1]"})], "done"], HANDLERS)
    call_ = record.transcript[0]["neutral"]["tool_calls"][0]
    assert (call_["arguments"], call_["parse_error"]) == ({"_raw": "[1]"}, None)
    replayed = replay_record(record)
    assert replayed.transcript[0]["neutral"] == record.transcript[0]["neutral"]
    assert replayed.tool_calls == record.tool_calls


def test_R03_flipping_the_malformed_flag_of_a_recorded_call_diverges():
    """R03: the flag is an input to the parse, and what it yields must still be what the record holds."""
    data = copy.deepcopy(record_run([[("echo", {"_raw": "[1]"})], "done"], HANDLERS).to_json())
    data["transcript"][0]["neutral"]["tool_calls"][0]["parse_error"] = "arguments are not a JSON object"
    data["transcript"][0]["neutral"]["tool_calls"][0]["raw_arguments"] = "[1]"
    data["transcript"][0]["neutral"]["tool_calls"][0]["arguments"] = {}
    with pytest.raises(ReplayDivergence):
        replay_record(RunRecord.from_json(data))


def test_R03_a_messages_api_message_gives_text_thinking_and_calls():
    """R03: text blocks joined, thinking blocks joined by newlines, tool_use blocks as calls."""
    native = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "first", "signature": "s"},
            {"type": "text", "text": "a"},
            {"type": "redacted_thinking", "data": "xx"},
            {"type": "thinking", "thinking": "second"},
            {"type": "text", "text": "b"},
            {"type": "tool_use", "id": "use-1", "name": "echo", "input": {"x": "1"}},
            {"type": "tool_use", "id": "use-2", "name": "echo", "input": {"_raw": "[1, 2]"}},
        ],
    }
    derived = anthropic_neutral(native, [False, True])
    assert (derived["text"], derived["reasoning"]) == ("ab", "first\nsecond")
    assert derived["tool_calls"][0] == {
        "id": "use-1",
        "name": "echo",
        "arguments": {"x": "1"},
        "parse_error": None,
        "raw_arguments": None,
    }
    assert derived["tool_calls"][1] == {
        "id": "use-2",
        "name": "echo",
        "arguments": {},
        "parse_error": "arguments are not a JSON object",
        "raw_arguments": "[1, 2]",
    }


def test_R03_the_stub_backend_writes_the_chat_completions_shape_and_an_unknown_backend_has_no_parser():
    native = {"role": "assistant", "content": "hi"}
    assert neutral_of("stub", native) == neutral_of("openai_compat", native)
    assert neutral_of("anthropic", {"role": "assistant", "content": []})["text"] == ""
    assert neutral_of("elsewhere", native) is None
    assert neutral_of(None, native) is None


def test_R03_a_backend_registers_the_parser_of_its_own_native_shape():
    """R03: a record of a backend with its own shape is replayable once its parser is registered."""
    register_native_parser(
        "shape_x", lambda native, malformed: {"text": native["t"], "reasoning": None, "tool_calls": []}
    )
    assert neutral_of("shape_x", {"t": "hello"})["text"] == "hello"


def test_R03_reasoning_that_is_empty_counts_as_none_on_both_sides():
    assert normalised({"text": "", "reasoning": "", "tool_calls": []})["reasoning"] is None


def test_R03_every_scenario_turn_follows_from_its_native_message_as_the_backends_wrote_it():
    """R03: the recorded neutral turn of each scenario equals the one derived from its native message."""
    for name, (build, _) in SCENARIOS.items():
        record = build()
        for turn in record.transcript:
            derived = neutral_of(record.route["backend"], turn["native"], malformed_flags(turn["neutral"]))
            assert derived == normalised(turn["neutral"]), name


def test_R03_a_run_with_an_abandoned_worker_replays_faithfully():
    """R03: the provider's count of worker threads left behind is served from the record."""
    record = SCENARIOS["abandoned_worker"][0]()
    assert record.totals["abandoned_threads"] == 2
    assert replay_record(record).totals["abandoned_threads"] == 2
