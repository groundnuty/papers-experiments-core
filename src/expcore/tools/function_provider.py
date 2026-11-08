# Provenance: new
"""A tool provider built from plain functions, with blocking work moved off the event loop (L15).

A study or platform that has a few Python callables for tools wraps them here instead of writing a provider class.
A function that blocks (a sandbox call, a lock, a file read) must not run on the event loop: it would stop every other
run's agent loop and their deadlines with it. So a plain function runs in a worker thread via `asyncio.to_thread`,
and only a coroutine function runs on the loop itself.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from expcore.plugin import ToolOutput, ToolSpec

ToolFunction = Callable[[dict[str, Any]], "str | ToolOutput | Awaitable[str | ToolOutput]"]


class FunctionProvider:
    """Offers the given (spec, function) pairs as tools. A function takes the call's arguments and returns the
    result text or a `ToolOutput`."""

    def __init__(self, tools: Sequence[tuple[ToolSpec, ToolFunction]]):
        self._functions: dict[str, ToolFunction] = {}
        self._specs: list[ToolSpec] = []
        for spec, function in tools:
            if spec.name in self._functions:
                raise ValueError(f"two functions are registered for the tool {spec.name!r}")
            self._functions[spec.name] = function
            self._specs.append(spec)

    def tools(self) -> Sequence[ToolSpec]:
        """Return the specs, in registration order."""
        return list(self._specs)

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
        """Run the function: a coroutine function on the loop, any other function in a worker thread (L15)."""
        function = self._functions[name]
        if inspect.iscoroutinefunction(function):
            result = await function(arguments)
        else:
            result = await asyncio.to_thread(function, arguments)
            if inspect.isawaitable(result):  # a plain function that returned a coroutine: the loop is the place for it
                result = await result
        return result if isinstance(result, ToolOutput) else ToolOutput(str(result))
