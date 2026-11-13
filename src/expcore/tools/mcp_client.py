# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/knowledge.py, blob 1b3794632c03cda80f9824e4fbf82c98a6632525
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""An MCP stdio client that serves a server's tools to the agent loop, as a tool provider.

One server session per run: spawn, initialize, list the tools, then call them across turns. Five rules:

- The start is bounded by a timeout. A server that does not answer in time is broken (L14).
- A call is bounded by its `timeout_s`, inside the loop and outside it. A server that is up and does not answer in time
  is infrastructure of kind `knowledge`, naming the tool and the bound (L16).
- The server's tools must equal the frozen manifest of the condition, and the allow-list the caller gave the
  server's launcher must name tools the server has. An extra or a missing tool fails the start. The client never
  filters the list itself: the server enforces the allow-list (L13).
- A closed transport or an internal server error is an infrastructure failure of kind `knowledge` (L12).
- Any other MCP error, and a result the server marks as an error, is a tool result for the model (L12).
"""

import asyncio
import contextlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR

from expcore.errors import InfraError
from expcore.plugin import NO_WAITS, CallContext, ToolOutput, ToolSpec
from expcore.tools.manifest import FrozenManifest, ManifestError, canonical, list_all_tools, mismatches

DEFAULT_START_TIMEOUT_S = 120.0
DETAIL_LIMIT = 500
# The transport errors of the stream library under the SDK, matched by class name so the core does not import it.
_TRANSPORT_ERROR_NAMES = frozenset({"ClosedResourceError", "BrokenResourceError", "EndOfStream"})


def mcp_result_to_text(result: Any) -> str:
    """Serialise an MCP tool result to text: the structured content as JSON, else the text blocks joined."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(structured)
    parts = [b.text for b in (getattr(result, "content", None) or []) if isinstance(getattr(b, "text", None), str)]
    return "".join(parts) if parts else repr(getattr(result, "content", None))


def _is_transport_error(exc: BaseException) -> bool:
    """Tell whether an exception means the connection to the server is gone."""
    if isinstance(exc, ConnectionError | EOFError | BrokenPipeError):
        return True
    return any(cls.__name__ in _TRANSPORT_ERROR_NAMES for cls in type(exc).__mro__)


def _detail(exc: BaseException) -> str:
    """A short line for an infrastructure detail: the class and message, never longer than the limit."""
    return f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]


class CallTimedOut(InfraError):
    """A tool call outlasted its `timeout_s` without the server answering it (L16): infrastructure of kind `knowledge`.

    It is its own class so that a caller that bounds a call itself, as the day-0 outage check does, can tell the bound's
    expiry from a server that closed or failed. `tool` and `bound_s` are what the detail says.
    """

    def __init__(self, tool: str, bound_s: float):
        super().__init__("knowledge", f"the tool server did not answer a call to {tool!r} within {bound_s:g} s")
        self.tool = tool
        self.bound_s = bound_s


