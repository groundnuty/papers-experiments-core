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
treat (O05, C01). A cell a process of which outlived its kill at the cell's deadline raises `InfraError` of kind
`cell_unkillable`, before anything else, which no condition can treat either (O34).

What is kept of a cell's output is capped per stream by the sandbox (`cells.OUTPUT_CAP_BYTES`, O43). When it cut
one, the text this tool returns ends with a notice that says how much of each cut stream was written and how much kept,
and the call's `source_truncated` is True, as for any source's own limit (L07, apart from the loop's own cap); the
numbers are on the cell's record too (`output_cut`).

The loop's cap (`output_cap_chars`, L06) then applies to that whole text, the notice included, and the order is fixed:
the sandbox cuts first, this tool adds its notice, and the loop cuts last, from the end of the text, with its own notice
counted inside the cap. A cap that the text and the notice fit leaves the notice as the last line. A smaller cap cuts
it off with the tail, and the model sees the loop's notice instead. A cap just below the text, by a few characters
(the loop's notice takes room from the cap too), cuts the notice in the middle, and a cap too short for the loop's
notice cuts the text plain. The record keeps the whole text and both flags either way.
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
        # a cell that outlived its kill first (O34): its mount or its sandbox is gone, and nothing read after that, the
        # gate's log included, is the cell's evidence; then the gate's refusal: its read was cut by the gate
        failure = (
            kinds.deadline_error(result.kill) or kinds.gate_error(result.gate_refusals) or kinds.kill_error(result.kill)
        )
        if failure is not None:
            raise failure
        return self._output(result)

    @classmethod
    def _output(cls, result: ExecResult) -> ToolOutput:
        """The text the model sees for a finished cell, or for one too little time was left to start; a notice ends it
        when the sandbox cut the cell's output (O43)."""
        shown = cls._shown(result)
        if not result.output_cut:
            return shown
        return ToolOutput(
            f"{shown.text}\n{_cut_notice(result.output_cut)}", shown.is_error, shown.error_class, source_truncated=True
        )

    @staticmethod
    def _shown(result: ExecResult) -> ToolOutput:
        """The output of a finished cell as it would be shown if nothing had been cut."""
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


def _cut_notice(cut: dict[str, dict[str, int]]) -> str:
    """The line that tells the model what the sandbox cut of a cell's output: each cut stream, stdout first."""
    parts = [
        f"the first {counts['kept']} of the {counts['produced']} bytes of {stream} were kept"
        for stream, counts in sorted(cut.items(), reverse=True)
    ]
    return f"[output cut by the sandbox: {'; '.join(parts)}]"


def _with(output: str, note: str) -> str:
    """The output followed by a note on its own line."""
    return f"{output.rstrip()}\n{note}".lstrip("\n")
