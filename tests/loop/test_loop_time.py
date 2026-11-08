"""Time in the loop: the agent is charged only for its own time (L04), no hidden call limit (L05), injected clock.

Waits on infrastructure are measured live: a handler marks one with `async with ctx.infrastructure_wait()`, and the
agent's clock is paused while it is open. The fake clock moves only when a handler or a backend moves it, and the
loop's sleep (`sleeper`) ends when the fake clock reaches its bound, so a call is cut exactly when a test says time
passed.
"""

import asyncio

from loop_support import FakeProvider, ScriptedBackend, call, turn

from expcore.errors import InfraError
from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter


def slow(clock, seconds, *, wait=0.0, text="done"):
    """A tool handler that takes `seconds` on the fake clock, the first `wait` of them in a wait on infrastructure."""

    async def handler(args, ctx):
        if wait:
            async with ctx.infrastructure_wait():
                clock.advance(wait)
        clock.advance(seconds - wait)
        return text

    return handler


def stalls(clock, seconds, *, cleanup=0.0):
    """A tool handler that runs `seconds` on the fake clock and then never returns; stopping it takes `cleanup`."""

    async def handler(args):
        clock.advance(seconds)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            clock.advance(cleanup)
            raise

    return handler


def waits_on_infrastructure(clock, step=100.0, *, cleanup=0.0):
    """A tool handler that waits on infrastructure for ever, the fake clock moving `step` seconds as it yields."""

    async def handler(args, ctx):
        try:
            async with ctx.infrastructure_wait():
                while True:
                    clock.advance(step)
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            clock.advance(cleanup)
            raise

    return handler


class StallingBackend(StubAdapter):
    """A backend whose request number `stall_at` runs `seconds` on the fake clock and then never returns."""

    def __init__(self, script, clock, *, stall_at, seconds):
        super().__init__(script)
        self.clock, self.stall_at, self.seconds = clock, stall_at, seconds

    async def step(self):
        if self._i == self.stall_at:
            self.clock.advance(self.seconds)
            await asyncio.Event().wait()
        return await super().step()


def test_l04_a_slow_infrastructure_call_leaves_the_charged_time_under_the_cap(run, clock, sleeper):
    """L04: a call that waits 500 s on infrastructure, marked as such, leaves the charged time under the 300 s cap.

    The sleep ends when the fake clock reaches the bound, so the test passes only if no bound was reached: each call
    is watched from its start, and the model request after the second has only the ceiling's 200 s left to run in.
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
    assert result.cleanup_s == 0
    assert [t.infra_wait_s for t in result.tool_calls] == [500, 500]  # measured per call
    assert [c[2] for c in provider.calls] == [300, 300]  # each call was given the whole of the agent's time
    assert sleeper.bounds == [300, 300, 300, 300, 200]  # first looks; the last request had 200 s of ceiling left


def test_l04_a_slow_call_that_marks_no_wait_is_the_agents_time(run, clock):
    """L04: the same 500 s without a marked wait is charged, and the cap fires before the next model call."""
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


def test_l04_a_model_call_still_running_at_the_cap_is_cut_and_its_usage_is_unknown(run, clock, sleeper):
    """L04: the cap bounds the model call too; the abandoned request leaves the totals incomplete."""
    backend = StallingBackend([], clock, stall_at=0, seconds=300)
    result = run(backend, [FakeProvider({"t": lambda a: "x"})], sleep=sleeper, time_cap_s=300)
    assert result.outcome.name == "time_cap"
    assert result.usage_incomplete is True
    assert result.agent_s == 300


def test_l04_a_tool_call_still_running_at_the_agents_cap_is_cut_and_answered(run, clock, sleeper):
    """L04: a provider that never returns and marks no wait is the agent's time: cut at the cap, a time cap."""
    result = run(
        StubAdapter([[("hang", {})]]), [FakeProvider({"hang": stalls(clock, 300)})], sleep=sleeper, time_cap_s=300
    )
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert len([m for m in result.transcript if m["role"] == "tool"]) == 1
    assert result.agent_s == 300


def test_l04_a_long_wait_on_infrastructure_is_not_cut_at_the_agents_cap(run, clock, sleeper):
    """L04: a call waits 500 s on infrastructure under a 300 s cap while the loop watches it; the agent's clock is
    paused for the wait, so the call is not cut and nothing is charged."""

    async def waits(args, ctx):
        async with ctx.infrastructure_wait():
            for _ in range(5):
                clock.advance(100)
                await asyncio.sleep(0)  # let the loop look at the clock, as it would between real waits
        return "done"

    result = run(StubAdapter([[("store", {})], "done"]), [FakeProvider({"store": waits})], sleep=sleeper)
    assert result.outcome.name == "finished"
    assert result.agent_s == 0
    assert result.infra_wait_s == 500
    assert any(label == "watch" for label, _ in result.clock_readings)  # the loop did look, and let it go on


