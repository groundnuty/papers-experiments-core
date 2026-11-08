"""Stub runs that cover the endings and the tool outcomes, recorded through the real loop. No network."""

import asyncio
from collections.abc import Callable

from loop_support import FakeClock, FakeProvider, ScriptedBackend, call, malformed, turn
from record_support import record_run

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelTurn, StubAdapter, ToolCall
from expcore.plugin import ToolOutput
from expcore.records.record import RunRecord


async def at_once(_seconds: float) -> None:
    """Stands in for asyncio.sleep: every bound passes as soon as the loop waits on it."""
    await asyncio.sleep(0)


async def hang(_args: dict) -> str:
    """A tool that never answers."""
    await asyncio.Event().wait()
    return "unreachable"


REQUIRED_X = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
HANDLERS = {
    "echo": lambda args: f"echoed {args.get('x', '')}",
    "fail": lambda args: ToolOutput("no such thing", is_error=True, error_class="NotFound", infra_wait_s=0.25),
    "submit": lambda args: f"submitted {args.get('x', '')}",
}


def tools_provider(handlers: dict | None = None) -> FakeProvider:
    """A provider with an echo tool that needs its argument, an error tool, and a terminal submit tool."""
    return FakeProvider(handlers or HANDLERS, terminal=("submit",), schemas={"echo": REQUIRED_X})


class Thinking(ScriptedBackend):
    """A backend whose assistant messages carry the reasoning, and echo it back in the conversation."""

    async def step(self):
        reply = await super().step()
        self._messages[-1]["_reasoning"] = "private notes"
        reply.reasoning = "private notes"
        return reply


class ReadsTime(ScriptedBackend):
    """A backend that reads the loop's time before each request, as a retry policy does."""

    time_left = None

    async def step(self):
        self.time_left()
        return await super().step()


class Hangs(StubAdapter):
    """A backend whose request is never answered."""

    async def step(self):
        await asyncio.Event().wait()


def _tools() -> RunRecord:
    script = [
        [("echo", {"x": "a"}), ("fail", {})],
        [("echo", {"x": "z" * 500})],
        [("nosuch", {})],
        [("echo", {})],
        [("submit", {"x": "final"}), ("echo", {"x": "after the end"})],
    ]
    return record_run(script, {}, providers=[tools_provider()])


def _malformed() -> RunRecord:
    return record_run([turn(malformed("mal-1", "echo")), turn(call("call-1", "echo", {"x": "b"})), "done"], HANDLERS)


def _malformed_end() -> RunRecord:
    return record_run(
        [turn(malformed("mal-1", "echo"), call("call-2", "echo", {"x": "b"}))], HANDLERS, malformed_policy="end"
    )


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
    return record_run([[("echo", {})], "never"], {"echo": hang}, sleep=at_once)


def _tool_crash() -> RunRecord:
    def crash(_args):
        raise KeyError("vary-by-version")

    return record_run([[("echo", {}), ("echo", {})], "never"], {"echo": crash})


def _tool_infra() -> RunRecord:
    def down(_args):
        raise InfraError("knowledge", "tool server exited")

    return record_run([[("echo", {})], "never"], {"echo": down})


def _time_left() -> RunRecord:
    return record_run(
        [[("echo", {"x": "a"})], "done"],
        HANDLERS,
        backend=ReadsTime([[("echo", {"x": "a"})], "done"]),
        shared_timer=True,
    )


def _reasoning() -> RunRecord:
    return record_run([[("echo", {"x": "a"})], "done"], HANDLERS, backend=Thinking([[("echo", {"x": "a"})], "done"]))


SCENARIOS: dict[str, tuple[Callable[[], RunRecord], str]] = {
    "tools": (_tools, "finished"),
    "malformed": (_malformed, "finished"),
    "malformed_end": (_malformed_end, "malformed_end"),
    "cached_turn": (_cached_turn, "finished"),
    "turn_cap": (lambda: record_run([[("echo", {})], [("echo", {})], "x"], HANDLERS, max_tool_rounds=1), "turn_cap"),
    "token_cap": (lambda: record_run([[("echo", {})], "x"], HANDLERS, token_budget=4), "token_cap"),
    "output_cap": (lambda: record_run([{"text": "cut off", "stop": "length"}], HANDLERS), "output_cap"),
    "time_cap": (_time_cap, "time_cap"),
    "infra_turn_three": (
        lambda: record_run(
            [[("echo", {"x": "1"})], [("echo", {"x": "2"})], InfraError("endpoint", "HTTP 503 after 8 retries")],
            HANDLERS,
        ),
        "infrastructure",
    ),
    "infra_tool": (_tool_infra, "infrastructure"),
    "tool_crash": (_tool_crash, "harness_defect"),
    "tool_cut": (_tool_cut, "time_cap"),
    "model_error": (lambda: record_run([ValueError("boom")], HANDLERS), "error"),
    "context_overflow": (lambda: record_run([ContextOverflow(400, "context_length")], HANDLERS), "error"),
    "abandoned": (lambda: record_run([RequestAbandoned("read timeout")], HANDLERS), "time_cap"),
    "model_cut": (lambda: record_run([], HANDLERS, backend=Hangs([]), sleep=at_once), "time_cap"),
    "time_left_readings": (_time_left, "finished"),
    "reasoning": (_reasoning, "finished"),
}
