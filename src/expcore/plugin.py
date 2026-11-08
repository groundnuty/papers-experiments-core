# Provenance: new
"""Plugin interfaces. All are `typing.Protocol`s, so a test double needs no inheritance.

This module holds the tool interface the agent loop is built on. The core ships one provider (the MCP stdio
client) and a wrapper for plain functions; a platform or a study supplies any other, including a code-execution
tool: the core has no built-in tool of its own.
"""

import math
import re
from collections.abc import Sequence
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
    sees first. `infra_wait_s` is the time the call spent waiting on infrastructure (a store, a gate, a slot, a
    lock, output validation); the loop does not charge it to the agent's time cap (L04).

    `source_truncated` is for the data source's own limit (L07): True when the source hit its row or size limit, so
    the result may be cut by the source; False when the provider checked and it was not; None when the provider says
    nothing. The record keeps it apart from the flag for the harness's output cap, which the loop sets on what it cut.
    """

    text: str
    is_error: bool = False
    error_class: str | None = None
    infra_wait_s: float = 0.0
    source_truncated: bool | None = None

    def __post_init__(self) -> None:
        """Refuse an error class on a result that is not an error, a wait that is negative or not a number, and a
        source flag that is not a boolean."""
        if self.error_class is not None and not self.is_error:
            raise ValueError("error_class is only for a result with is_error set")
        if not math.isfinite(self.infra_wait_s) or self.infra_wait_s < 0:
            raise ValueError(f"infra_wait_s must be a finite number of seconds, not negative: {self.infra_wait_s!r}")
        if self.source_truncated is not None and not isinstance(self.source_truncated, bool):
            raise ValueError(f"source_truncated must be True, False or None, not {self.source_truncated!r}")


@runtime_checkable
class ToolProvider(Protocol):
    """The tools the agent sees for a condition, with schemas, and the executor that runs them.

    A provider returns a `ToolOutput` for every outcome the model should see, errors included (a tool that
    rejects its arguments, a server that answers with an error). Raising is for what the model must not see:
    `InfraError` when the infrastructure failed, and any other exception for a defect in the provider, which the
    loop records as a harness defect and sets the attempt aside (L08).

    Work that blocks must not run on the event loop: a provider wraps it in `asyncio.to_thread` (L15). A running
    thread cannot be killed, so the function in it must bound itself with `timeout_s`, and a provider whose call is
    cancelled must say that the thread may still be running, without waiting for it (`FunctionProvider` does).
    """

    def tools(self) -> Sequence[ToolSpec]:
        """Return the tools this provider offers; the list does not change during a run."""
        ...

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> ToolOutput:
        """Run one call. `timeout_s` is the run's remaining time: the call has no other limit (L05)."""
        ...
