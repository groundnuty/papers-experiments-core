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


def test_the_timer_pauses_the_agents_clock_while_a_wait_on_infrastructure_is_open():
    """L04: the wait is measured as it happens; inside it the agent's time stands still, after it the time runs on."""
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    timer.start()

    async def go():
        clock.advance(10)
        async with timer.infrastructure_wait():
            clock.advance(50)
            during = timer.check("during")
        clock.advance(5)
        return during, timer.check("after")

    during, after = asyncio.run(go())
    assert (during.elapsed_s, during.agent_s, during.remaining_s, during.paused) == (60, 10, 90, True)
    assert (after.elapsed_s, after.agent_s, after.remaining_s, after.paused) == (65, 15, 85, False)
    assert timer.infra_wait_s == 50
    assert timer.time_left() == 85


def test_overlapping_waits_pause_the_clock_for_their_union_and_not_their_sum():
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    timer.start()

    async def go():
        async with timer.infrastructure_wait():
            clock.advance(10)
            async with timer.infrastructure_wait():
                clock.advance(10)
            clock.advance(10)

    asyncio.run(go())
    assert timer.infra_wait_s == 30
    assert timer.check("t").agent_s == 0


def test_a_wait_marked_before_the_run_starts_measures_nothing():
    timer = AgentTimer(100, clock=FakeClock())

    async def go():
        async with timer.infrastructure_wait():
            pass

    asyncio.run(go())
    assert (timer.infra_wait_s, timer.readings) == (0, [])


def test_a_cut_ends_the_open_wait_at_the_cut_and_the_time_after_it_is_cleanup_not_the_agents():
    """L04: the wait still open at the cut ends there; the time cleaning up is kept apart, and the figures add up."""
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    timer.start()

    async def go():
        clock.advance(20)
        wait = timer.infrastructure_wait()
        await wait.__aenter__()
        clock.advance(30)
        cut = timer.check("cut")
        timer.begin_cleanup(cut)
        assert timer.cleaning
        clock.advance(7)  # the cancelled call takes this long to stop
        await wait.__aexit__(None, None, None)  # it unwinds its wait during cleanup: not counted again
        timer.end_cleanup()
        return timer.check("end")

    end = asyncio.run(go())
    assert (timer.infra_wait_s, timer.cleanup_s, timer.cleaning) == (30, 7, False)
    assert (end.elapsed_s, end.agent_s) == (57, 20)
    assert end.agent_s + timer.infra_wait_s + timer.cleanup_s == end.elapsed_s


def test_the_ceiling_left_and_the_next_look_follow_the_pause():
    """L04: while the agent's clock is paused only the ceiling can end a call; otherwise the nearer of the two."""
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock, ceiling_s=300)
    timer.start()
    clock.advance(40)
    running = timer.check("running")
    assert (running.ceiling_left_s, running.watch_s) == (260, 60)

    async def go():
        async with timer.infrastructure_wait():
            return timer.check("paused")

    paused = asyncio.run(go())
    assert (paused.paused, paused.watch_s) == (True, 260)


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
    box = ToolBox([FakeProvider({"echo": lambda a: ToolOutput("fine")})], cap_chars=100)
    run = run_call(box, ToolCall("c", "echo", {}))
    assert (run.status, run.result.text, run.raw_text, run.truncated) == ("ok", "fine", "fine", False)


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
