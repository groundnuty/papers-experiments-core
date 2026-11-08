# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/loop.py, blob 7e8f2022a0c02e4c99747bd593e61d8b5ad32f6a
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The agent loop: model call -> tool calls -> capped results -> next call, until the model
answers without a tool call (termination `report`), or a cap ends it.

Kept from the dataset_77 base (panel.py ForgeOpenAILeg): one model call per round, all tool calls of a
round executed in order, results appended, stop when a turn has no tool call. Added: a time cap,
a token budget, the identical output cap, per-turn records, and a sandboxed Python tool.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from dataset_76.records import Termination, Turn

from .adapters import ModelAdapter, ToolCall, ToolResult
from .capping import cap_text
from .config import Limits
from .errors import InfraError, RequestAbandoned
from .knowledge import KnowledgePlane
from .sandbox_api import SandboxLike

PYTHON_TOOL = {
    "name": "python",
    "description": ("Run Python code in your sandbox and return its printed output (stdout and stderr). "
                    "Only what you print comes back; data stay in the sandbox."),
    "input_schema": {"type": "object", "properties": {"code": {"type": "string", "description": "Python source to run"}},
                     "required": ["code"]},
}


@dataclasses.dataclass
class LoopResult:
    termination: Termination
    error_type: str | None
    turns: list[Turn]
    code_cells: list[dict]
    final_text: str
    stop_reasons: list[str | None]
    reasoning: list[str | None]
    ending: str = ""                     # final_message | turn_cap | token_budget | time_cap | output_cap | error:<type>
    last_turn_no_tool_call: bool = False
    invalid_tool_args_rounds: int = 0    # rounds with >= 1 call whose arguments did not parse
    invalid_tool_args_events: list = dataclasses.field(default_factory=list)   # {round, call_id, name, error, raw}
    enospc_cells: list = dataclasses.field(default_factory=list)   # cells whose output reports ENOSPC
    kills: list = dataclasses.field(default_factory=list)   # cells killed or timed out: {cell, exit_code, reason, category}


# How long past a cell's own time limit the loop waits for the sandbox to report back (the cell's kill, the read of why it
# was killed). A defensive bound only: the cell itself is stopped by the sandbox at the run's time cap.
TOOL_GRACE_S = 60.0
UNATTRIBUTED_KILL = "unattributed_kill"   # the kind of infrastructure attempt it is: it may discard an agent's own outcome, so `export_infra` counts it per option and model
MIN_CELL_S = 1.0        # a cell is not started with less time left than this (GNU timeout reads 0 as no limit)


def kill_note(kill: dict | None, exit_code: int, limits: Limits, agent_memory_gib: float | None) -> str | None:
    """The one line a killed cell's tool result carries, from a fixed template: identical in every option, naming the limit that
    stopped the cell and its value. The cause comes from the sandbox (cgroup facts), never from the exit code alone."""
    reason = (kill or {}).get("reason")
    if reason == "memory_limit":
        mem = "the memory limit" if agent_memory_gib is None else f"the memory limit of {agent_memory_gib:g} GiB"
        if exit_code not in (137, 124, -9):      # a worker: the cell itself ended on its own (science, 2 Nov)
            return f"note: a process of this cell was stopped because it reached {mem}. The run continues."
        return f"note: the cell was stopped because it reached {mem}. The run continues."
    if reason == "time_limit":
        return f"note: the cell was stopped because the run's time cap ({limits.time_cap_s / 60:g} min) was reached."
    if exit_code in (137, 124, -9) or reason in ("killed_other", "unknown"):
        return f"note: the cell was killed (exit code {exit_code}); the cause is not known. The run continues."
    return None


def _is_enospc(text: str) -> bool:
    return "[Errno 28]" in text or "No space left on device" in text


