# Provenance: new
"""The code-execution tool: it runs the agent's Python in the sandbox, and nothing else.

The core has no built-in code tool; this is the platform's. It is a `ToolProvider` over any `Sandbox` that has `exec`.
The tool's name and description are arguments, so the study decides what the model is told, and the manifest hash it
records follows from them.

Waits. Executing the agent's code is the agent's own time, and so is every read of data the code makes: none of it is
inside `ctx.infrastructure_wait`, and no wait kind exists to hide it under (L04). Start and teardown of the sandbox are
the only waits of this layer (`SandboxPlatform.launch`).

Blocking work does not run on the event loop: `exec` runs in a thread and bounds itself with `timeout_s` (the
container's own `timeout` kills the cell, and the host call has a margin beyond it), because a thread cannot be
killed (L15).

What the model sees. The code's output, and for a failure its exit code and error stream. A kill the kernel's counters
attribute to the agent (its own memory limit, its own time limit) is the agent's outcome and is told to the model as
such. A kill the counters attribute to the host or a component, or that nothing attributes, is infrastructure: it
raises `InfraError` of kind `sandbox` or `unattributed_kill` and ends the attempt (O17). A cell during which the data
gate refused an answer for its own integrity raises `InfraError` of kind `data_gate_integrity`, which no condition can
treat (O05, C01).
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from expcore.plugin import CallContext, ToolOutput, ToolSpec
from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.api import NOT_STARTED, ExecResult, Sandbox

DEFAULT_NAME = "python"
DEFAULT_DESCRIPTION = "Run Python code in the sandbox and return what it printed. State does not carry between calls."
MIN_TIMEOUT_S = 1.0  # the sandbox refuses a shorter limit; the loop cuts a call that outlasts the agent's time anyway


class CodeExecutionProvider:
    """A `ToolProvider` with one tool that runs Python source in a sandbox."""

    def __init__(self, sandbox: Sandbox, *, name: str = DEFAULT_NAME, description: str = DEFAULT_DESCRIPTION) -> None:
        self._sandbox = sandbox
        self._spec = ToolSpec(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Python source to run."}},
                "required": ["code"],
                "additionalProperties": False,
            },
        )

    def tools(self) -> Sequence[ToolSpec]:
        return (self._spec,)

    async def execute(self, name: str, arguments: dict[str, Any], *, timeout_s: float, ctx: CallContext) -> ToolOutput:
        if name != self._spec.name:
            return ToolOutput(f"unknown tool {name!r}", is_error=True, error_class="unknown_tool")
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return ToolOutput(
                "the argument 'code' must be a non-empty string", is_error=True, error_class="bad_arguments"
            )
        result = await asyncio.to_thread(self._sandbox.exec, code, max(float(timeout_s), MIN_TIMEOUT_S))
        failure = kinds.gate_error(result.gate_refusals) or kinds.kill_error(result.kill)
        if failure is not None:  # the gate's refusal first: whatever the cell did after, its read was cut by the gate
            raise failure
        return self._output(result)

    @staticmethod
    def _output(result: ExecResult) -> ToolOutput:
        """The text the model sees for a finished cell, or for one too little time was left to start."""
        if result.status == NOT_STARTED:  # it ran nothing: no timeout, no exit; the loop's time rule decides the rest
            return ToolOutput(
                "the code was not started: too little of the time was left", is_error=True, error_class="not_started"
            )
        if result.timed_out:
            return ToolOutput(
                _with(result.stdout, "the code did not finish within the time limit and was stopped"),
                is_error=True,
                error_class="timeout",
            )
        if result.kill is not None:  # the agent's own limit: the kill is told as what it was
            reason = str(result.kill.get("reason", "killed")).replace("_", " ")
            return ToolOutput(
                _with(result.stdout, f"the code was killed ({reason})"), is_error=True, error_class="killed"
            )
        if result.exit_code != 0:
            return ToolOutput(
                _with(result.stdout, f"exit code {result.exit_code}\n{result.stderr}".rstrip()),
                is_error=True,
                error_class="exit_status",
            )
        return ToolOutput(_with(result.stdout, result.stderr) if result.stderr.strip() else result.stdout)


def _with(output: str, note: str) -> str:
    """The output followed by a note on its own line."""
    return f"{output.rstrip()}\n{note}".lstrip("\n")
