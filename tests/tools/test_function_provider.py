"""The function provider: results, errors, and that blocking functions never run on the event loop (L15)."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from loop_support import FakeClock

from expcore.loop.timer import AgentTimer, WaitContext
from expcore.plugin import ToolOutput, ToolProvider, ToolSpec
from expcore.tools.function_provider import FunctionProvider


def provider(**functions):
    return FunctionProvider([(ToolSpec(name, f"The {name} tool."), function) for name, function in functions.items()])


def execute(p, name, arguments=None):
    return asyncio.run(p.execute(name, arguments or {}, timeout_s=5.0))


def test_a_function_result_becomes_a_tool_output():
    p = provider(shout=lambda args, timeout_s: args["text"].upper())
    assert isinstance(p, ToolProvider)
    assert [s.name for s in p.tools()] == ["shout"]
    assert execute(p, "shout", {"text": "hi"}) == ToolOutput("HI")


def test_a_function_may_return_a_full_tool_output():
    failing = ToolOutput("no", is_error=True, error_class="Nope")
    assert execute(provider(f=lambda args, timeout_s: failing), "f") == failing


def test_a_coroutine_function_runs_on_the_loop():
    seen = {}

    async def where(args):
        seen["thread"] = threading.current_thread()
        return "ok"

    async def go():
        seen["loop_thread"] = threading.current_thread()
        return await provider(w=where).execute("w", {}, timeout_s=1.0)

    assert asyncio.run(go()) == ToolOutput("ok")
    assert seen["thread"] is seen["loop_thread"]


def test_an_exception_in_a_function_reaches_the_caller_unchanged():
    def boom(args, timeout_s):
        raise KeyError("missing")

    with pytest.raises(KeyError):
        execute(provider(boom=boom), "boom")


def test_two_functions_for_one_tool_are_refused():
    spec = ToolSpec("t", "d")
    with pytest.raises(ValueError, match="two functions"):
        FunctionProvider([(spec, lambda args, timeout_s: ""), (spec, lambda args, timeout_s: "")])


def test_l15_blocking_waits_never_run_on_the_event_loop():
    """L15: two concurrent calls that each block until the other has started overlap in wall time.

    On the event loop the first would block it and the second could never start, so the barrier would time out.
    """
    barrier = threading.Barrier(2, timeout=5.0)

    def wait_for_the_other(args, timeout_s):
        barrier.wait()
        return "met"

    p = provider(rendezvous=wait_for_the_other)

    async def both():
        return await asyncio.gather(
            p.execute("rendezvous", {}, timeout_s=5.0), p.execute("rendezvous", {}, timeout_s=5.0)
        )

    started = time.monotonic()
    assert asyncio.run(both()) == [ToolOutput("met"), ToolOutput("met")]
    assert time.monotonic() - started < 4.0


# --- the remaining time reaches a plain function (L05) ---------------------------------------------------------------


def test_l05_a_plain_function_is_handed_the_time_the_run_has_left_so_it_can_bound_itself():
    """L05: the timeout_s keyword carries the run's remaining time, unchanged, into the worker thread."""
    seen = []

    def bounded(args, timeout_s):
        seen.append(timeout_s)
        return "ok"

    asyncio.run(provider(bounded=bounded).execute("bounded", {}, timeout_s=42.5))
    assert seen == [42.5]


def test_l05_a_function_taking_the_keyword_only_or_any_keyword_is_accepted():
    def keyword_only(args, *, timeout_s):
        return "a"

    def any_keyword(args, **kwargs):
        return str(kwargs["timeout_s"])

    p = provider(keyword_only=keyword_only, any_keyword=any_keyword)
    assert execute(p, "keyword_only") == ToolOutput("a")
    assert execute(p, "any_keyword") == ToolOutput("5.0")


def positional_only(args, timeout_s, /):
    return ""


@pytest.mark.parametrize(
    "function",
    [lambda args: "", positional_only, str],
    ids=["no_timeout_parameter", "positional_only", "no_readable_signature"],
)
def test_l05_a_plain_function_that_cannot_take_timeout_s_is_refused_naming_the_function(function):
    """L05: a function that could not bound itself is refused when it is registered, and the message names it."""
    with pytest.raises(ValueError, match="timeout_s") as caught:
        provider(refused=function)
    assert "'refused'" in str(caught.value)
    assert getattr(function, "__qualname__", "") in str(caught.value)


def test_l05_a_coroutine_function_needs_no_timeout_keyword_because_the_loop_can_cancel_it():
    async def on_the_loop(args):
        return "ok"

    assert execute(provider(on_the_loop=on_the_loop), "on_the_loop") == ToolOutput("ok")


# --- a thread cannot be killed (L15) ---------------------------------------------------------------------------------


def test_l15_a_cancelled_call_returns_at_once_and_the_provider_records_the_thread_that_may_still_run():
    """L15: cancelling a call does not wait for its thread and does not block the loop; the thread is recorded until it
    returns."""
    started, release = threading.Event(), threading.Event()

    def blocks(args, timeout_s):
        started.set()
        release.wait(timeout=10)
        return "late"

    p = provider(blocks=blocks)
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(p.execute("blocks", {}, timeout_s=5.0))
        while not started.is_set():
            await asyncio.sleep(0.001)
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        seen["cancel_took_s"] = time.monotonic() - began
        seen["while_blocked"] = p.threads_possibly_running()
        await asyncio.sleep(0.01)  # the loop goes on while the thread is still blocked
        release.set()  # the thread cannot be killed; let it return so the executor can shut down

    asyncio.run(scenario())
    assert seen["cancel_took_s"] < 1.0
    assert seen["while_blocked"] == ["blocks"]
    assert p.abandoned_total == 1
    assert p.threads_possibly_running() == []  # asyncio.run waited for the thread, which has returned