class McpToolProvider:
    """A tool provider over one MCP server session. Use it as an async context manager around the run."""

    def __init__(
        self,
        command: str,
        args: Iterable[str] = (),
        *,
        frozen: FrozenManifest | Path,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        allowed_tools: Iterable[str] = (),
        start_timeout_s: float = DEFAULT_START_TIMEOUT_S,
    ):
        if start_timeout_s <= 0:
            raise ValueError(f"the start timeout must be positive, got {start_timeout_s!r}")
        self._params = StdioServerParameters(command=command, args=list(args), env=env, cwd=cwd)
        self._frozen = frozen if isinstance(frozen, FrozenManifest) else FrozenManifest.load(frozen)
        self._allowed = tuple(allowed_tools)
        self._start_timeout_s = start_timeout_s
        self._stack = contextlib.AsyncExitStack()
        self._session: ClientSession | None = None
        self._specs: list[ToolSpec] = []
        self.manifest_sha256: str | None = None  # the hash of what the server showed, for the record

    async def __aenter__(self) -> "McpToolProvider":
        """Start the server and check its tools; on any failure close everything, the child process included."""
        try:
            tools = await self._start()
            self._check(tools)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        """End the session and stop the server."""
        await self._stack.aclose()

    async def _start(self) -> list[dict]:
        """Spawn the server, initialize the session and read the tool list, all within the start timeout (L14)."""
        try:
            async with asyncio.timeout(self._start_timeout_s):
                read, write = await self._stack.enter_async_context(stdio_client(self._params))
                self._session = await self._stack.enter_async_context(ClientSession(read, write))
                await self._session.initialize()
                return canonical(await list_all_tools(self._session))
        except TimeoutError:
            raise InfraError("knowledge", f"the tool server did not start within {self._start_timeout_s:g} s") from None
        except (OSError, MCPError) as exc:
            raise InfraError("knowledge", f"the tool server could not start: {_detail(exc)}") from exc
        except Exception as exc:
            if _is_transport_error(exc):
                raise InfraError("knowledge", f"the tool server closed while starting: {_detail(exc)}") from exc
            raise

    def _check(self, live: list[dict]) -> None:
        """Compare the server's tools with the frozen manifest and the allow-list; fail the start on any difference."""
        problems = mismatches(live, self._frozen, self._allowed)
        if problems:
            raise ManifestError(problems)
        self.manifest_sha256 = self._frozen.sha256
        self._specs = [ToolSpec(t["name"], t["description"], t["input_schema"] or {"type": "object"}) for t in live]

    def tools(self) -> Sequence[ToolSpec]:
        """Return the server's tools, which equal the frozen manifest."""
        return list(self._specs)

    async def execute(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext = NO_WAITS
    ) -> ToolOutput:
        """Call a tool. The call ends within `timeout_s`, and that bound is its only limit (L05, L16).

        Inside the loop `timeout_s` is the agent's time left, so the bound is the loop's own and never longer; the loop
        cuts the call at the same moment, and the run ends as the loop's cut or as this expiry, whichever it sees
        first. Outside the loop (a pre-run check, a health probe) the bound is the only one there is: a server that is
        up and never answers raises `CallTimedOut`, which is `InfraError` of kind `knowledge` and names the tool and the
        bound. The call is abandoned and the session stays usable.

        The round trip to the server is the tool's own time and is charged to the agent: the client marks no wait on
        infrastructure, since the core cannot tell which part of a server's time a study counts as one.
        """
        if self._session is None:
            raise RuntimeError("the tool provider is not started; use it as an async context manager")
        if not timeout_s > 0:  # also refuses NaN
            raise ValueError(f"the call's timeout must be positive, got {timeout_s!r}")
        try:
            async with asyncio.timeout(timeout_s) as bound:
                result = await self._session.call_tool(name, arguments)
        except MCPError as exc:
            # Server-side failures only are infrastructure: the connection closed under us (the server crashed or is
            # unreachable) or the server reports an internal error. Every other protocol error (invalid params,
            # unknown tool, ...) answers the agent's own call and is a normal tool round (L12).
            if exc.code in (CONNECTION_CLOSED, INTERNAL_ERROR):
                raise InfraError("knowledge", f"MCPError {exc.code}: {exc.message}"[:DETAIL_LIMIT]) from exc
            return ToolOutput(f"{exc.code}: {exc.message}", is_error=True, error_class="MCPError")
        except Exception as exc:
            if (
                isinstance(exc, TimeoutError) and bound.expired()
            ):  # the bound's own expiry, not a timeout the SDK raised
                raise CallTimedOut(name, timeout_s) from None
            if _is_transport_error(exc):
                raise InfraError("knowledge", _detail(exc)) from exc
            raise
        text = mcp_result_to_text(result)
        if getattr(result, "is_error", False):
            return ToolOutput(text, is_error=True, error_class="ToolError")
        return ToolOutput(text)
