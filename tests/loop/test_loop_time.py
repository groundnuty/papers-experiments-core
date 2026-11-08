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


def test_l04_a_slow_infrastructure_call_leaves_the_charged_time_under_the_cap(run, clock, sleeper):
    """L04: a call that waits 500 s on infrastructure, reported as such, leaves the charged time under the 300 s cap.

    The sleep ends when the fake clock reaches the bound, so the test passes only if no bound was reached: each tool
    call was allowed up to the ceiling, and the model request after the second was bounded by the ceiling left.
    """
    provider = FakeProvider({"store": slow(clock, 500, wait=500)})
    result = run(
        StubAdapter([[("store", {})], [("store", {})], "done"]),
        [provider],
        sleep=sleeper,
        time_cap_s=300,
        ceiling_s=1200,
    )
    assert result.outcome.name == "finished"
    assert result.infra_wait_s == 1000
    assert result.elapsed_s == 1000
    assert result.agent_s == 0
    assert [c[2] for c in provider.calls] == [300, 300]  # each call was given the whole of the agent's time
    assert sleeper.bounds == [300, 1200, 300, 700, 200]  # the last request had 200 s of the ceiling left, not 300


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


def test_l04_a_tool_call_that_runs_on_to_the_ceiling_without_reporting_a_wait_is_cut_as_the_agents_time(
    run, clock, sleeper
):
    """L04: a provider that never returns and reports no wait has used the time as its own: cut, and a time cap."""

    async def hangs(args):
        clock.advance(900)  # it ran until the default ceiling of three times the cap
        await asyncio.Event().wait()

    result = run(StubAdapter([[("hang", {})]]), [FakeProvider({"hang": hangs})], sleep=sleeper)
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 1


def test_l05_the_timeout_passed_to_the_provider_is_the_deadline_minus_now(run, clock):
    """L05: the first call gets the whole cap; the second gets the cap minus the 100 s the first took."""
    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], [("slow", {})], "done"]), [provider], time_cap_s=300)
    assert [c[2] for c in provider.calls] == [300, 200]


def test_l05_there_is_no_hidden_limit_beyond_the_remaining_time_and_the_ceiling(run, clock):
    """L05: a model request is bounded by the remaining time and a tool call by the ceiling, which is three times the
    cap unless the run says otherwise; no other limit is added."""
    bounds: list[float] = []

    async def recording_sleep(seconds):
        bounds.append(seconds)
        await asyncio.Event().wait()

    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], "done"]), [provider], sleep=recording_sleep, time_cap_s=300)
    assert bounds == [300, 900, 200]  # model call, tool call, model call


def test_l04_a_long_wait_on_infrastructure_is_not_cut_at_the_remaining_time_plus_a_grace(run, clock, sleeper):
    """L04: with the default ceiling a call may wait 500 s on infrastructure under a 300 s cap, and the wait is not
    charged to the agent; the bound it ran under is the ceiling left, 900 s."""
    provider = FakeProvider({"store": slow(clock, 500, wait=500)})
    result = run(StubAdapter([[("store", {})], "done"]), [provider], sleep=sleeper, time_cap_s=300)
    assert result.outcome.name == "finished"
    assert result.agent_s == 0
    assert sleeper.bounds[1] == 900


def test_l04_a_model_request_is_bounded_by_the_ceiling_left_when_that_is_less_than_the_agents_time(run, clock, sleeper):
    """L04: after 700 s of waits the agent has all 300 s left but the ceiling only 200 s; the request gets 200 s."""
    provider = FakeProvider({"store": slow(clock, 700, wait=700)})
    run(StubAdapter([[("store", {})], "done"]), [provider], sleep=sleeper, time_cap_s=300)
    assert sleeper.bounds == [300, 900, 200]


def test_l04_a_model_request_cut_by_the_ceiling_with_the_agent_under_its_cap_is_infrastructure(run, clock):
    """L04: the request was still running when the ceiling passed and the agent had time left: set aside, not a cap."""

    class Hangs(StubAdapter):
        async def step(self):
            if self._i == 0:
                return await super().step()
            await asyncio.Event().wait()

    provider = FakeProvider({"store": slow(clock, 700, wait=700)})
    result = run(Hangs([[("store", {})]]), [provider], sleep=at_once, time_cap_s=300)
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "ceiling"
    assert result.outcome.set_aside is True
    assert result.usage_incomplete is True