def test_l15_a_call_cancelled_before_its_thread_starts_never_runs_its_function():
    """L15: with the only worker busy, a second call waits in the queue; cancelled there, its function never runs and
    nothing is recorded as running."""
    started, release = threading.Event(), threading.Event()
    ran = []

    def blocks(args, timeout_s):
        started.set()
        release.wait(timeout=10)
        return "first"

    def second(args, timeout_s):
        ran.append("second")
        return "second"

    p = provider(blocks=blocks, second=second)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        first = asyncio.ensure_future(p.execute("blocks", {}, timeout_s=5.0))
        while not started.is_set():
            await asyncio.sleep(0.001)
        queued = asyncio.ensure_future(p.execute("second", {}, timeout_s=5.0))
        await asyncio.sleep(0.01)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        release.set()
        return await first

    assert asyncio.run(scenario()) == ToolOutput("first")
    assert ran == []
    assert p.abandoned_total == 0
    assert p.threads_possibly_running() == []


# --- waits on infrastructure are marked through the call context (L04) -----------------------------------------------


def started_timer():
    clock = FakeClock()
    timer = AgentTimer(100, clock=clock)
    timer.start()
    return clock, timer


def test_l04_a_coroutine_function_that_takes_ctx_marks_its_wait_and_the_harness_measures_it():
    """L04: the context reaches the function, and the agent's clock is paused for as long as the wait is open."""
    clock, timer = started_timer()

    async def store(args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            clock.advance(40)
        clock.advance(2)
        return "ok"

    p = provider(store=store)
    out = asyncio.run(p.execute("store", {}, timeout_s=5.0, ctx=WaitContext(timer)))
    assert out == ToolOutput("ok")
    assert (timer.infra_wait_s, timer.check("t").agent_s) == (40, 2)


def test_l04_a_plain_function_in_a_thread_marks_its_wait_with_a_plain_with():
    """L04: the thread gets a stand-in for the context; the wait is opened and closed on the event loop."""
    clock, timer = started_timer()

    def store(args, timeout_s, ctx):
        with ctx.infrastructure_wait("lease_queue"):
            clock.advance(40)
        clock.advance(2)
        return "ok"

    p = provider(store=store)
    out = asyncio.run(p.execute("store", {}, timeout_s=5.0, ctx=WaitContext(timer)))
    assert out == ToolOutput("ok")
    assert (timer.infra_wait_s, timer.check("t").agent_s) == (40, 2)


def test_a_function_that_does_not_take_ctx_is_not_handed_one():
    seen = []

    def plain(args, timeout_s):
        seen.append("called")
        return "ok"

    assert execute(provider(plain=plain), "plain") == ToolOutput("ok")
    assert seen == ["called"]


# --- an abandoned thread blocks new calls (L15) ----------------------------------------------------------------------


def test_l15_while_an_abandoned_thread_is_running_new_calls_are_refused_with_a_tool_error_that_says_why():
    """L15: a thread cannot be killed; until it returns, every new call gets an error naming the reason, and calls are
    accepted again afterwards."""
    started, release = threading.Event(), threading.Event()

    def blocks(args, timeout_s):
        started.set()
        release.wait(timeout=10)
        return "late"

    p = provider(blocks=blocks, other=lambda args, timeout_s: "fine")
    seen = {}

    async def scenario():
        task = asyncio.ensure_future(p.execute("blocks", {}, timeout_s=5.0))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        seen["abandoned"] = p.abandoned_threads()
        seen["refused"] = await p.execute("other", {}, timeout_s=5.0)
        seen["refused_same_tool"] = await p.execute("blocks", {}, timeout_s=5.0)
        release.set()
        while p.abandoned_threads():
            await asyncio.sleep(0.001)
        seen["after"] = await p.execute("other", {}, timeout_s=5.0)

    asyncio.run(scenario())
    assert seen["abandoned"] == 1
    for refused in (seen["refused"], seen["refused_same_tool"]):
        assert refused.is_error
        assert refused.error_class == "AbandonedThread"
        assert "blocks" in refused.text
        assert "cannot be killed" in refused.text
    assert seen["after"] == ToolOutput("fine")


def test_l15_the_provider_does_not_try_to_stop_a_thread():
    """L15: nothing is done to the thread; it runs to its own end (here it sets a flag after the call was cancelled)."""
    started, release = threading.Event(), threading.Event()
    finished = []

    def blocks(args, timeout_s):
        started.set()
        release.wait(timeout=10)
        finished.append("ran to its end")
        return "late"

    async def scenario():
        p = provider(blocks=blocks)
        task = asyncio.ensure_future(p.execute("blocks", {}, timeout_s=5.0))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()

    asyncio.run(scenario())
    assert finished == ["ran to its end"]


def test_l04_a_plain_function_that_names_a_kind_outside_the_vocabulary_fails_at_the_call():
    """L04: the wrong kind raises in the thread when the context is asked for, not when the wait is entered."""
    _, timer = started_timer()
    raised = []

    def store(args, timeout_s, ctx):
        try:
            ctx.infrastructure_wait("data_access")
        except ValueError as exc:
            raised.append(str(exc))
            raise
        return "ok"

    p = provider(store=store)
    with pytest.raises(ValueError, match="unknown infrastructure wait kind 'data_access'"):
        asyncio.run(p.execute("store", {}, timeout_s=5.0, ctx=WaitContext(timer)))
    assert "data_access" in raised[0]
    assert timer.infra_wait_s == 0
