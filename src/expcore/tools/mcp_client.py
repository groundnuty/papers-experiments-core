# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/knowledge.py, blob 1b3794632c03cda80f9824e4fbf82c98a6632525
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Knowledge-plane tools (#6): an MCP stdio client, as in the dataset_77 base (panel.py
ForgeOpenAILeg: spawn -> initialize -> list_tools -> call_tool), but reusable across turns.

The server is the published groundnuty/onedata-mcp; its command comes from config. Tests drive
a fake MCP server over the real stdio protocol.
"""
from __future__ import annotations

import contextlib

import anyio
import json
from typing import Any, Iterable

from .errors import InfraError

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR
from mcp.client.stdio import stdio_client


def _attr(o: Any, snake: str, camel: str, default: Any = None) -> Any:
    """mcp 2.x uses snake_case fields, mcp 1.x camelCase; the lock file has 2.x, the dataset_77 base 1.x."""
    v = getattr(o, snake, None)
    return v if v is not None else getattr(o, camel, default)


def mcp_result_to_text(result: Any) -> str:
    """Serialise an mcp CallToolResult to text (same rule as the dataset_77 base)."""
    structured = _attr(result, "structured_content", "structuredContent")
    if structured is not None:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(structured)
    parts = [b.text for b in (getattr(result, "content", None) or []) if isinstance(getattr(b, "text", None), str)]
    return "".join(parts) if parts else repr(getattr(result, "content", None))


class KnowledgePlane:
    """Async context manager around one MCP server session (one per run)."""

    def __init__(self, command: str, args: Iterable[str] = (), env: dict | None = None,
                 allowed_tools: Iterable[str] = ()):
        self._params = StdioServerParameters(command=command, args=list(args), env=env)
        self._allowed = set(allowed_tools)
        self._stack = contextlib.AsyncExitStack()
        self._session: ClientSession | None = None
        self.tool_defs: list[dict] = []

    async def __aenter__(self) -> "KnowledgePlane":
        try:
            read, write = await self._stack.enter_async_context(stdio_client(self._params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            listed = await self._session.list_tools()
            by_name = {t.name: t for t in listed.tools}
            missing = self._allowed - set(by_name)
            if missing:
                raise RuntimeError(f"knowledge server does not expose tools: {sorted(missing)}")
            self.tool_defs = [
                {"name": n, "description": t.description or "",
                 "input_schema": _attr(t, "input_schema", "inputSchema") or {"type": "object", "properties": {}}}
                for n, t in sorted(by_name.items()) if not self._allowed or n in self._allowed
            ]
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.tool_defs}

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (text, is_error)."""
        assert self._session is not None
        try:
            res = await self._session.call_tool(name, args)
            return mcp_result_to_text(res), bool(_attr(res, "is_error", "isError", False))
        except (ConnectionError, EOFError, BrokenPipeError, anyio.ClosedResourceError,
                anyio.BrokenResourceError, anyio.EndOfStream) as e:
            raise InfraError("knowledge", f"{type(e).__name__}: {e}"[:500]) from e
        except MCPError as e:
            # Server-side failures only are infra: the connection closed under us (the server crashed or
            # is unreachable) or the server reports an internal error. Every other protocol error (invalid
            # params, unknown tool, ...) answers the agent's own call and is a normal tool round.
            if e.code in (CONNECTION_CLOSED, INTERNAL_ERROR):
                raise InfraError("knowledge", f"MCPError {e.code}: {e.message}"[:500]) from e
            return json.dumps({"error": f"MCPError {e.code}: {e.message}"}), True
        except Exception as e:  # noqa: BLE001 -- a tool-level failure is the model's to see
            return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
