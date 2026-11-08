"""Helpers for the loop tests: a controllable clock and sleep, fake tool providers, a scripted model. No network."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from expcore.models.adapters import ModelTurn, StubAdapter, ToolCall
from expcore.plugin import ToolOutput, ToolSpec


class FakeClock:
    """A clock that moves only when a test moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def never(seconds: float) -> None:
    """Stands in for asyncio.sleep: the bound never passes by itself, so only a test can end a call."""
    await asyncio.Event().wait()


async def at_once(seconds: float) -> None:
    """Stands in for asyncio.sleep: every bound passes as soon as the loop waits on it."""
    await asyncio.sleep(0)


class FakeProvider:
    """A tool provider with scripted handlers. Records every call with the timeout it was given."""

    def __init__(self, handlers: dict[str, Callable[..., Any]], *, terminal: tuple[str, ...] = (), schemas=None):
        self.handlers = handlers
        self.terminal = terminal
        self.schemas = schemas or {}
        self.calls: list[tuple[str, dict, float]] = []

    def tools(self):
        return [
            ToolSpec(
                name,
                f"The {name} tool.",
                self.schemas.get(name, {"type": "object", "properties": {"x": {"type": "string"}}}),
                terminal=name in self.terminal,
            )
            for name in self.handlers
        ]

    async def execute(self, name, arguments, *, timeout_s):
        self.calls.append((name, arguments, timeout_s))
        result = self.handlers[name](arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, ToolOutput) else ToolOutput(str(result))


class ScriptedBackend(StubAdapter):
    """The 3b stub, extended so a script item can be a ready-made ModelTurn (for malformed arguments, usage gaps)."""

    async def step(self):
        item = self._script[self._i] if self._i < len(self._script) else "done"
        if not isinstance(item, ModelTurn):
            return await super().step()
        self._i += 1
        entry: dict[str, Any] = {"role": "assistant", "content": item.text or None}
        if item.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        # the adapters never resend malformed arguments: the history holds valid JSON with the raw text
                        "arguments": json.dumps({"_raw": c.raw_arguments} if c.parse_error else c.arguments),
                    },
                }
                for c in item.tool_calls
            ]
        self._messages.append(entry)
        return item


def turn(*calls: ToolCall, text: str = "", input_tokens: int | None = 10, output_tokens: int | None = 5, stop=None):
    """Build a model reply by hand."""
    return ModelTurn(text, list(calls), input_tokens, output_tokens, stop or ("tool_calls" if calls else "stop"), 0.0)


def call(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(call_id, name, arguments or {})


def malformed(call_id: str, name: str, raw: str = '{"a":') -> ToolCall:
    return ToolCall(call_id, name, {}, "arguments are not valid JSON (Expecting value)", raw)
