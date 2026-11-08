"""Leaked tool calls (L10): a call written as text or in the reasoning is labelled and not run; domain JSON is not."""

import json
import time

import pytest
from loop_support import FakeProvider, ScriptedBackend, call, turn

from expcore.loop.leaks import SCAN_LIMIT, leaked_tool
from expcore.models.adapters import ModelTurn
from expcore.models.adapters import StubAdapter as Stub

TOOLS = ["echo", "search"]
ECHO = {"echo": lambda args: "echoed", "search": lambda args: "found"}

LEAKS = {
    "bare object": '{"name": "echo", "arguments": {"x": "a"}}',
    "parameters instead of arguments": '{"name": "echo", "parameters": {"x": "a"}}',
    "in a code fence": 'I will look it up.\n```json\n{"name": "echo", "arguments": {}}\n```\nDone.',
    "in a tag": '<tool_call>{"name": "echo", "arguments": {"x": "a"}}</tool_call>',
    "chat API shape with a string of arguments": json.dumps(
        {"type": "function", "function": {"name": "echo", "arguments": json.dumps({"x": "a"})}}
    ),
    "in a list of calls": '{"tool_calls": [{"name": "echo", "arguments": {}}]}',
    "under the key tool": '{"tool": "echo", "arguments": {}}',
    "under the key tool_name": '{"tool_name": "echo", "parameters": {}}',
    "arguments first": '{"arguments": {"x": 1}, "name": "echo"}',
    "after prose with other braces": 'Use {curly} braces? Then {"name": "echo", "arguments": {"x": {"y": [1]}}}',
}

NOT_LEAKS = {
    "domain JSON naming no tool": '{"name": "Alice", "arguments": {"verbose": true}}',
    "a tool name with no arguments object": '{"name": "echo"}',
    "arguments that are not an object": '{"name": "echo", "arguments": [1, 2]}',
    "arguments that are plain text": '{"name": "echo", "arguments": "please"}',
    "a tool name as an ordinary value": '{"query": "echo", "arguments": {}}',
    "prose that mentions a tool": "I would call echo with the argument x, then search.",
    "JSON that does not parse": '{"name": "echo", "arguments": {',
    "an answer in JSON": '{"answer": "echo", "confidence": 0.9}',
    "empty": "",
}


@pytest.mark.parametrize("text", LEAKS.values(), ids=LEAKS.keys())
def test_l10_each_leak_shape_is_labelled(text):
    """L10: a JSON object naming an offered tool with an arguments or parameters object is a leaked call."""
    assert leaked_tool(text, TOOLS) == "echo"


@pytest.mark.parametrize("text", NOT_LEAKS.values(), ids=NOT_LEAKS.keys())
def test_l10_domain_json_that_names_no_offered_tool_is_not_a_leak(text):
    """L10: JSON that names no offered tool with an arguments object is the model's content, not a call."""
    assert leaked_tool(text, TOOLS) is None


def test_l10_a_tool_that_is_not_offered_is_not_a_leak():
    assert leaked_tool('{"name": "delete", "arguments": {}}', TOOLS) is None
    assert leaked_tool(LEAKS["bare object"], []) is None
    assert leaked_tool(None, TOOLS) is None


def test_l10_only_the_first_64_kib_are_scanned():
    """L10: a call inside the window is found; the same call placed after it is not."""
    call = LEAKS["bare object"]
    inside = " " * (SCAN_LIMIT - len(call) - 10) + call
    outside = " " * (SCAN_LIMIT + 10) + call
    assert leaked_tool(inside, TOOLS) == "echo"
    assert leaked_tool(outside, TOOLS) is None


def test_l10_a_hostile_reply_costs_a_bounded_scan():
    """L10: thousands of unclosed objects around the tool name do not make the scan slow."""
    hostile = '{"a":' * 12000 + '"echo"' + '{"b":' * 12000
    started = time.monotonic()
    assert leaked_tool(hostile, TOOLS) is None
    assert time.monotonic() - started < 2.0


# --- in the loop -----------------------------------------------------------------------------------------------------


def test_l10_a_leaked_call_in_the_text_ends_the_attempt_and_is_not_executed(run):
    """L10: the reply is labelled, nothing runs, and the model is not asked again."""
    provider = FakeProvider(ECHO)
    backend = Stub([LEAKS["bare object"], "second answer, never asked for"])
    result = run(backend, [provider])
    assert result.outcome.name == "leaked_tool_call"
    assert "text" in result.outcome.detail
    assert "'echo'" in result.outcome.detail
    assert result.outcome.set_aside is False
    assert provider.calls == []
    assert result.tool_calls == []
    assert len(result.turns) == 1
    assert result.final_text == LEAKS["bare object"]  # what the model wrote is kept as the evidence


def test_l10_a_leaked_call_in_the_reasoning_is_labelled_too(run):
    """L10: the reply's text is an ordinary sentence; the call is in the reasoning channel."""
    leaking = ModelTurn(
        "Done.", [], 10, 5, "stop", 0.0, reasoning=f"I should run {LEAKS['parameters instead of arguments']}"
    )
    provider = FakeProvider(ECHO)
    result = run(ScriptedBackend([leaking]), [provider])
    assert result.outcome.name == "leaked_tool_call"
    assert "reasoning" in result.outcome.detail
    assert provider.calls == []
    assert result.turns[0].reasoning is not None


def test_l10_domain_json_in_the_final_answer_is_an_ordinary_finish(run):
    """L10: an answer that is JSON naming no offered tool finishes as usual."""
    answer = '{"name": "Alice", "arguments": {"verbose": true}}'
    result = run(Stub([answer]), [FakeProvider(ECHO)])
    assert result.outcome.name == "finished"
    assert result.final_text == answer


def test_l10_a_reply_with_a_real_call_is_not_scanned(run):
    """L10: a reply that made a tool call is a tool round, whatever its text says."""
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend([turn(call("call_one", "echo"), text=LEAKS["bare object"]), "done"])
    result = run(backend, [provider])
    assert result.outcome.name == "finished"
    assert len(provider.calls) == 1
