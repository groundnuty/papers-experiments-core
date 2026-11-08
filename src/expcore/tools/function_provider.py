# Provenance: new
"""A tool provider built from plain functions, with blocking work moved off the event loop (L15).

A study or platform that has a few Python callables for tools wraps them here instead of writing a provider class.
A function that blocks (a sandbox call, a lock, a file read) must not run on the event loop: it would stop every other
run's agent loop and their deadlines with it. So a plain function runs in a worker thread via `asyncio.to_thread`,
and only a coroutine function runs on the loop itself.

A running thread cannot be killed. When the loop gives up on a call (the run's time ran out) the call is cancelled,
but the function goes on in its thread until it returns, and the default executor's shutdown at the end of
`asyncio.run` waits for it. So a plain function is handed the time the run has left, as the keyword `timeout_s`, and
is expected to bound itself with it (L05); registering a plain function that cannot take the keyword is refused. A
call cancelled while its thread was running is recorded on the provider (`threads_possibly_running`,
`abandoned_total`), and the provider never waits for the thread: the cancelled call returns at once.
"""

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from expcore.plugin import ToolOutput, ToolSpec

# A coroutine function takes the call's arguments. Any other function takes them and the keyword `timeout_s`.
ToolFunction = Callable[..., "str | ToolOutput | Awaitable[str | ToolOutput]"]
_TIMEOUT_KEYWORD = "timeout_s"
_KEYWORD_KINDS = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _label(function: ToolFunction) -> str:
    """Name a function for an error message."""
    return getattr(function, "__qualname__", None) or repr(function)


def _require_timeout_keyword(tool: str, function: ToolFunction) -> None:
    """Refuse a plain function that cannot be handed `timeout_s`: it could not bound itself, and it cannot be killed."""
    rule = (
        f"a function that runs in a worker thread must accept a {_TIMEOUT_KEYWORD} keyword, the time the run has left"
    )
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        raise ValueError(f"tool {tool!r}: cannot read the signature of {_label(function)}; {rule}") from None
    named = parameters.get(_TIMEOUT_KEYWORD)
    if (named is not None and named.kind in _KEYWORD_KINDS) or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    ):
        return
    raise ValueError(f"tool {tool!r}: {_label(function)} does not accept {_TIMEOUT_KEYWORD}; {rule}")


@dataclass
class _Call:
    """One call that went to a worker thread. `state` moves queued -> running -> done, or queued -> cancelled; it is
    changed only under the lock, so a call cancelled before its thread starts never runs its function."""

    name: str
    state: str = "queued"
    lock: threading.Lock = field(default_factory=threading.Lock)


class FunctionProvider:
    """Offers the given (spec, function) pairs as tools.

    A coroutine function takes the call's arguments. Any other function takes the arguments and the keyword
    `timeout_s` (the seconds the run has left), and runs in a worker thread. Either returns the result text or a
    `ToolOutput`.
    """

    def __init__(self, tools: Sequence[tuple[ToolSpec, ToolFunction]]):
        self._functions: dict[str, ToolFunction] = {}
        self._specs: list[ToolSpec] = []
        for spec, function in tools:
            if spec.name in self._functions:
                raise ValueError(f"two functions are registered for the tool {spec.name!r}")
            if not inspect.iscoroutinefunction(function):
                _require_timeout_keyword(spec.name, function)
            self._functions[spec.name] = function
            self._specs.append(spec)
        self._abandoned: list[_Call] = []
        self.abandoned_total = 0  # calls cancelled while their function was running in a thread

    def tools(self) -> Sequence[ToolSpec]:
        """Return the specs, in registration order."""
        return list(self._specs)

    def threads_possibly_running(self) -> list[str]:
        """Return the tool names of calls that were cancelled while their function ran and that have not returned.

        The thread of each may still be running, holding whatever the function holds. A name is listed once per call.
        """
        self._abandoned = [call for call in self._abandoned if call.state == "running"]
        return [call.name for call in self._abandoned]

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
        """Run the function: a coroutine function on the loop, any other function in a worker thread (L15).

        A cancelled call returns at once and does not wait for its thread (the thread cannot be killed); see
        `threads_possibly_running`.
        """
        function = self._functions[name]
        if inspect.iscoroutinefunction(function):
            result = await function(arguments)
        else:
            result = await self._in_thread(name, function, arguments, timeout_s)
            if inspect.isawaitable(result):  # a plain function that returned a coroutine: the loop is the place for it
                result = await result
        return result if isinstance(result, ToolOutput) else ToolOutput(str(result))

    async def _in_thread(self, name: str, function: ToolFunction, arguments: dict[str, Any], timeout_s: float) -> Any:
        """Run a plain function in a worker thread and wait for it, or for this task's cancellation."""
        call = _Call(name)

        def work() -> Any:
            with call.lock:
                if call.state == "cancelled":  # the caller gave up before the thread started: do not run
                    return None
                call.state = "running"
            try:
                return function(arguments, timeout_s=timeout_s)
            finally:
                with call.lock:
                    call.state = "done"

        try:
            return await asyncio.to_thread(work)
        except asyncio.CancelledError:
            with call.lock:
                if call.state == "running":
                    self._abandoned.append(call)
                    self.abandoned_total += 1
                elif call.state == "queued":
                    call.state = "cancelled"
            raise
