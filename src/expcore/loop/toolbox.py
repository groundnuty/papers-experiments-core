# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/tools.py:121-141
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:611-617, 905-919
"""The tool box: the tools of a run, built from providers, with the output cap stated in every description.

The box answers every call the model can make with a result the model can read: an unknown tool and arguments of
the wrong type become error results (L02), malformed arguments are reported and never run (L01), and every text
the model sees, errors included, goes through the same character cap (L06). The full raw result stays with the
caller, next to a flag saying whether the cap cut what the model saw.
"""

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.loop.arguments import argument_problems
from expcore.loop.capping import cap_error, cap_text, describe_cap, render_error
from expcore.models.adapters import ToolCall, ToolResult
from expcore.plugin import ToolProvider, ToolSpec

SKIPPED_CLASS = "skipped"
SKIP_REASONS = {
    "ended": "not executed: the attempt ended at an earlier call in this turn",
    "time": "not executed: the run's time was used up before this call started",
    "cut": "not completed: the run's time was used up while this call was running",
    "failed": "not completed: this call failed and the attempt ended",
    "cap": "not executed: the attempt ended on a cap before any call of this reply ran",
}
MALFORMED_NOTICE = "The call was not executed; send it again with the arguments as one valid JSON object."


@dataclass(frozen=True)
class ToolRun:
    """The outcome of one call: what the model is given, and what the record keeps."""

    result: ToolResult  # text after the cap, as the backend delivers it
    status: str  # ok, error, malformed or skipped
    raw_text: str  # the full result or error, before the cap
    truncated: bool
    error_class: str | None
    infra_wait_s: float
    ends_attempt: bool  # a successful call of a terminal tool
    source_truncated: bool | None = None  # the data source's own limit was hit, as the provider says (L07)


class ToolBox:
    """The tools one run exposes, from any number of providers, and the cap that applies to all of them."""

    def __init__(self, providers: Sequence[ToolProvider], *, cap_chars: int):
        if cap_chars <= 0:
            raise ValueError(f"the output cap must be a positive number of characters, got {cap_chars!r}")
        self.cap_chars = cap_chars
        self._providers: dict[str, ToolProvider] = {}
        self._specs: dict[str, ToolSpec] = {}
        for provider in providers:
            for spec in provider.tools():
                if spec.name in self._specs:
                    raise ValueError(f"two providers offer a tool named {spec.name!r}")
                self._providers[spec.name] = provider
                self._specs[spec.name] = spec
        self._definitions = [self._definition(self._specs[name]) for name in sorted(self._specs)]
        self.manifest_sha256 = hashlib.sha256(
            json.dumps(self._definitions, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def names(self) -> list[str]:
        """The tool names, sorted."""
        return sorted(self._specs)

    def _definition(self, spec: ToolSpec) -> dict[str, Any]:
        """The tool as the model is told about it; the cap is part of the description, so the manifest hash pins it."""
        description = f"{spec.description.rstrip()} {describe_cap(self.cap_chars)}".strip()
        return {"name": spec.name, "description": description, "input_schema": spec.input_schema}

    def definitions(self) -> list[dict[str, Any]]:
        """Return the tool definitions the backend is given, sorted by name."""
        return json.loads(json.dumps(self._definitions))

    def _error_run(self, call: ToolCall, status: str, error_class: str, message: str) -> ToolRun:
        """Build the run of a call that ends as an error result."""
        raw = render_error(error_class, message)
        text, truncated = cap_error(error_class, message, self.cap_chars)
        return ToolRun(ToolResult(call, text, True), status, raw, truncated, error_class, 0.0, False)

    def skipped(self, call: ToolCall, reason: str = "ended") -> ToolRun:
        """Answer a call that did not run to the end (L09). `reason` is a key of SKIP_REASONS; the text starts with
        the class `skipped` either way."""
        return self._error_run(call, "skipped", SKIPPED_CLASS, SKIP_REASONS[reason])

    def malformed(self, call: ToolCall) -> ToolRun:
        """Answer a call whose arguments were not a JSON object, without running anything (L01)."""
        message = f"{call.parse_error or 'arguments are not valid JSON'}. {MALFORMED_NOTICE}"
        return self._error_run(call, "malformed", "MalformedArguments", message)

    async def run(self, call: ToolCall, *, timeout_s: float) -> ToolRun:
        """Run one call and return its outcome. `timeout_s` is the run's remaining time, passed on unchanged (L05).

        The provider is handed a deep copy of the arguments; the call keeps the original for the record. An exception
        from the provider is not caught: `InfraError` and defects reach the loop, which decides.
        """
        if call.parse_error:
            return self.malformed(call)
        spec = self._specs.get(call.name)
        if spec is None:
            known = ", ".join(self.names) or "none"
            return self._error_run(
                call, "error", "UnknownTool", f"there is no tool named {call.name!r}; the tools are: {known}"
            )
        problems = argument_problems(call.arguments, spec.input_schema)
        if problems:
            return self._error_run(call, "error", "InvalidArguments", "; ".join(problems))
        # The provider gets its own copy: what it does to the arguments (pops a key, edits a nested value) must not
        # change what the record says the model sent.
        provided = copy.deepcopy(call.arguments)
        output = await self._providers[call.name].execute(call.name, provided, timeout_s=timeout_s)
        if output.is_error:
            error_class = output.error_class or "ToolError"
            run = self._error_run(call, "error", error_class, output.text)
            return ToolRun(
                run.result,
                "error",
                run.raw_text,
                run.truncated,
                error_class,
                output.infra_wait_s,
                False,
                output.source_truncated,
            )
        text, truncated = cap_text(output.text, self.cap_chars)
        return ToolRun(
            ToolResult(call, text, False),
            "ok",
            output.text,
            truncated,
            None,
            output.infra_wait_s,
            spec.terminal,
            output.source_truncated,
        )
