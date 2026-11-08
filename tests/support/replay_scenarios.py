"""Stub runs that cover the endings and the tool outcomes, recorded through the real loop. No network."""

import asyncio
from collections.abc import Callable

from loop_support import ClockSleep, FakeClock, FakeProvider, ScriptedBackend, call, malformed, never, turn
from record_support import record_run

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelTurn, StubAdapter, ToolCall
from expcore.plugin import ToolOutput
from expcore.records.record import RunRecord


async def hang(_args: dict) -> str:
    """A tool that never answers."""
    await asyncio.Event().wait()
    return "unreachable"


REQUIRED_X = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
HANDLERS = {
    "echo": lambda args: f"echoed {args.get('x', '')}",
    "fail": lambda args: ToolOutput("no such thing", is_error=True, error_class="NotFound", source_truncated=False),
    "submit": lambda args: f"submitted {args.get('x', '')}",
    "rows": lambda args: ToolOutput("row 1\nrow 2", source_truncated=True),
}


def tools_provider(handlers: dict | None = None) -> FakeProvider:
    """A provider with an echo tool that needs its argument, an error tool, and a terminal submit tool."""
    return FakeProvider(handlers or HANDLERS, terminal=("submit",), schemas={"echo": REQUIRED_X})


class BadRequest(Exception):
    """What an SDK raises for a plain 400: an error the endpoint answered with, which is the attempt's own."""

    status_code = 400


class Thinking(ScriptedBackend):
    """A backend whose assistant messages carry the reasoning, and echo it back to the model in the next request."""

    async def step(self):
        reply = await super().step()
        self._messages[-1]["_reasoning"] = "private notes"
        self._messages[-1]["reasoning_content"] = "private notes"  # the field the request echoes back
        reply.reasoning = "private notes"
        return reply


class ReadsTime(ScriptedBackend):
    """A backend that reads the loop's time before each request, as a retry policy does."""

    time_left = None

    async def step(self):
        self.time_left()
        return await super().step()


class Retrying(ScriptedBackend):
    """A backend that backs off before its second request, marking the backoff as a wait on infrastructure."""

    timer = None
    clock = None

    async def step(self):
        if self._i == 1:
            async with self.timer.infrastructure_wait("model_retry"):
                self.clock.advance(7)
        return await super().step()


class Hangs(StubAdapter):
    """A backend whose request is never answered; the fake clock first moves `seconds`."""

    def __init__(self, script, clock=None, seconds=0):
        super().__init__(script)
        self.clock, self.seconds = clock, seconds

    async def step(self):
        if self.clock is not None:
            self.clock.advance(self.seconds)
        await asyncio.Event().wait()


class Abandoning(FakeProvider):
    """A provider that says a worker thread of a cancelled call may still be running."""

    def abandoned_threads(self) -> int:
        return 2


def _abandoned_worker() -> RunRecord:
    return record_run([[("echo", {})], "done"], {}, providers=[Abandoning(HANDLERS)])


def _tools() -> RunRecord:
    script = [
        [("echo", {"x": "a"}), ("fail", {})],
        [("echo", {"x": "z" * 500})],
        [("nosuch", {})],
        [("echo", {})],
        [("rows", {})],
        [("submit", {"x": "final"}), ("echo", {"x": "after the end"})],
    ]
    return record_run(script, {}, providers=[tools_provider()])


def _malformed() -> RunRecord:
    return record_run([turn(malformed("mal-1", "echo")), turn(call("call-1", "echo", {"x": "b"})), "done"], HANDLERS)


def _malformed_end() -> RunRecord:
    reply = turn(malformed("mal-1", "echo"), call("call-2", "echo", {"x": "b"}))
    return record_run([reply], HANDLERS, malformed_policy="end")


