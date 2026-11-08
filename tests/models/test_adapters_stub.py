"""The scripted stub backend, and the turn type every backend returns."""

import asyncio

import pytest

from expcore.errors import InfraError
from expcore.models.adapters import ModelTurn, StubAdapter, ToolResult


def step(backend):
    return asyncio.run(backend.step())


def test_a_script_of_text_and_tool_calls_is_replayed_in_order():
    stub = StubAdapter([[("run", {"code": "1"})], "all done"], name="scripted", seed=5)
    stub.begin("system", "task", [])
    first = step(stub)
    assert [(c.name, c.arguments) for c in first.tool_calls] == [("run", {"code": "1"})]
    assert first.stop_reason == "tool_calls"
    stub.add_tool_results([ToolResult(first.tool_calls[0], "ok")])
    second = step(stub)
    assert (second.text, second.stop_reason, second.tool_calls) == ("all done", "stop", [])
    assert [m["role"] for m in stub.transcript()] == ["system", "user", "assistant", "tool", "assistant"]


def test_a_used_up_script_answers_done():
    stub = StubAdapter([])
    stub.begin("", "task", [])
    assert step(stub).text == "done"


def test_b12_a_scripted_reply_can_carry_the_length_stop_reason():
    """B12: the stub can produce the reply that was cut at the output limit."""
    stub = StubAdapter([{"text": "cut off", "stop": "length"}, "fine"])
    stub.begin("", "task", [])
    cut = step(stub)
    assert (cut.stop_reason, cut.hit_output_limit) == ("length", True)
    assert step(stub).hit_output_limit is False


def test_a_scripted_exception_is_raised_by_the_step_that_reaches_it():
    stub = StubAdapter(["first", InfraError("endpoint", "scripted outage")])
    stub.begin("", "task", [])
    step(stub)
    with pytest.raises(InfraError, match="scripted outage"):
        step(stub)


def test_tokens_are_synthetic_and_the_record_says_so():
    stub = StubAdapter(["x" * 40])
    stub.begin("s" * 8, "u" * 8, [])
    turn = step(stub)
    assert turn.input_tokens == 4
    assert turn.output_tokens == 11
    info = stub.info()
    assert info["synthetic_tokens"] is True
    assert (info["backend"], info["base_url"], info["settings_sent"]) == ("stub", None, {})


def test_probe_succeeds_unless_given_an_error():
    asyncio.run(StubAdapter([]).probe())
    with pytest.raises(InfraError, match="down"):
        asyncio.run(StubAdapter([], probe_error=InfraError("endpoint", "down")).probe())


def test_limits_default_to_unknown_and_can_be_set():
    assert (StubAdapter([]).output_limit, StubAdapter([]).context_tokens) == (None, None)
    stub = StubAdapter([], output_limit=100, context_tokens=1000)
    assert (stub.output_limit, stub.context_tokens) == (100, 1000)


def test_a_turn_defaults_leave_unreported_fields_none_and_a_non_limit_stop_is_not_cut():
    turn = ModelTurn("t", [], None, None, "stop", 0.1)
    assert (turn.input_tokens, turn.output_tokens, turn.served_model, turn.retries) == (None, None, None, ())
    assert not turn.hit_output_limit
    assert ModelTurn("t", [], 1, 1, None, 0.1).hit_output_limit is False