class ToolBox:
    """The tools one run exposes: `python` plus the knowledge-plane tools."""

    def __init__(self, sandbox: SandboxLike, limits: Limits, knowledge: KnowledgePlane | None = None,
                 agent_memory_gib: float | None = None):
        self.sandbox, self.limits, self.knowledge = sandbox, limits, knowledge
        self.agent_memory_gib = agent_memory_gib
        self.code_cells: list[dict] = []
        self.enospc_cells: list[int] = []    # cells whose output reports "No space left on device" (a lower bound: a caught error is not seen)
        self.kills: list[dict] = []          # one entry per cell that was killed or timed out: {cell, exit_code, reason, category}

    def definitions(self) -> list[dict]:
        return [PYTHON_TOOL] + (self.knowledge.tool_defs if self.knowledge else [])

    async def run(self, call: ToolCall, deadline: float) -> ToolResult:
        """Run one tool call; every delivered text, errors included, goes through the same cap."""
        r = await self._run(call, deadline)
        return ToolResult(r.call, cap_text(r.text, self.limits.output_cap_chars)[0], r.is_error)

    def _exec_by(self, code: str, deadline: float):
        """In the worker thread: a wait for a free thread comes off the cell's time, like the sandbox's own preparation. None when none is left."""
        left = deadline - time.monotonic()
        return self.sandbox.exec(code, left) if left >= MIN_CELL_S else None

    async def _run(self, call: ToolCall, deadline: float) -> ToolResult:
        if call.parse_error:
            msg = (f"error: {call.parse_error}. The call was not executed; send it again with the arguments "
                   "as one valid JSON object.")
            return ToolResult(call, cap_text(msg, self.limits.output_cap_chars)[0], True)
        if call.name == "python":
            code = call.arguments.get("code")
            if not isinstance(code, str):
                return ToolResult(call, "error: the python tool needs a string argument 'code'", True)
            # A cell has no limit of its own: it is bounded only by what is left of the run's time cap.
            timeout = deadline - time.monotonic()
            if timeout < MIN_CELL_S:
                return ToolResult(call, f"note: the cell was not started because the run's time cap ({self.limits.time_cap_s / 60:g} min) "
                                        "was reached.", True)
            try:
                res = await asyncio.to_thread(self._exec_by, code, deadline)
            except Exception as e:  # noqa: BLE001 -- exec raising (not a non-zero exit) is a sandbox failure
                raise InfraError("sandbox", f"{type(e).__name__}: {e}"[:500]) from e
            if res is None:
                return ToolResult(call, f"note: the cell was not started because the run's time cap ({self.limits.time_cap_s / 60:g} min) "
                                        "was reached.", True)
            kill = getattr(res, "kill", None)
            cell = {"code": code, "exit_code": res.exit_code, "duration_s": res.duration_s}
            # 124 is the harness's own only when it says so (`timeout -s KILL` exits 137): otherwise the agent's code chose it, and nothing was killed
            own_124 = res.exit_code == 124 and not getattr(res, "timed_out", True)
            if kill or (res.exit_code in (137, 124, -9) and not own_124):
                k = {"cell": len(self.code_cells) + 1, "exit_code": res.exit_code, "reason": (kill or {}).get("reason", "unknown"),
                     "category": (kill or {}).get("category", "unknown")}
                self.kills.append(k)
                cell["kill"] = k
            if _is_enospc(res.stdout) or _is_enospc(res.stderr):
                cell["enospc"] = True
                self.enospc_cells.append(len(self.code_cells) + 1)
            self.code_cells.append(cell)
            if kill and kill.get("category") == "infrastructure":     # the host ran out of memory, or the sandbox died: not the agent's doing
                raise InfraError("sandbox", f"cell {len(self.code_cells)} ended with exit {res.exit_code}: {kill['reason']} ({kill.get('basis')})"[:500])
            if kill and kill.get("category") == "unknown":            # what the harness lost track of is not an outcome (science, 2 Nov): retried, and reported per option and model
                raise InfraError(UNATTRIBUTED_KILL, f"cell {len(self.code_cells)} ended with exit {res.exit_code}: {kill['reason']} ({kill.get('basis')})"[:500])
            note = None if own_124 and not kill else kill_note(kill, res.exit_code, self.limits, self.agent_memory_gib)
            text = (f"exit_code: {res.exit_code}\n" + (note + "\n" if note else "")
                    + f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}")
            return ToolResult(call, cap_text(text, self.limits.output_cap_chars)[0], res.exit_code != 0)
        if self.knowledge and call.name in self.knowledge.names:
            text, err = await self.knowledge.call(call.name, call.arguments)
            return ToolResult(call, cap_text(text, self.limits.output_cap_chars)[0], err)
        return ToolResult(call, f"error: unknown tool {call.name!r}", True)


OUTPUT_LIMIT_STOPS = ("length", "max_tokens")     # OpenAI finish_reason / Anthropic stop_reason


def _measure(rec: Turn) -> None:
    """Exact size of what the tool results delivered: characters and UTF-8 bytes, per result and in total.
    Model-independent (unlike a token difference, which depends on whether reasoning is kept in history)."""
    rec.tool_output_chars = [len(t) for t in rec.tool_outputs]
    rec.tool_output_bytes = [len(t.encode("utf-8")) for t in rec.tool_outputs]
    rec.chars_into_context = sum(rec.tool_output_chars)
    rec.bytes_into_context = sum(rec.tool_output_bytes)