def test_l04_a_model_request_cut_with_the_agents_time_and_the_ceiling_both_at_their_end_is_a_time_cap(run, clock):
    """L04: the agent's own cap decides first when the request runs out both bounds at once."""

    class Hangs(StubAdapter):
        async def step(self):
            if self._i == 0:
                return await super().step()
            await asyncio.Event().wait()

    provider = FakeProvider({"store": slow(clock, 600, wait=600)})
    result = run(Hangs([[("store", {})]]), [provider], sleep=at_once, time_cap_s=300)  # 300 s left of each
    assert result.outcome.name == "time_cap"


def test_l04_with_a_ceiling_a_tool_call_may_wait_on_infrastructure_up_to_the_ceiling(run, clock):
    """L04: the wall-time bound of a tool call is the ceiling minus the time elapsed, so reported waits are not cut."""
    bounds: list[float] = []

    async def recording_sleep(seconds):
        bounds.append(seconds)
        await asyncio.Event().wait()

    provider = FakeProvider({"store": slow(clock, 200, wait=200)})
    run(
        StubAdapter([[("store", {})], "done"]),
        [provider],
        sleep=recording_sleep,
        time_cap_s=300,
        ceiling_s=1000,
        ceiling_kind="sandbox",
    )
    assert bounds == [
        300,
        1000,
        300,
    ]  # the model call, the tool call up to the ceiling, the model call: 200 s were waits


def test_l04_a_tool_call_cut_at_the_ceiling_with_the_agent_under_its_cap_is_infrastructure(run, clock):
    """L04: the agent's own time was not used up, so the cut call is infrastructure of the declared kind."""

    async def hangs(args):
        clock.advance(200)
        await asyncio.Event().wait()

    provider = FakeProvider({"store": slow(clock, 200, wait=200), "hang": hangs})
    result = run(
        StubAdapter([[("store", {})], [("hang", {})]]),
        [provider],
        sleep=at_once,
        time_cap_s=300,
        ceiling_s=400,
        ceiling_kind="knowledge",
    )
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "knowledge"
    assert result.tool_calls[-1].status == "cut"


def test_l04_a_tool_call_cut_at_the_ceiling_with_the_agents_time_also_used_up_is_a_time_cap(run, clock):
    """L04: the agent's own cap decides first, also when it is a running call that crosses both."""

    async def hangs(args):
        clock.advance(450)
        await asyncio.Event().wait()

    result = run(
        StubAdapter([[("hang", {})]]),
        [FakeProvider({"hang": hangs})],
        sleep=at_once,
        time_cap_s=300,
        ceiling_s=400,
        ceiling_kind="knowledge",
    )
    assert result.outcome.name == "time_cap"


def test_l04_a_terminal_result_that_arrives_after_the_agents_cap_is_not_accepted(run, clock):
    """L04: the submission came back 350 s in, over the 300 s cap: a time cap, not finished. The result is still
    logged and answered, and the calls after it are answered as skipped."""
    provider = FakeProvider(
        {"submit": slow(clock, 350, text="accepted"), "later": lambda a: "never"}, terminal=("submit",)
    )
    result = run(StubAdapter([[("submit", {}), ("later", {})], "never asked"]), [provider], time_cap_s=300)
    assert result.outcome.name == "time_cap"
    assert "terminal tool 'submit' returned" in result.outcome.detail
    assert [t.status for t in result.tool_calls] == ["ok", "skipped"]
    assert result.tool_calls[0].text == "accepted"
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 2
    assert result.final_text is None
    assert len(result.turns) == 1


def test_l04_a_terminal_result_after_a_long_reported_wait_is_accepted(run, clock):
    """L04: the same 350 s, reported as waiting on infrastructure, leaves the agent under its cap: finished."""
    provider = FakeProvider({"submit": slow(clock, 350, wait=350)}, terminal=("submit",))
    result = run(StubAdapter([[("submit", {})], "never asked"]), [provider], time_cap_s=300)
    assert result.outcome.name == "finished"


def test_l04_a_terminal_result_that_arrives_after_the_ceiling_with_the_agent_under_its_cap_is_infrastructure(
    run, clock
):
    """L04: the agent's time was not used up, but the result came after the ceiling: set aside, not finished."""
    provider = FakeProvider({"submit": slow(clock, 450, wait=450)}, terminal=("submit",))
    result = run(
        StubAdapter([[("submit", {})], "never asked"]),
        [provider],
        time_cap_s=300,
        ceiling_s=400,
        ceiling_kind="sandbox",
    )
    assert (result.outcome.name, result.outcome.infra_kind) == ("infrastructure", "sandbox")


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
