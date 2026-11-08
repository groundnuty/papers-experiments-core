# Provenance: new
"""Plugin interfaces. All are `typing.Protocol`s, so a test double needs no inheritance.

This module holds the tool interface the agent loop is built on. The core ships one provider (the MCP stdio
client) and a wrapper for plain functions; a platform or a study supplies any other, including a code-execution
tool: the core has no built-in tool of its own.
"""

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# The characters both vendor APIs accept in a function name.
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent sees: a name, a description and a JSON schema for its arguments.

    A `terminal` tool ends the attempt when a call to it succeeds, as a submission does; the calls that follow it
    in the same reply are answered as skipped (L09).
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    terminal: bool = False

    def __post_init__(self) -> None:
        """Refuse a spec the vendor APIs would reject, so the fault shows when the tool box is built."""
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError(f"tool name {self.name!r} must be 1 to 64 letters, digits, underscores or dashes")
        if not isinstance(self.input_schema, dict) or self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: input_schema must be a JSON schema of type object")


@dataclass(frozen=True)
class ToolOutput:
    """What a provider returns for one call.

    `text` is the full result, or for an error the message. An error names its `error_class`, which the model
    sees first. Time spent waiting on infrastructure is not reported here: the provider wraps each such wait in
    `ctx.infrastructure_wait(kind)` while it happens, and the harness measures it (L04).

    `source_truncated` is for the data source's own limit (L07): True when the source hit its row or size limit, so
    the result may be cut by the source; False when the provider checked and it was not; None when the provider says
    nothing. The record keeps it apart from the flag for the harness's output cap, which the loop sets on what it cut.
    """

    text: str
    is_error: bool = False
    error_class: str | None = None
    source_truncated: bool | None = None

    def __post_init__(self) -> None:
        """Refuse an error class on a result that is not an error, and a source flag that is not a boolean."""
        if self.error_class is not None and not self.is_error:
            raise ValueError("error_class is only for a result with is_error set")
        if self.source_truncated is not None and not isinstance(self.source_truncated, bool):
            raise ValueError(f"source_truncated must be True, False or None, not {self.source_truncated!r}")


# The kinds of wait on infrastructure, a closed vocabulary. Each is time the agent neither controls nor causes: the
# harness's own machinery. None can cover tool execution, data access or a federation operation the agent triggers.
WAIT_KINDS: dict[str, str] = {
    "model_retry": "retries of a model request and the backoff between them",
    "lease_queue": "waiting for a slot or a lease",
    "sandbox_lifecycle": "start-up and teardown of the sandbox",
    "toolserver_lifecycle": "start-up and teardown of a tool server",
}


def check_wait_kind(kind: object) -> str:
    """Return the kind when it is one of `WAIT_KINDS`; raise ValueError naming it otherwise (L04)."""
    if not isinstance(kind, str) or kind not in WAIT_KINDS:
        raise ValueError(f"unknown infrastructure wait kind {kind!r}; the kinds are: {', '.join(WAIT_KINDS)}")
    return kind


@runtime_checkable
class CallContext(Protocol):
    """What the harness hands a provider with one call: the way to say that the call is waiting on infrastructure.

    `async with ctx.infrastructure_wait(kind):` wraps a wait of one of the `WAIT_KINDS`: `model_retry` (retries and
    backoff), `lease_queue` (waiting for a slot or a lease), `sandbox_lifecycle` and `toolserver_lifecycle` (start-up
    and teardown). Any other kind, or none, raises at once, naming it. The harness pauses the agent's clock while any
    such wait is open and measures it as it happens, per kind, so a wait is never charged to the agent and never has
    to be reported afterwards (L04). Waits may nest or overlap; the clock is paused for their union.

    What the agent's own call does is never infrastructure, and no kind covers it: executing a tool, reading or
    writing data, and any federation operation the agent triggers (a transfer, a replication) are the agent's time,
    however long they take. A provider cannot hide that cost in a wait, because there is no kind to hide it under. A
    wait still open when the harness cuts the call ends at the cut; the time the call then takes to stop is the
    harness's own.
    """

    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]:
        """Return an async context manager that pauses the agent's clock for as long as it is open."""
        ...


@asynccontextmanager
async def _no_wait() -> AsyncIterator[None]:
    yield


class _NoWaits:
    """A call context with no clock behind it, for a provider run outside the loop: waits are not measured. The kind
    is still checked, so a provider that names a wrong one fails here as it would in a run."""

    def infrastructure_wait(self, kind: str) -> AbstractAsyncContextManager[None]:
        check_wait_kind(kind)
        return _no_wait()


NO_WAITS: CallContext = _NoWaits()


@runtime_checkable
class ToolProvider(Protocol):
    """The tools the agent sees for a condition, with schemas, and the executor that runs them.

    A provider returns a `ToolOutput` for every outcome the model should see, errors included (a tool that
    rejects its arguments, a server that answers with an error). Raising is for what the model must not see:
    `InfraError` when the infrastructure failed, and any other exception for a defect in the provider, which the
    loop records as a harness defect and sets the attempt aside (L08).

    Work that blocks must not run on the event loop: a provider wraps it in `asyncio.to_thread` (L15). A running
    thread cannot be killed, and the core does not try: the function in it must bound itself with `timeout_s`, and a
    provider whose call is cancelled must say that the thread may still be running, without waiting for it
    (`FunctionProvider` does, and refuses new calls until the thread returns). A provider that can leave work running
    this way may offer `abandoned_threads() -> int`; the loop result records the count.

    Every wait on infrastructure is wrapped in `ctx.infrastructure_wait(kind)` (see `CallContext`).
    """

    def tools(self) -> Sequence[ToolSpec]:
        """Return the tools this provider offers; the list does not change during a run."""
        ...

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
        """Run one call. `timeout_s` is the agent's time left: the call has no other limit (L05). `ctx` marks waits."""
        ...