def _cached_turn() -> RunRecord:
    reply = ModelTurn(
        "",
        [ToolCall("call-1", "echo", {"x": "q"})],
        120,
        8,
        "tool_calls",
        0.75,
        reasoning="thinking",
        uncached_input_tokens=20,
        cache_creation_input_tokens=40,
        cache_read_input_tokens=60,
        served_model="m1-20260101",
        system_fingerprint="fp_1",
        retries=({"retry": 1, "kind": "rate_limit", "error": "RateLimitError", "status": 429, "wait_s": 5.0},),
    )
    return record_run([reply, turn(text="done", input_tokens=None, output_tokens=3)], HANDLERS)


def _time_cap() -> RunRecord:
    clock = FakeClock()

    def slow(_args):
        clock.advance(400)
        return "late"

    return record_run([[("echo", {})], "never"], {"echo": slow}, clock=clock)


def _tool_cut() -> RunRecord:
    clock = FakeClock()

    async def stalls(_args):
        clock.advance(400)
        await asyncio.Event().wait()

    return record_run([[("echo", {})], "never"], {"echo": stalls}, clock=clock, sleep=ClockSleep(clock))


def _model_cut() -> RunRecord:
    clock = FakeClock()
    return record_run([], HANDLERS, backend=Hangs([], clock, 400), clock=clock, sleep=ClockSleep(clock))


