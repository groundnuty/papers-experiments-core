# Provenance: new
"""What a finished attempt hands to the record: turns, tool calls with their raw results, counts, the outcome.

These are plain data. The record writer (a later step) turns them into the on-disk format; nothing here reads a
clock or touches a file.
"""

from dataclasses import dataclass, field
from typing import Any

from expcore.loop.outcomes import Outcome

# ok: the tool ran and returned. error: the model sees an error result (unknown tool, bad arguments, a tool error).
# malformed: the arguments were not a JSON object; the call did not run. skipped: the call did not run because the
# attempt ended at an earlier call or on a cap. cut: the run's time ran out while the call was running. failed: the
# provider raised (an infrastructure failure or a defect; the detail says which).
TOOL_STATUSES = ("ok", "error", "malformed", "skipped", "cut", "failed")


@dataclass
class TurnRecord:
    """One model reply."""

    index: int
    text: str
    reasoning: str | None
    stop_reason: str | None
    input_tokens: int | None  # None when the reply did not report it (B10)
    output_tokens: int | None
    latency_s: float
    tool_call_ids: list[str]
    served_model: str | None = None
    system_fingerprint: str | None = None
    retries: tuple[dict[str, Any], ...] = ()


@dataclass
class ToolCallRecord:
    """One tool call the model made: what it sent, what the model saw, and the full raw result."""

    turn: int  # index of the reply that made the call
    round: int  # 1-based round of tool calls, which the turn cap counts; 0 when the call was never part of one
    call_id: str
    name: str
    arguments: dict[str, Any]  # as the model sent them; {"_raw": ...} when they were not a JSON object (L01)
    status: str  # one of TOOL_STATUSES
    text: str  # exactly what the model saw, after the cap
    raw_text: str  # the full result or error before the cap
    truncated: bool  # the harness cap cut what the model saw (L06); not the source's own limit, see source_truncated
    error_class: str | None = None
    raw_arguments: str | None = None  # the model's own text, kept when it did not parse
    infra_wait_s: float = 0.0
    started_s: float | None = None  # seconds since the run started; None when the call never started
    ended_s: float | None = None
    detail: str = ""  # why a call was skipped or crashed
    source_truncated: bool | None = None  # the data source hit its own row or size limit, as the provider said (L07)

    def __post_init__(self) -> None:
        """Refuse a status outside the closed list."""
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"unknown tool call status {self.status!r}; the statuses are: {', '.join(TOOL_STATUSES)}")


@dataclass
class LoopResult:
    """The whole attempt, however it ended; an infrastructure ending still carries everything so far (R09).

    `final_text` is None when the model never gave a final reply (a cap, an error, a terminal tool call ended
    the attempt) and "" when it replied with no content: the two differ (L11).
    """

    outcome: Outcome
    turns: list[TurnRecord]
    tool_calls: list[ToolCallRecord]
    final_text: str | None
    rounds: int
    malformed_calls: int  # the run's count of malformed tool-call arguments (L01)
    malformed_events: list[dict[str, Any]]
    tokens_used: int
    usage_incomplete: bool  # an abandoned request, or a reply without usage: the totals are a lower bound
    agent_s: float  # the time charged to the agent
    elapsed_s: float
    infra_wait_s: float
    clock_readings: list[tuple[str, float]]
    transcript: list[dict[str, Any]]
    backend_info: dict[str, Any]
    tool_manifest_sha256: str
    config_sha256: str
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def malformed_rounds(self) -> int:
        """The number of rounds that held at least one malformed call."""
        return len({event["round"] for event in self.malformed_events})
