"""The function provider: results, errors, and that blocking functions never run on the event loop (L15)."""

import asyncio
import threading
import time

import pytest

from expcore.plugin import ToolOutput, ToolProvider, ToolSpec
from expcore.tools.function_provider import FunctionProvider


def provider(**functions):
    return FunctionProvider([(ToolSpec(name, f"The {name} tool."), function) for name, function in functions.items()])


def execute(p, name, arguments=None):
    return asyncio.run(p.execute(name, arguments or {}, timeout_s=5.0))


def test_a_function_result_becomes_a_tool_output():
    p = provider(shout=lambda args: args["text"].upper())
    assert isinstance(p, ToolProvider)
    assert [s.name for s in p.tools()] == ["shout"]
    assert execute(p, "shout", {"text": "hi"}) == ToolOutput("HI")


def test_a_function_may_return_a_full_tool_output():
    failing = ToolOutput("no", is_error=True, error_class="Nope", infra_wait_s=2.0)
    assert execute(provider(f=lambda args: failing), "f") == failing


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
    def boom(args):
        raise KeyError("missing")

    with pytest.raises(KeyError):
        execute(provider(boom=boom), "boom")


def test_two_functions_for_one_tool_are_refused():
    spec = ToolSpec("t", "d")
    with pytest.raises(ValueError, match="two functions"):
        FunctionProvider([(spec, str), (spec, str)])


def test_l15_blocking_waits_never_run_on_the_event_loop():
    """L15: two concurrent calls that each block until the other has started overlap in wall time.

    On the event loop the first would block it and the second could never start, so the barrier would time out.
    """
    barrier = threading.Barrier(2, timeout=5.0)

    def wait_for_the_other(args):
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