def test_l04_the_agents_clock_runs_again_when_the_wait_ends(run, clock, sleeper):
    """L04: 200 s of waiting, then the agent's own time in the same call: cut as a time cap at 300 s of agent time,
    not at 300 s after the call began (the cap would have found it at 100 s of agent time)."""

    async def waits_then_works(args, ctx):
        async with ctx.infrastructure_wait():
            clock.advance(200)
        for _ in range(100):
            clock.advance(10)
            await asyncio.sleep(0)

    result = run(StubAdapter([[("work", {})], "x"]), [FakeProvider({"work": waits_then_works})], sleep=sleeper)
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert result.infra_wait_s == 200
    assert 300 <= result.agent_s <= 340  # the agent's own 300 s, found within a few looks of the loop


def test_l04_a_wait_that_lasts_to_the_ceiling_is_cut_as_infrastructure_with_the_agent_under_its_cap(
    run, clock, sleeper
):
    """L04: still waiting when the ceiling passes: cut, infrastructure of the ceiling kind, agent time untouched."""
    result = run(
        StubAdapter([[("store", {})], "x"]),
        [FakeProvider({"store": waits_on_infrastructure(clock)})],
        sleep=sleeper,
        time_cap_s=300,
    )
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "ceiling"
    assert result.outcome.set_aside is True
    assert result.tool_calls[0].status == "cut"
    assert result.agent_s == 0
    assert result.infra_wait_s == result.elapsed_s  # the wait ended at the cut


def test_l04_cleanup_after_a_cut_is_harness_time_and_never_added_to_the_agents_time(run, clock, sleeper):
    """L04: stopping the cut call takes 500 s; agent time stays what it was at the cut, under the cap, and the
    outcome (infrastructure) agrees with the figures the record keeps."""
    result = run(
        StubAdapter([[("store", {})], "x"]),
        [FakeProvider({"store": waits_on_infrastructure(clock, cleanup=500)})],
        sleep=sleeper,
        time_cap_s=300,
    )
    assert result.outcome.name == "infrastructure"
    assert result.agent_s == 0
    assert result.cleanup_s == 500
    assert result.agent_s + result.infra_wait_s + result.cleanup_s == result.elapsed_s


def test_l04_cleanup_after_a_time_cap_cut_is_not_the_agents_time_either(run, clock, sleeper):
    """L04: cut at the 300 s cap, then 40 s of cleanup: the agent is charged 300 s, not 340."""
    result = run(
        StubAdapter([[("hang", {})]]),
        [FakeProvider({"hang": stalls(clock, 300, cleanup=40)})],
        sleep=sleeper,
        time_cap_s=300,
    )
    assert (result.outcome.name, result.agent_s, result.cleanup_s) == ("time_cap", 300, 40)
    assert result.elapsed_s == 340


def test_l04_a_tool_call_cut_at_the_ceiling_with_the_agents_time_also_used_up_is_a_time_cap(run, clock, sleeper):
    """L04: the agent's own cap decides first, also when a running call has passed both."""
    result = run(
        StubAdapter([[("hang", {})]]),
        [FakeProvider({"hang": stalls(clock, 450)})],
        sleep=sleeper,
        time_cap_s=300,
        ceiling_s=400,
        ceiling_kind="knowledge",
    )
    assert result.outcome.name == "time_cap"
    assert result.outcome.infra_kind is None


def test_l05_the_timeout_passed_to_the_provider_is_the_deadline_minus_now(run, clock):
    """L05: the first call gets the whole cap; the second gets the cap minus the 100 s the first took."""
    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], [("slow", {})], "done"]), [provider], time_cap_s=300)
    assert [c[2] for c in provider.calls] == [300, 200]


def test_l05_there_is_no_hidden_limit_beyond_the_remaining_time_and_the_ceiling(run, clock, sleeper):
    """L05: each call is watched for the agent's remaining time or the ceiling left, whichever comes first, and for
    nothing else."""
    provider = FakeProvider({"slow": slow(clock, 100)})
    run(StubAdapter([[("slow", {})], "done"]), [provider], sleep=sleeper, time_cap_s=300)
    assert sleeper.bounds == [300, 300, 200]  # model call, tool call, model call


