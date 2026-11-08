"""The agent timer, the tool box (manifest, duplicates, error classes) and the argument checks."""

import asyncio

import pytest
from loop_support import FakeClock, FakeProvider

from expcore.loop.arguments import argument_problems
from expcore.loop.timer import AgentTimer
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import ToolCall
from expcore.plugin import ToolOutput

# --- the timer ----------------------------------------------------------------------------------------------------


def test_the_timer_charges_elapsed_time_minus_waits_on_infrastructure():
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    timer.start()
    clock.advance(60)
    timer.add_infra_wait(50)
    tick = timer.check("t")
    assert (tick.elapsed_s, tick.agent_s, tick.remaining_s) == (60, 10, 90)
    assert timer.time_left() == 90


def test_the_timer_logs_every_reading_and_is_not_started_twice():
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    assert timer.time_left() is None  # a backend asking before the run starts gets no deadline
    timer.start()
    timer.check("a")
    timer.time_left()
    assert [label for label, _ in timer.readings] == ["start", "a", "time_left"]
    with pytest.raises(RuntimeError, match="already started"):
        timer.start()


def test_a_timer_that_has_not_started_cannot_be_checked():
    with pytest.raises(RuntimeError, match="not started"):
        AgentTimer(10, clock=FakeClock()).check("x")


def test_the_ceiling_cannot_be_below_the_cap_and_the_cap_must_be_positive():
    with pytest.raises(ValueError, match="ceiling"):
        AgentTimer(100, ceiling_s=50)
    with pytest.raises(ValueError, match="positive"):
        AgentTimer(0)


# --- the argument checks ------------------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "mode": {"enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nested": {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def test_arguments_that_fit_the_schema_pass():
    ok = {"name": "x", "count": 2, "ratio": 1, "flag": True, "mode": "a", "tags": ["t"], "nested": {"k": 1}}
    assert argument_problems(ok, SCHEMA) == []
    assert argument_problems({"name": "x", "count": 3.0}, SCHEMA) == []  # an integral float is an integer


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "missing required argument 'name'"),
        ({"name": 5}, "argument 'name': expected string, got integer"),
        ({"name": "x", "count": "2"}, "argument 'count': expected integer, got string"),
        ({"name": "x", "count": 2.5}, "argument 'count': expected integer, got number"),
        ({"name": "x", "flag": 1}, "argument 'flag': expected boolean, got integer"),
        ({"name": "x", "mode": "c"}, "argument 'mode': must be one of ['a', 'b']"),
        ({"name": "x", "tags": ["a", 1]}, "argument 'tags'[1]: expected string, got integer"),
        ({"name": "x", "nested": {}}, "missing required argument 'k'"),
        ({"name": "x", "extra": 1}, "unexpected argument 'extra'"),
    ],
)
def test_arguments_that_break_the_schema_are_described(arguments, expected):
    problems = argument_problems(arguments, SCHEMA)
    assert any(expected in problem for problem in problems), problems


def test_a_schema_keyword_the_check_does_not_know_is_left_to_the_tool():
    assert argument_problems({"x": 1}, {"type": "object", "properties": {"x": {"type": "integer", "minimum": 5}}}) == []


# --- the tool box -------------------------------------------------------------------------------------------------


def run_call(box, call, timeout_s=10.0):
    return asyncio.run(box.run(call, timeout_s=timeout_s))


def test_two_providers_offering_one_name_are_refused():
    with pytest.raises(ValueError, match="two providers offer a tool named 'echo'"):
        ToolBox([FakeProvider({"echo": str}), FakeProvider({"echo": str})], cap_chars=100)


def test_the_definitions_are_sorted_and_state_the_cap_and_the_manifest_hash_follows_them():
    box = ToolBox([FakeProvider({"zeta": str, "alpha": str})], cap_chars=500)
    defs = box.definitions()
    assert [d["name"] for d in defs] == ["alpha", "zeta"]
    assert all("longer than 500 characters" in d["description"] for d in defs)
    assert ToolBox([FakeProvider({"zeta": str, "alpha": str})], cap_chars=500).manifest_sha256 == box.manifest_sha256
    assert ToolBox([FakeProvider({"zeta": str, "alpha": str})], cap_chars=501).manifest_sha256 != box.manifest_sha256
    defs[0]["name"] = "changed"
    assert box.definitions()[0]["name"] == "alpha"  # a caller cannot change the box through what it was handed


def test_the_cap_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        ToolBox([], cap_chars=0)


def test_a_result_within_the_cap_is_returned_whole_and_unflagged():
    box = ToolBox([FakeProvider({"echo": lambda a: ToolOutput("fine", infra_wait_s=1.5)})], cap_chars=100)
    run = run_call(box, ToolCall("c", "echo", {}))
    assert (run.status, run.result.text, run.raw_text, run.truncated, run.infra_wait_s) == (
        "ok",
        "fine",
        "fine",
        False,
        1.5,
    )


def test_an_error_run_has_the_class_first_and_the_provider_is_not_called_for_a_bad_call():
    provider = FakeProvider({"echo": str})
    box = ToolBox([provider], cap_chars=100)
    unknown = run_call(box, ToolCall("c", "ghost", {}))
    assert unknown.result.is_error
    assert unknown.error_class == "UnknownTool"
    bad = run_call(box, ToolCall("c", "echo", {"x": 1}))
    assert bad.error_class == "InvalidArguments"
    assert provider.calls == []


def test_the_timeout_reaches_the_provider_unchanged():
    provider = FakeProvider({"echo": str})
    run_call(ToolBox([provider], cap_chars=100), ToolCall("c", "echo", {}), timeout_s=42.5)
    assert provider.calls[0][2] == 42.5
