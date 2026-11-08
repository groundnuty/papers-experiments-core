"""Time in the loop: the agent is charged only for its own time (L04), no hidden call limit (L05), injected clock."""

import asyncio

from loop_support import FakeProvider, ScriptedBackend, at_once, call, turn

from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolOutput


def slow(clock, seconds, *, wait=0.0, text="done"):
    """A tool handler that takes `seconds` on the fake clock and reports `wait` of it as waiting on infrastructure."""

    def handler(args):
        clock.advance(seconds)
        return ToolOutput(text, infra_wait_s=wait)

    return handler


def test_l04_a_slow_infrastructure_call_leaves_the_charged_time_under_the_cap(run, clock):
    """L04: a call that waits 500 s on infrastructure, reported as such, leaves the charged time under the 300 s cap."""
    provider = FakeProvider({"store": slow(clock, 500, wait=500)})
    result = run(StubAdapter([[("store", {})], [("store", {})], "done"]), [provider], time_cap_s=300)
    assert result.outcome.name == "finished"
    assert result.infra_wait_s == 1000
    assert result.elapsed_s == 1000
    assert result.agent_s == 0
    assert [c[2] for c in provider.calls] == [300, 300]  # each call was given the whole of the agent's time


def test_l04_a_slow_call_that_reports_no_wait_is_the_agents_time(run, clock):
    """L04: the same 500 s without the report is charged, and the cap fires before the next model call."""
    provider = FakeProvider({"slow": slow(clock, 500)})
    result = run(StubAdapter([[("slow", {})], "never asked"]), [provider], time_cap_s=300)
    assert result.outcome.name == "time_cap"
    assert result.agent_s == 500
    assert len(result.turns) == 1


def test_l04_the_wall_cap_is_checked_before_every_tool_call_of_a_round(run, clock):
    """L04: when the first call uses up the time, the rest of the round is not started and is answered as skipped."""
    provider = FakeProvider({"slow": slow(clock, 301), "later": lambda a: "never"})
    result = run(StubAdapter([[("slow", {}), ("later", {}), ("later", {})]]), [provider], time_cap_s=300)
    assert result.outcome.name == "time_cap"
    assert [c[0] for c in provider.calls] == ["slow"]
    assert [t.status for t in result.tool_calls] == ["ok", "skipped", "skipped"]
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 3
    assert "run's time was used up" in result.tool_calls[1].text


def test_l04_own_time_at_the_cap_gives_a_time_cap_even_when_the_ceiling_is_also_passed(run, clock):
    """L04: the agent's own cap decides before any whole-time ceiling."""
    provider = FakeProvider({"slow": slow(clock, 450)})
    result = run(StubAdapter([[("slow", {})], "x"]), [provider], time_cap_s=300, ceiling_s=400, ceiling_kind="sandbox")
    assert result.outcome.name == "time_cap"
    assert result.outcome.infra_kind is None


def test_l04_a_ceiling_passed_while_the_agent_is_under_its_cap_is_infrastructure_of_the_declared_kind(run, clock):
    """L04: waits that pass the ceiling, with the agent still under its cap, end the attempt as infrastructure."""
    provider = FakeProvider({"store": slow(clock, 450, wait=450)})
    result = run(
        StubAdapter([[("store", {})], "x"]), [provider], time_cap_s=300, ceiling_s=400, ceiling_kind="knowledge"
    )
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "knowledge"
    assert result.outcome.set_aside is True
    assert result.agent_s == 0


def test_l04_a_model_call_still_running_at_the_cap_is_cut_and_its_usage_is_unknown(run):
    """L04: the cap bounds the model call too; the abandoned request leaves the totals incomplete."""

    class Hangs(StubAdapter):
        async def step(self):
            await asyncio.Event().wait()

    result = run(Hangs([]), [FakeProvider({"t": lambda a: "x"})], sleep=at_once)
    assert result.outcome.name == "time_cap"
    assert result.usage_incomplete is True


def test_l04_a_tool_call_still_running_at_the_cap_is_cut_and_answered(run):
    """L04: a provider that never returns is cut when its bound passes; the call is logged cut."""

    async def hangs(args):
        await asyncio.Event().wait()

    result = run(StubAdapter([[("hang", {})]]), [FakeProvider({"hang": hangs})], sleep=at_once)
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 1


def test_l05_the_timeout_passed_to_the_provider_is_the_deadline_minus_now(run, clock):
    """L05: the first call gets the whole cap; the second gets the cap minus the 100 s the first took."""
    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], [("slow", {})], "done"]), [provider], time_cap_s=300)
    assert [c[2] for c in provider.calls] == [300, 200]


def test_l05_there_is_no_hidden_limit_beyond_the_remaining_time(run, clock):
    """L05: the model call is bounded by the remaining time and a tool call by it plus only the declared grace."""
    bounds: list[float] = []

    async def recording_sleep(seconds):
        bounds.append(seconds)
        await asyncio.Event().wait()

    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], "done"]), [provider], sleep=recording_sleep, time_cap_s=300, tool_grace_s=7)
    assert bounds == [300, 307, 200]  # model call, tool call, model call


def test_l05_no_call_starts_with_less_than_a_second_left(run, clock):
    """L05: with half a second left a tool call is not started and the attempt ends as time cap."""
    provider = FakeProvider({"slow": slow(clock, 299.5), "later": lambda a: "never"})
    backend = ScriptedBackend([turn(call("call_one", "slow")), turn(call("call_two", "later"))])
    result = run(backend, [provider], time_cap_s=300)
    assert result.outcome.name == "time_cap"
    assert [c[0] for c in provider.calls] == ["slow"]
    assert result.tool_calls[1].status == "skipped"
    assert "left, too little to start a tool call" in result.outcome.detail


def test_every_clock_reading_is_recorded_with_a_label(run, clock):
    """The injected clock is the only source of time, and each reading is logged so a replay can serve it."""
    provider = FakeProvider({"slow": slow(clock, 10)})
    result = run(StubAdapter([[("slow", {})], "done"]), [provider])
    labels = [label for label, _ in result.clock_readings]
    assert labels[0] == "start"
    assert labels[-1] == "end"
    assert {"before_model", "before_tool", "after_tool"} <= set(labels)
    values = [value for _, value in result.clock_readings]
    assert values == sorted(values)
    assert values[0] == 1000.0