def test_l04_a_model_request_is_watched_for_the_ceiling_left_when_that_is_less_than_the_agents_time(
    run, clock, sleeper
):
    """L04: after 700 s of waits the agent has all 300 s left but the ceiling only 200 s; the request gets 200 s."""
    provider = FakeProvider({"store": slow(clock, 700, wait=700)})
    run(StubAdapter([[("store", {})], "done"]), [provider], sleep=sleeper, time_cap_s=300)
    assert sleeper.bounds == [300, 300, 200]


def test_l04_a_model_request_cut_by_the_ceiling_with_the_agent_under_its_cap_is_infrastructure(run, clock, sleeper):
    """L04: the request was still running when the ceiling passed and the agent had time left: set aside, not a cap."""
    backend = StallingBackend([[("store", {})]], clock, stall_at=1, seconds=200)
    provider = FakeProvider({"store": slow(clock, 700, wait=700)})
    result = run(backend, [provider], sleep=sleeper, time_cap_s=300)
    assert result.outcome.name == "infrastructure"
    assert result.outcome.infra_kind == "ceiling"
    assert result.outcome.set_aside is True
    assert result.usage_incomplete is True
    assert result.agent_s == 200


def test_l04_a_model_request_cut_with_the_agents_time_and_the_ceiling_both_at_their_end_is_a_time_cap(
    run, clock, sleeper
):
    """L04: the agent's own cap decides first when the request runs out both limits at once."""
    backend = StallingBackend([[("store", {})]], clock, stall_at=1, seconds=300)
    provider = FakeProvider({"store": slow(clock, 600, wait=600)})
    result = run(backend, [provider], sleep=sleeper, time_cap_s=300)  # 300 s left of each
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
    """L04: the same 350 s, marked as waiting on infrastructure, leaves the agent under its cap: finished."""
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


def test_l04_the_live_waits_the_looks_and_the_cleanup_are_all_in_the_clock_readings(run, clock, sleeper):
    """L04: a replay serves every reading in order, so the wait boundaries, the loop's looks and the end of the
    cleanup are logged with their labels too, in time order."""
    result = run(
        StubAdapter([[("store", {}), ("later", {})], "x"]),
        [FakeProvider({"store": slow(clock, 20, wait=20), "later": waits_on_infrastructure(clock, cleanup=3)})],
        sleep=sleeper,
        time_cap_s=300,
    )
    assert result.outcome.name == "infrastructure"
    labels = [label for label, _ in result.clock_readings]
    assert {"wait_start", "wait_end", "watch", "cleanup_end"} <= set(labels)
    values = [value for _, value in result.clock_readings]
    assert values == sorted(values)


# --- the watch looks again when a wait opens or closes (L04) ---------------------------------------------------------


def waits_then_works_for_ever(clock, wait_s, step=1.0, yields=10):
    """A tool handler that waits on infrastructure for `wait_s` seconds and then works for ever, a step at a time.

    The loop gets `yields` turns between two steps of the fake clock, so what it does at a moment is not blurred by the
    clock having moved on while the loop was still waking up.
    """

    async def tick():
        clock.advance(step)
        for _ in range(yields):
            await asyncio.sleep(0)

    async def handler(args, ctx):
        async with ctx.infrastructure_wait():
            for _ in range(int(wait_s / step)):
                await tick()
        while True:
            await tick()

    return handler


def test_l04_a_wait_that_ends_moves_the_cut_to_the_agents_cap_not_the_ceiling(run, clock, sleeper):
    """L04: cap 10 s, ceiling 30 s, a wait from 0 to 11 s and then work: the agent's cap is reached 10 s of work after
    the wait, at 21 s, and the call is cut there with 10 s charged. Left to the first sleep, which went to the ceiling
    while the clock was paused, it would be cut at 30 s with 19 s charged."""
    result = run(
        StubAdapter([[("work", {})], "x"]),
        [FakeProvider({"work": waits_then_works_for_ever(clock, 11)})],
        sleep=sleeper,
        time_cap_s=10,
        ceiling_s=30,
    )
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert result.infra_wait_s == 11
    assert 10 <= result.agent_s <= 10.5
    assert 21 <= result.elapsed_s <= 21.5


