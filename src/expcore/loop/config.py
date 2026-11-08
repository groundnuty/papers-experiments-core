# Provenance: new
"""The loop's configuration as resolved for one run: the caps, the malformed-call policy and their hash (L01, L06).

The record keeps these values, not a hash of them; the hash lets two conditions of one contrast be compared at a
glance. It covers the output cap, so two conditions cannot differ in it unnoticed.
"""

import dataclasses
import hashlib
import json
import math
import time
from collections.abc import Callable
from typing import Any

from expcore.config.models import Limits
from expcore.errors import infra_kinds
from expcore.loop.timer import AgentTimer

MALFORMED_POLICIES = ("report", "end")
MIN_CALL_S = 1.0  # a tool call does not start with less time left than this (L05)
TOOL_GRACE_S = 60.0  # how long past the run's remaining time the loop waits for a provider that ignores its timeout


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """What the loop needs to run one attempt.

    - `limits`: the run's caps (rounds, agent time, tokens, characters per tool result, output tokens per reply).
    - `malformed_policy`: `report` tells the model its arguments were not valid JSON and goes on (the default);
      `end` ends the attempt as `malformed_end`. Running the tool with `{}` is not offered (L01).
    - `ceiling_s` and `ceiling_kind`: an optional ceiling on the whole elapsed time, waits on infrastructure
      included. Passing it while the agent is still under its own cap ends the attempt as infrastructure of that
      kind; the agent's own cap decides first (L04).
    - `min_call_s`: no tool call starts with less agent time than this left (L05).
    - `tool_grace_s`: how long past the remaining time the loop waits for a provider that ignores the timeout it
      was given. The agent is told nothing of it; it never shortens a call.
    """

    limits: Limits
    malformed_policy: str = "report"
    ceiling_s: float | None = None
    ceiling_kind: str | None = None
    min_call_s: float = MIN_CALL_S
    tool_grace_s: float = TOOL_GRACE_S

    def __post_init__(self) -> None:
        """Refuse caps that cannot work, an unknown policy, and a ceiling without the kind it ends as."""
        problems = []
        limits = self.limits
        if limits.max_tool_rounds < 0 or limits.token_budget <= 0 or limits.output_cap_chars <= 0:
            problems.append("max_tool_rounds must not be negative; token_budget and output_cap_chars must be positive")
        if not math.isfinite(limits.time_cap_s) or limits.time_cap_s <= 0:
            problems.append("time_cap_s must be a positive number of seconds")
        if self.malformed_policy not in MALFORMED_POLICIES:
            problems.append(f"malformed_policy must be one of {', '.join(MALFORMED_POLICIES)}")
        if (self.ceiling_s is None) != (self.ceiling_kind is None):
            problems.append("ceiling_s and ceiling_kind go together")
        elif self.ceiling_s is not None and not limits.time_cap_s <= self.ceiling_s < math.inf:
            problems.append("ceiling_s must be finite and not below time_cap_s")
        if self.ceiling_kind is not None and self.ceiling_kind not in infra_kinds():
            problems.append(f"ceiling_kind must be one of {', '.join(infra_kinds())}")
        if self.min_call_s < 0 or self.tool_grace_s < 0:
            problems.append("min_call_s and tool_grace_s must not be negative")
        if problems:
            raise ValueError("agent configuration: " + "; ".join(problems))

    def timer(self, clock: Callable[[], float] = time.monotonic) -> AgentTimer:
        """Return a timer holding this configuration's time cap and ceiling, reading the given clock."""
        return AgentTimer(self.limits.time_cap_s, clock=clock, ceiling_s=self.ceiling_s)

    def as_dict(self) -> dict[str, Any]:
        """Return every value as the record keeps it."""
        return dataclasses.asdict(self)

    def config_sha256(self) -> str:
        """Return the SHA-256 of the canonical form of every value; any cap that differs gives a different hash."""
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
