# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/loop.py:636-651
"""How the clock ends an attempt: which outcome a reading, or a call cut at its bound, calls for (L04, L05).

The rule is the same everywhere. The agent's own time decides first: a reading with none left is a time cap, whatever
else is true. Only with the agent under its cap does the whole-time ceiling decide, and it ends the attempt as
infrastructure of the configured kind, since something other than the agent held the run up.
"""

from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import Outcome
from expcore.loop.timer import Tick


def time_ending(tick: Tick, config: AgentConfig, infra_wait_s: float) -> Outcome | None:
    """The ending a reading calls for, or None while the agent is under its cap and the ceiling is not passed."""
    if tick.remaining_s <= 0:
        return Outcome("time_cap", f"the agent's time cap of {config.limits.time_cap_s:g} s was reached")
    if tick.ceiling_passed:
        return Outcome(
            "infrastructure",
            f"{tick.elapsed_s:.0f} s elapsed with {infra_wait_s:.0f} s of waits, past the ceiling of "
            f"{config.ceiling_s:g} s",
            infra_kind=config.ceiling_kind,
        )
    return None


def model_cut(tick: Tick, config: AgentConfig) -> Outcome:
    """The ending for a model request cut at its bound, judged by the reading taken before it was sent.

    A request reports no waits, so the time it ran is the agent's. The bound was the ceiling only when less of it was
    left than of the agent's own time, and the agent is then still under its cap. When both ran out together the
    agent's cap decides.
    """
    if tick.ceiling_left_s is not None and tick.ceiling_left_s < tick.remaining_s:
        detail = f"the model call was still running at the ceiling of {config.ceiling_s:g} s"
        return Outcome("infrastructure", detail, infra_kind=config.ceiling_kind)
    return Outcome("time_cap", "the model call was cut at the agent's time cap")


def tool_cut(name: str, tick: Tick, config: AgentConfig) -> Outcome:
    """The ending for a tool call cut at the ceiling, judged by the reading taken after the cut.

    The agent's time first: a call that reported no wait on infrastructure has used the time it ran as its own, so
    when that used up the cap the ending is a time cap. With the agent still under its cap the ceiling decides.
    """
    if tick.remaining_s > 0:
        detail = f"tool {name!r} was still running at the ceiling of {config.ceiling_s:g} s"
        return Outcome("infrastructure", detail, infra_kind=config.ceiling_kind)
    return Outcome("time_cap", f"tool {name!r} was still running at the agent's time cap")


def cannot_start(tick: Tick, config: AgentConfig, *, too_late: bool) -> Outcome:
    """The ending for a tool call that cannot start: too little agent time left (checked first), or the ceiling."""
    if too_late:
        return Outcome("time_cap", f"{max(tick.remaining_s, 0):.1f} s left, too little to start a tool call")
    return Outcome(
        "infrastructure",
        f"{tick.elapsed_s:.0f} s elapsed, past the ceiling of {config.ceiling_s:g} s",
        infra_kind=config.ceiling_kind,
    )
