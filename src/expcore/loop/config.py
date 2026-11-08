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
CEILING_FACTOR = (
    3.0  # the whole-time ceiling is this many times the agent's time cap unless the run says otherwise (L04)
)
CEILING_KIND = "ceiling"  # the infrastructure kind of an attempt that passes the ceiling with the agent under its cap


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """What the loop needs to run one attempt.

    - `limits`: the run's caps (rounds, agent time, tokens, characters per tool result, output tokens per reply).
    - `malformed_policy`: `report` tells the model its arguments were not valid JSON and goes on (the default);
      `end` ends the attempt as `malformed_end`. Running the tool with `{}` is not offered (L01).
    - `ceiling_s`, `ceiling_factor` and `ceiling_kind`: the ceiling on the whole elapsed time, waits on infrastructure
      included. There is always one, so a call that hangs on infrastructure cannot run for ever: it is `ceiling_s`
      when given, else `ceiling_factor` times the time cap (3 by default), and never below the cap. Every model
      request and tool call is bounded by what is left of it. A call still running when it passes, with the agent
      under its own cap, ends the attempt as infrastructure of `ceiling_kind` (the core's `ceiling` by default);
      the agent's own cap decides first (L04). Both fields are filled in on construction, so the record holds the
      ceiling that was in force.
    - `min_call_s`: no tool call starts with less agent time than this left; never below one second (L05).
    """

    limits: Limits
    malformed_policy: str = "report"
    ceiling_s: float | None = None
    ceiling_kind: str | None = None
    min_call_s: float = MIN_CALL_S
    ceiling_factor: float | None = None

    def __post_init__(self) -> None:
        """Refuse caps that cannot work and an unknown policy, and settle the ceiling and the kind it ends as."""
        problems = []
        limits = self.limits
        if limits.max_tool_rounds < 0 or limits.token_budget <= 0 or limits.output_cap_chars <= 0:
            problems.append("max_tool_rounds must not be negative; token_budget and output_cap_chars must be positive")
        if not math.isfinite(limits.time_cap_s) or limits.time_cap_s <= 0:
            problems.append("time_cap_s must be a positive number of seconds")
        if self.malformed_policy not in MALFORMED_POLICIES:
            problems.append(f"malformed_policy must be one of {', '.join(MALFORMED_POLICIES)}")
        problems.extend(self._settle_ceiling())
        if not (math.isfinite(self.min_call_s) and self.min_call_s >= MIN_CALL_S):
            problems.append(
                f"min_call_s must be a finite number of at least {MIN_CALL_S:g} s: no call starts with less (L05)"
            )
        if problems:
            raise ValueError("agent configuration: " + "; ".join(problems))

    def _settle_ceiling(self) -> list[str]:
        """Fill in `ceiling_s`, `ceiling_factor` and `ceiling_kind` from what was given; return what is wrong.

        Given neither of the first two, the factor is the default. Given both they must agree, which is what a copy
        of a settled configuration holds. Given only `ceiling_s`, no factor is recorded.
        """
        problems = []
        cap = self.limits.time_cap_s
        kind = self.ceiling_kind if self.ceiling_kind is not None else CEILING_KIND
        if kind not in infra_kinds():
            problems.append(f"ceiling_kind must be one of {', '.join(infra_kinds())}")
        object.__setattr__(self, "ceiling_kind", kind)
        factor = self.ceiling_factor
        if factor is not None and not (math.isfinite(factor) and factor >= 1):
            problems.append("ceiling_factor must be a finite number, at least 1")
            return problems
        if not math.isfinite(cap) or cap <= 0:
            return problems  # reported above; no ceiling can be derived from it
        if self.ceiling_s is None:
            factor = CEILING_FACTOR if factor is None else factor
            object.__setattr__(self, "ceiling_factor", factor)
            object.__setattr__(self, "ceiling_s", cap * factor)
        elif not (math.isfinite(self.ceiling_s) and cap <= self.ceiling_s):
            problems.append("ceiling_s must be finite and not below time_cap_s")
        elif factor is not None and not math.isclose(self.ceiling_s, cap * factor):
            problems.append("give ceiling_s or ceiling_factor, not two that disagree")
        return problems

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