def _ceiling_cut() -> RunRecord:
    """A tool that waits on infrastructure past the ceiling is cut, and the attempt ends as infrastructure."""
    clock = FakeClock()

    async def queued(_args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            while True:
                clock.advance(100)
                await asyncio.sleep(0)

    return record_run([[("echo", {})], "never"], {"echo": queued}, clock=clock, sleep=ClockSleep(clock))


def _cleanup_timeout() -> RunRecord:
    """A cut call that ignores its cancellation: the cleanup deadline runs out and the call is given up on."""
    clock = FakeClock()

    async def stubborn(_args):
        clock.advance(400)
        cancels = 0
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                clock.advance(3)
                cancels += 1
                if cancels > 1:  # the first cancellation is ignored; the one that ends the event loop is not
                    raise

    async def deadline_ran_out(tasks, _deadline_s):
        return True

    return record_run(
        [[("echo", {})], "never"],
        {"echo": stubborn},
        clock=clock,
        sleep=ClockSleep(clock),
        cleanup_waiter=deadline_ran_out,
    )


def _cleanup_in_time() -> RunRecord:
    """A cut call that stops at once: the cleanup ends in time, and takes time of its own."""
    clock = FakeClock()

    async def stalls(_args):
        clock.advance(400)
        try:
            await asyncio.Event().wait()
        finally:
            clock.advance(3)

    return record_run([[("echo", {})], "never"], {"echo": stalls}, clock=clock, sleep=ClockSleep(clock))


def _wait_left_open() -> RunRecord:
    clock = FakeClock()

    async def forgets(_args, ctx):
        await ctx.infrastructure_wait("toolserver_lifecycle").__aenter__()
        clock.advance(9)
        return "ok"

    return record_run([[("echo", {})], "done"], {"echo": forgets}, clock=clock, sleep=ClockSleep(clock))


def _waits_by_kind() -> RunRecord:
    """Waits of three kinds in one call, two of them nested, with the loop's watch looking at each change."""
    clock = FakeClock()

    async def provisions(_args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            clock.advance(10)
            await asyncio.sleep(0)
            async with ctx.infrastructure_wait("sandbox_lifecycle"):
                clock.advance(20)
                await asyncio.sleep(0)
            clock.advance(5)
        clock.advance(2)  # the agent's own time
        async with ctx.infrastructure_wait("toolserver_lifecycle"):
            clock.advance(4)
        return "ok"

    return record_run(
        [[("echo", {})], [("echo", {})], "done"], {"echo": provisions}, clock=clock, sleep=ClockSleep(clock)
    )


def _zero_wait() -> RunRecord:
    """A wait that is declared and lasts no time on the clock."""

    async def instant(_args, ctx):
        async with ctx.infrastructure_wait("lease_queue"):
            pass
        return "ok"

    return record_run([[("echo", {})], "done"], {"echo": instant}, clock=FakeClock(), sleep=never)


def _model_wait() -> RunRecord:
    clock = FakeClock()
    backend = Retrying([[("echo", {"x": "a"})], "done"])
    backend.clock = clock
    return record_run([], HANDLERS, backend=backend, clock=clock, sleep=ClockSleep(clock), shared_timer=True)


def _tool_crash() -> RunRecord:
    def crash(_args):
        raise KeyError("vary-by-version")

    return record_run([[("echo", {}), ("echo", {})], "never"], {"echo": crash})


def _tool_infra() -> RunRecord:
    def down(_args):
        raise InfraError("knowledge", "tool server exited")

    return record_run([[("echo", {})], "never"], {"echo": down})


def _time_left() -> RunRecord:
    backend = ReadsTime([[("echo", {"x": "a"})], "done"])
    return record_run([], HANDLERS, backend=backend, shared_timer=True)


def _reasoning() -> RunRecord:
    return record_run([], HANDLERS, backend=Thinking([[("echo", {"x": "a"})], "done"]))


def _leaked() -> RunRecord:
    leak = '{"name": "echo", "arguments": {"x": "1"}}'
    return record_run([leak], HANDLERS)


SCENARIOS: dict[str, tuple[Callable[[], RunRecord], str]] = {
    "tools": (_tools, "finished"),
    "malformed": (_malformed, "finished"),
    "malformed_end": (_malformed_end, "malformed_end"),
    "cached_turn": (_cached_turn, "finished"),
    "turn_cap": (lambda: record_run([[("echo", {})], [("echo", {})], "x"], HANDLERS, max_tool_rounds=1), "turn_cap"),
    "token_cap": (lambda: record_run([[("echo", {})], "x"], HANDLERS, token_budget=4), "token_cap"),
    "output_cap": (lambda: record_run([{"text": "cut off", "stop": "length"}], HANDLERS), "output_cap"),
    "time_cap": (_time_cap, "time_cap"),
    "leaked_tool_call": (_leaked, "leaked_tool_call"),
    "infra_turn_three": (
        lambda: record_run(
            [[("echo", {"x": "1"})], [("echo", {"x": "2"})], InfraError("endpoint", "HTTP 503 after 8 retries")],
            HANDLERS,
        ),
        "infrastructure",
    ),
    "infra_treatment": (
        lambda: record_run(
            [[("echo", {"x": "1"})], InfraError("endpoint", "HTTP 503 after 8 retries")],
            HANDLERS,
            record_options={"treatment_kinds": ["endpoint"]},
        ),
        "infrastructure",
    ),
    "infra_tool": (_tool_infra, "infrastructure"),
    "tool_crash": (_tool_crash, "harness_defect"),
    "abandoned_worker": (_abandoned_worker, "finished"),
    "tool_cut": (_tool_cut, "time_cap"),
    "ceiling_cut": (_ceiling_cut, "infrastructure"),
    "cleanup_in_time": (_cleanup_in_time, "time_cap"),
    "cleanup_timeout": (_cleanup_timeout, "time_cap"),
    "wait_left_open": (_wait_left_open, "finished"),
    "zero_wait": (_zero_wait, "finished"),
    "waits_by_kind": (_waits_by_kind, "finished"),
    "model_wait": (_model_wait, "finished"),
    "model_error": (lambda: record_run([BadRequest("rejected")], HANDLERS), "error"),
    "backend_crash": (lambda: record_run([ValueError("boom")], HANDLERS), "harness_defect"),
    "context_overflow": (lambda: record_run([ContextOverflow(400, "context_length")], HANDLERS), "error"),
    "abandoned": (lambda: record_run([RequestAbandoned("read timeout")], HANDLERS), "time_cap"),
    "model_cut": (_model_cut, "time_cap"),
    "time_left_readings": (_time_left, "finished"),
    "reasoning": (_reasoning, "finished"),
}
