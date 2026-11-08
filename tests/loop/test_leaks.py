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
    "arguments first": '{"arguments": {"x": 1}, "name": "echo"}',
    "other keys beside the name and arguments": '{"id": "call_1", "name": "echo", "arguments": {}}',
    "with whitespace around": '\n  {"name": "echo", "arguments": {}}  \n',
    "in a code fence with a language": '```json\n{"name": "echo", "arguments": {"x": "a"}}\n```',
    "in a code fence without a language": '```\n{"name": "echo", "arguments": {}}\n```',
    "wrapped once as tool_call": '{"tool_call": {"name": "echo", "arguments": {}}}',
    "wrapped once as function": '{"function": {"name": "echo", "parameters": {"x": "a"}}}',
    "wrapped once as tool": '{"tool": {"name": "echo", "arguments": {}}}',
    "a list of calls": '[{"name": "echo", "arguments": {}}, {"name": "search", "arguments": {"q": "a"}}]',
    "a list of wrapped calls in a fence": '```json\n[{"function": {"name": "echo", "arguments": {}}}]\n```',
}

NOT_LEAKS = {
    "an algorithm described in JSON, the tool name nested under it": json.dumps(
        {"algorithm": {"name": "search", "parameters": {"depth": 3}}, "notes": "breadth first"}
    ),
    "domain JSON naming no offered tool": '{"name": "Alice", "arguments": {"verbose": true}}',
    "a call nested in a list under another key": '{"tool_calls": [{"name": "echo", "arguments": {}}]}',
    "a call wrapped twice": '{"tool_call": {"function": {"name": "echo", "arguments": {}}}}',
    "a wrapper with another key beside it": '{"type": "function", "function": {"name": "echo", "arguments": {}}}',
    "prose before the object": 'I will call it: {"name": "echo", "arguments": {}}',
    "prose before a fenced block": 'Calling now:\n```json\n{"name": "echo", "arguments": {}}\n```',
    "prose after a fenced block": '```json\n{"name": "echo", "arguments": {}}\n```\nDone.',
    "two fenced blocks": "```json\n{call}\n```\n```json\n{call}\n```".replace(
        "{call}", '{"name": "echo", "arguments": {}}'
    ),
    "a list with an element that is not a call": '[{"name": "echo", "arguments": {}}, {"name": "Alice"}]',
    "an empty list": "[]",
    "a tool name with no arguments object": '{"name": "echo"}',
    "arguments that are a list": '{"name": "echo", "arguments": [1, 2]}',
    "arguments that are a string": '{"name": "echo", "arguments": "{\\"x\\": 1}"}',
    "a tool name as an ordinary value": '{"query": "echo", "arguments": {}}',
    "the name under another key": '{"tool": "echo", "arguments": {}}',
    "prose that mentions a tool": "I would call echo with the argument x, then search.",
    "JSON that does not parse": '{"name": "echo", "arguments": {',
    "an answer in JSON": '{"answer": "echo", "confidence": 0.9}',
    "empty": "",
}


@pytest.mark.parametrize("text", LEAKS.values(), ids=LEAKS.keys())
def test_l10_each_leak_shape_is_labelled(text):
    """L10: a top-level envelope that calls an offered tool with an arguments or parameters object is a leak."""
    assert leaked_tool(text, TOOLS) in TOOLS


@pytest.mark.parametrize("text", NOT_LEAKS.values(), ids=NOT_LEAKS.keys())
def test_l10_json_that_is_not_a_top_level_call_envelope_is_not_a_leak(text):
    """L10: a tool name nested deeper in domain JSON, or anything around the envelope, is content, not a call."""
    assert leaked_tool(text, TOOLS) is None


def test_l10_the_tool_named_is_the_one_returned():
    assert leaked_tool(LEAKS["wrapped once as function"], TOOLS) == "echo"
    assert leaked_tool('{"name": "search", "arguments": {}}', TOOLS) == "search"


def test_l10_a_tool_that_is_not_offered_is_not_a_leak():
    assert leaked_tool('{"name": "delete", "arguments": {}}', TOOLS) is None
    assert leaked_tool(LEAKS["bare object"], []) is None
    assert leaked_tool(None, TOOLS) is None


def test_l10_only_a_text_within_64_kib_is_looked_at():
    """L10: an envelope that fits the window is found; the same envelope grown past it is not looked at."""
    inside = json.dumps({"name": "echo", "arguments": {"x": "a" * (SCAN_LIMIT - 100)}})
    outside = json.dumps({"name": "echo", "arguments": {"x": "a" * (SCAN_LIMIT + 100)}})
    assert leaked_tool(inside, TOOLS) == "echo"
    assert leaked_tool(outside, TOOLS) is None


def test_l10_a_hostile_reply_costs_a_bounded_scan():
    """L10: JSON nested past what the parser takes is refused at once, not parsed for long."""
    started = time.monotonic()
    assert leaked_tool("[" * 30000 + "]" * 30000, TOOLS) is None
    assert leaked_tool('{"a":' * 12000, TOOLS) is None
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
    leaking = ModelTurn("Done.", [], 10, 5, "stop", 0.0, reasoning=LEAKS["in a code fence with a language"])
    provider = FakeProvider(ECHO)
    result = run(ScriptedBackend([leaking]), [provider])
    assert result.outcome.name == "leaked_tool_call"
    assert "reasoning" in result.outcome.detail
    assert provider.calls == []
    assert result.turns[0].reasoning is not None


def test_l10_reasoning_that_talks_about_a_call_in_prose_is_not_a_leak(run):
    """L10: the reasoning thinks aloud about a call and quotes it; only a reasoning that is the call counts."""
    thinking = ModelTurn(
        "Done.", [], 10, 5, "stop", 0.0, reasoning=f"I could run {LEAKS['bare object']} but the answer is known."
    )
    result = run(ScriptedBackend([thinking]), [FakeProvider(ECHO)])
    assert result.outcome.name == "finished"


def test_l10_domain_json_in_the_final_answer_is_an_ordinary_finish(run):
    """L10: an answer that is JSON naming no offered tool finishes as usual."""
    answer = '{"name": "Alice", "arguments": {"verbose": true}}'
    result = run(Stub([answer]), [FakeProvider(ECHO)])
    assert result.outcome.name == "finished"
    assert result.final_text == answer


def test_l10_an_algorithm_described_in_json_with_a_tool_name_nested_in_it_ends_as_finished(run):
    """L10: the answer describes an algorithm whose step is named like an offered tool; it is not a call."""
    answer = json.dumps({"algorithm": {"name": "search", "parameters": {"depth": 3}}, "notes": "breadth first"})
    provider = FakeProvider(ECHO)
    result = run(Stub([answer]), [provider])
    assert result.outcome.name == "finished"
    assert result.final_text == answer
    assert provider.calls == []


def test_l10_a_reply_with_a_real_call_is_not_scanned(run):
    """L10: a reply that made a tool call is a tool round, whatever its text says."""
    provider = FakeProvider(ECHO)
    backend = ScriptedBackend([turn(call("call_one", "echo"), text=LEAKS["bare object"]), "done"])
    result = run(backend, [provider])
    assert result.outcome.name == "finished"
    assert len(provider.calls) == 1
