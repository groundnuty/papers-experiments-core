"""An outside cancellation of the loop leaves no child task of the loop pending, and a cancellation always ends."""

import asyncio
import threading
import time

import pytest
from loop_support import ClockSleep, FakeProvider, never

from expcore.loop.agent_loop import run_agent
from expcore.loop.toolbox import ToolBox
from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolSpec
from expcore.tools.function_provider import FunctionProvider


class Hangs(StubAdapter):
    """A backend whose request never returns; it needs the loop to clean up after it is cancelled."""

    def __init__(self, started, cleaned):
        super().__init__([])
        self.started, self.cleaned = started, cleaned

    async def step(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # cleanup that needs the loop: the call has to be awaited, not only cancelled
            self.cleaned.append("model")
            raise


@pytest.mark.parametrize("where", ["model request", "tool call"])
def test_an_outside_cancellation_cancels_and_awaits_the_children_before_it_propagates(make_config, clock, where):
    """Cancelling the whole run while a call is in flight cancels the call and the bound's timer and waits for both."""
    cleaned: list[str] = []
    config = make_config()

    async def scenario():
        started = asyncio.Event()

        async def hangs(args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleaned.append("tool")
                raise

        if where == "tool call":
            backend, providers = StubAdapter([[("hang", {})]]), [FakeProvider({"hang": hangs})]
        else:
            backend, providers = Hangs(started, cleaned), [FakeProvider({"t": lambda args: ""})]
        box = ToolBox(providers, cap_chars=config.limits.output_cap_chars)
        task = asyncio.ensure_future(run_agent(backend, box, "s", "u", config, clock=clock, sleep=never))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    pending = asyncio.run(scenario())
    assert pending == []
    assert cleaned == ["tool" if where == "tool call" else "model"]


# --- a cancellation always terminates ----------------------------------------------------------------------------


class StuckInCleanup(StubAdapter):
    """A backend whose cleanup waits on an event that is never set, so cancelling its request never finishes."""

    def __init__(self, started):
        super().__init__([])
        self.started = started

    async def step(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.Event().wait()


def test_a_cancellation_ends_within_the_cleanup_deadline_when_the_cancelled_call_will_not_stop(make_config, clock):
    """Cancelling the run while a request is stuck in its own cleanup gives up on it at the deadline, records that on
    the timer, and raises CancelledError instead of waiting for ever."""
    config = make_config(cleanup_deadline_s=0.05)
    timer = config.timer(clock)
    seen = {}

    async def scenario():
        started = asyncio.Event()
        box = ToolBox([FakeProvider({"t": lambda args: ""})], cap_chars=config.limits.output_cap_chars)
        task = asyncio.ensure_future(
            run_agent(StuckInCleanup(started), box, "s", "u", config, timer=timer, clock=clock, sleep=never)
        )
        await started.wait()
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        seen["took_s"] = time.monotonic() - began

    asyncio.run(scenario())  # the loop's own shutdown cancels the task that was given up on, which ends it
    assert seen["took_s"] < 2.0
    assert timer.cleanup_timed_out is True


def test_a_call_that_will_not_stop_after_a_cut_is_given_up_on_and_the_attempt_still_ends(run, clock, sleeper):
    """The same bound applies to a cut: the result says the cleanup timed out, and the ending stands."""

    async def stuck(args):
        clock.advance(300)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.Event().wait()

    result = run(
        StubAdapter([[("stuck", {})]]),
        [FakeProvider({"stuck": stuck})],
        sleep=sleeper,
        time_cap_s=300,
        cleanup_deadline_s=0.05,
    )
    assert result.outcome.name == "time_cap"
    assert result.cleanup_timed_out is True
    assert result.tool_calls[0].status == "cut"


def test_a_cleanup_that_finishes_in_time_is_not_a_timeout(run, clock, sleeper):
    async def stops_slowly(args):
        clock.advance(300)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)  # real time, well within the deadline

    result = run(
        StubAdapter([[("slow", {})]]),
        [FakeProvider({"slow": stops_slowly})],
        sleep=sleeper,
        time_cap_s=300,
        cleanup_deadline_s=5,
    )
    assert result.outcome.name == "time_cap"
    assert result.cleanup_timed_out is False


# --- threads that cannot be stopped (L15) ---------------------------------------------------------------------------


def blocking_provider(clock, started, release):
    """A function provider with one tool whose thread uses up the agent's cap on the fake clock and then blocks."""

    def blocks(args, timeout_s):
        clock.advance(300)
        started.set()
        release.wait(timeout=10)
        return "late"

    return FunctionProvider([(ToolSpec("blocks", "A tool that blocks."), blocks)])


def test_l15_the_loop_result_records_the_threads_a_cut_call_left_running(make_config, clock):
    """L15: the call is cut at the time cap; its thread cannot be killed, and the result says it may still run."""
    started, release = threading.Event(), threading.Event()
    config = make_config(time_cap_s=300)
    p = blocking_provider(clock, started, release)

    async def scenario():
        box = ToolBox([p], cap_chars=config.limits.output_cap_chars)
        try:
            return await run_agent(
                StubAdapter([[("blocks", {})]]), box, "s", "u", config, clock=clock, sleep=ClockSleep(clock)
            )
        finally:
            release.set()  # let the thread end, so that the loop can shut down

    result = asyncio.run(scenario())
    assert result.outcome.name == "time_cap"
    assert result.tool_calls[0].status == "cut"
    assert result.abandoned_threads == 1


def test_l15_a_run_with_no_abandoned_thread_records_none(run):
    result = run(StubAdapter(["done"]), [FakeProvider({"t": lambda a: ""})])
    assert result.abandoned_threads == 0


def test_l15_a_later_run_on_the_same_provider_gets_a_tool_error_and_records_the_thread_still_running(
    make_config, clock
):
    """L15: the second attempt's call is refused with an error naming the reason, and its result records the thread."""
    started, release = threading.Event(), threading.Event()
    config = make_config(time_cap_s=300)
    p = blocking_provider(clock, started, release)

    async def scenario():
        box = ToolBox([p], cap_chars=config.limits.output_cap_chars)
        try:
            first = await run_agent(
                StubAdapter([[("blocks", {})]]), box, "s", "u", config, clock=clock, sleep=ClockSleep(clock)
            )
            second = await run_agent(
                StubAdapter([[("blocks", {})], "done"]), box, "s", "u", config, clock=clock, sleep=ClockSleep(clock)
            )
        finally:
            release.set()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.abandoned_threads == 1
    (refused,) = second.tool_calls
    assert (refused.status, refused.error_class) == ("error", "AbandonedThread")
    assert "still running" in refused.text
    assert second.outcome.name == "finished"
    assert second.abandoned_threads == 1