async def run_agent(adapter: ModelAdapter, tools: ToolBox, system: str, user: str, limits: Limits) -> LoopResult:
    """Run the loop. Raises InfraError (carrying `.partial`, the LoopResult so far) on endpoint or
    sandbox failure; every other ending is a normal LoopResult."""
    start = time.monotonic()
    deadline = start + limits.time_cap_s
    adapter.begin(system, user, tools.definitions())
    turns: list[Turn] = []
    stop_reasons, reasoning = [], []
    used_tokens = 0
    n_rounds = 0
    invalid_rounds, invalid_events = 0, []
    final_text = ""
    term, err = Termination.TURN_CAP, None
    last_no_tool = False

    def result(infra: bool = False) -> LoopResult:
        ending = ("final_message" if term is Termination.REPORT else
                  f"error:{err}" if term is Termination.ERROR else term.value)
        return LoopResult(term, err, turns, tools.code_cells, final_text, stop_reasons, reasoning,
                          ending, last_no_tool, invalid_rounds, invalid_events, list(tools.enospc_cells), list(tools.kills))

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            term = Termination.TIME_CAP
            break
        try:
            turn = await asyncio.wait_for(adapter.step(), timeout=remaining)
        except asyncio.TimeoutError:
            # (an adapter marks a request abandoned itself, only when the cancellation hit one in flight)
            term = Termination.TIME_CAP
            break
        except RequestAbandoned:                 # accepted, then slow, never resent: the wall time is what ran out
            term = Termination.TIME_CAP
            break
        except InfraError as e:
            e.partial = result()  # type: ignore[attr-defined]
            raise
        except Exception as e:  # noqa: BLE001 -- recorded as termination error with its type
            term, err = Termination.ERROR, type(e).__name__
            break
        used_tokens += turn.input_tokens + turn.output_tokens
        stop_reasons.append(turn.stop_reason)
        reasoning.append(turn.reasoning)
        rec = Turn(index=len(turns), input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                   tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls],
                   tool_outputs=[], bytes_into_context=0, tokens_into_context=0,
                   uncached_input_tokens=turn.uncached_input_tokens,
                   cache_creation_input_tokens=turn.cache_creation_input_tokens,
                   cache_read_input_tokens=turn.cache_read_input_tokens)
        turns.append(rec)
        bad = [c for c in turn.tool_calls if c.parse_error]     # counted before any cap check: a capped round
        if bad:                                                 # is still the round the model requested
            invalid_rounds += 1
            for c in bad:
                invalid_events.append({"round": n_rounds + 1, "call_id": c.id, "name": c.name,
                                       "error": c.parse_error, "raw": (c.raw_arguments or "")[:2000]})
        if not turn.tool_calls and turn.stop_reason in OUTPUT_LIMIT_STOPS:
            final_text, term = turn.text, Termination.OUTPUT_CAP   # classified before the budget check
            break
        if used_tokens > limits.token_budget:
            term = Termination.TOKEN_BUDGET
            break
        if not turn.tool_calls:
            final_text, term, last_no_tool = turn.text, Termination.REPORT, True   # (truncated ones broke out above)
            break
        # {TURN_CAP} counts tool rounds (one assistant message with >= 1 tool calls). The cap fires when
        # the agent requests round TURN_CAP+1; a tool-free final reply after the last round is allowed
        # (that case broke out above).
        if n_rounds + 1 > limits.max_tool_rounds:
            term = Termination.TURN_CAP
            break
        n_rounds += 1
        results = []
        stop: tuple[Termination, str | None] | None = None
        for c in turn.tool_calls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop = (Termination.TIME_CAP, None)
                break
            try:
                results.append(await asyncio.wait_for(tools.run(c, deadline), timeout=remaining + (TOOL_GRACE_S if c.name == "python" else 1.0)))
            except asyncio.TimeoutError:
                stop = (Termination.TIME_CAP, None)
                break
            except InfraError as e:
                rec.tool_outputs = [r.text for r in results]
                _measure(rec)
                e.partial = result()  # type: ignore[attr-defined]
                raise
            except Exception as e:  # noqa: BLE001 -- unexpected tool failure: keep what was measured
                stop = (Termination.ERROR, type(e).__name__)
                break
        rec.tool_outputs = [r.text for r in results]
        _measure(rec)
        if stop:
            term, err = stop
            break
        adapter.add_tool_results(results)
        if time.monotonic() >= deadline:
            term = Termination.TIME_CAP
            break
    return result()