def test_l04_the_watch_takes_a_reading_when_a_wait_opens_and_again_when_it_closes(run, clock, sleeper):
    """L04: the watch is woken by the wait itself, not only by its own sleep: with every sleep far off, there is a
    reading of the watch inside the wait and another after it."""

    async def waits_briefly(args, ctx):
        await asyncio.sleep(0)
        async with ctx.infrastructure_wait():
            for _ in range(5):
                clock.advance(1)
                await asyncio.sleep(0)
        for _ in range(5):
            clock.advance(1)
            await asyncio.sleep(0)
        return "done"

    result = run(
        StubAdapter([[("store", {})], "done"]),
        [FakeProvider({"store": waits_briefly})],
        sleep=sleeper,
        time_cap_s=300,
    )
    assert result.outcome.name == "finished"
    labels = [label for label, _ in result.clock_readings]
    start, end = labels.index("wait_start"), labels.index("wait_end")
    assert "watch" in labels[start:end]
    assert "watch" in labels[end:]


# --- a wait belongs to its call (L04) --------------------------------------------------------------------------------


def leaves_a_wait_open(clock, seconds, kept=None):
    """A tool handler that opens a wait on infrastructure, never closes it, and returns after `seconds`."""

    async def handler(args, ctx):
        wait = ctx.infrastructure_wait()
        await wait.__aenter__()
        if kept is not None:
            kept.append(wait)
        clock.advance(seconds)
        return "ok"

    return handler


def test_l04_a_wait_left_open_when_a_call_returns_is_closed_there_and_the_next_call_is_charged_normally(run, clock):
    """L04: the call returns with its wait still open; the loop closes it at that moment as infrastructure time,
    records it, and the next call's own time counts against the agent as usual."""
    provider = FakeProvider({"leaky": leaves_a_wait_open(clock, 40), "after": slow(clock, 10)})
    result = run(StubAdapter([[("leaky", {})], [("after", {})], "done"]), [provider], time_cap_s=300)
    assert result.outcome.name == "finished"
    assert [t.waits_left_open for t in result.tool_calls] == [1, 0]
    assert [t.infra_wait_s for t in result.tool_calls] == [40, 0]
    assert (result.infra_wait_s, result.agent_s) == (40, 10)
    assert result.agent_s + result.infra_wait_s + result.cleanup_s == result.elapsed_s


def test_l04_a_wait_left_open_by_a_call_that_raises_is_closed_when_it_raises(run, clock):
    """L04: the same when the provider raises: the invariant holds and the record says a wait was left open."""

    async def leaks_then_fails(args, ctx):
        await ctx.infrastructure_wait().__aenter__()
        clock.advance(5)
        raise InfraError("sandbox", "went away")

    result = run(StubAdapter([[("fail", {})], "x"]), [FakeProvider({"fail": leaks_then_fails})])
    assert result.outcome.infra_kind == "sandbox"
    assert result.tool_calls[0].status == "failed"
    assert result.tool_calls[0].waits_left_open == 1
    assert (result.infra_wait_s, result.agent_s) == (5, 0)
    assert result.agent_s + result.infra_wait_s + result.cleanup_s == result.elapsed_s


def test_l04_a_stale_wait_that_closes_late_does_not_close_the_next_calls_wait(run, clock):
    """L04: the first call's wait, closed by the loop at its end, is closed again by the next call: nothing happens, and
    the next call's own wait goes on to the end of its own block."""
    kept: list = []

    async def closes_the_old_wait(args, ctx):
        async with ctx.infrastructure_wait():
            clock.advance(5)
            await kept[0].__aexit__(None, None, None)  # the stale one
            clock.advance(5)  # still inside this call's own wait

    provider = FakeProvider({"leaky": leaves_a_wait_open(clock, 1, kept), "next": closes_the_old_wait})
    result = run(StubAdapter([[("leaky", {})], [("next", {})], "done"]), [provider])
    assert [t.infra_wait_s for t in result.tool_calls] == [1, 10]
    assert result.agent_s == 0


def test_l04_a_wait_a_model_request_leaves_open_is_closed_when_it_returns(make_config, clock):
    """L04: a model backend marks waits through the timer too; one left open ends with its request (turn says so)."""
    config = make_config()
    timer = config.timer(clock)

    class Leaks(StubAdapter):
        async def step(self):
            await timer.infrastructure_wait().__aenter__()
            clock.advance(7)
            return await super().step()

    box = ToolBox([FakeProvider({"t": lambda a: ""})], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(Leaks(["done"]), box, "s", "u", config, timer=timer, clock=clock))
    assert result.outcome.name == "finished"
    assert result.turns[0].waits_left_open == 1
    assert (result.infra_wait_s, result.agent_s) == (7, 0)
    assert result.agent_s + result.infra_wait_s + result.cleanup_s == result.elapsed_s
