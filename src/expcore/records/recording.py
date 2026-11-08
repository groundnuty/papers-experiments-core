# Provenance: new
"""A wrapper that records a model backend while the loop drives it, without changing the backend or the loop.

The backend owns the conversation, so what the record needs per turn (the native message, the messages that carried the
tool results back, the settings of the request, the digest of the conversation the request carried) is read from the
backend's own public methods around each call:

- before `step`, the digest of the conversation the request will carry. Anything echoed back to the model, such as a
  reasoning field, is in it, so tampering with a recorded reasoning shows as a different digest on replay (R06);
- after `step`, the settings the request actually carried (`info()["settings_sent"]`, B15) and the last message of the
  conversation, the native assistant message;
- after `add_tool_results`, the messages the backend appended for the results.

A request that raises is recorded too (R09, R03): its kind and stable fields, never the exception text of a library,
which differs between interpreter versions (R07).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import ModelBackend, ModelTurn, ToolResult
from expcore.records.digest import digest, plain
from expcore.records.schema import RecordError


@dataclass
class Captured:
    """What was seen around one model reply."""

    turn: ModelTurn
    native: dict[str, Any]
    settings: dict[str, Any]
    tool_messages: list[dict[str, Any]] = field(default_factory=list)


def failure_of(exc: BaseException | None, settings: dict[str, Any]) -> dict[str, Any]:
    """Describe a request that did not return a reply, in the stable fields of the record (R07).

    `exc` is None for a request the loop gave up on while it was in flight (cancelled): kind `cut`. A library's own
    exception is recorded by its class name only; the core's exceptions keep their fixed fields.
    """
    entry: dict[str, Any] = {
        "kind": "cut",
        "infra_kind": None,
        "detail": None,
        "status": None,
        "pattern": None,
        "error_type": None,
        "settings_sha256": digest(settings),
        "request_sha256": "",  # filled in by `seal`, over the redacted conversation
    }
    if isinstance(exc, InfraError):
        entry.update(kind="infra", infra_kind=exc.kind, detail=exc.detail)
    elif isinstance(exc, ContextOverflow):
        entry.update(kind="context_overflow", status=exc.status, pattern=exc.pattern)
    elif isinstance(exc, RequestAbandoned):
        entry.update(kind="abandoned")
    elif exc is not None:
        entry.update(kind="error", error_type=type(exc).__name__)
    return entry


class RecordingBackend:
    """A model backend that delegates to another and keeps what the record needs. Use it in place of the backend."""

    def __init__(self, inner: ModelBackend):
        self.inner = inner
        self.captured: list[Captured] = []
        self.failure: dict[str, Any] | None = None
        self.failure_settings: dict[str, Any] | None = None
        self._known = 0  # messages of the conversation already accounted for

    @property
    def output_limit(self) -> int | None:
        """The inner backend's output limit."""
        return self.inner.output_limit

    @property
    def context_tokens(self) -> int | None:
        """The inner backend's context window."""
        return self.inner.context_tokens

    def info(self) -> dict[str, Any]:
        """Return the inner backend's description of the route."""
        return self.inner.info()

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start the conversation on the inner backend."""
        self.inner.begin(system, user, tools)
        self._known = len(self.inner.transcript())

    def _settings(self) -> dict[str, Any]:
        """Return the settings of the latest request, as plain values."""
        return plain(self.inner.info().get("settings_sent", {}))

    async def step(self) -> ModelTurn:
        """Ask the inner backend for a reply and keep what surrounds it; a request that raises is the failure."""
        try:
            turn = await self.inner.step()
        except asyncio.CancelledError:
            self._fail(None)
            raise
        except Exception as exc:
            self._fail(exc)
            raise
        conversation = self.inner.transcript()
        native = conversation[-1] if conversation else {}
        if native.get("role") != "assistant":
            raise RecordError("the backend's conversation does not end with an assistant message after a reply")
        self.captured.append(Captured(turn, native, self._settings()))
        self._known = len(conversation)
        return turn

    def _fail(self, exc: BaseException | None) -> None:
        """Keep the request that raised, with the settings it carried."""
        settings = self._settings()
        self.failure = failure_of(exc, settings)
        self.failure_settings = settings

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Hand the results to the inner backend and keep the messages it appended for them."""
        self.inner.add_tool_results(results)
        conversation = self.inner.transcript()
        if self.captured:
            self.captured[-1].tool_messages = conversation[self._known :]
        self._known = len(conversation)

    def transcript(self) -> list[dict]:
        """Return the inner backend's conversation."""
        return self.inner.transcript()

    async def probe(self) -> None:
        """Probe through the inner backend."""
        await self.inner.probe()

    async def aclose(self) -> None:
        """Close the inner backend."""
        await self.inner.aclose()
