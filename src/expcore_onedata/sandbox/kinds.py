# Provenance: new
"""From a kill or a death to the core's infrastructure kind (O17).

The causes come from the kernel's counters and the container's own state (killcause.py, infradeath.py, earlyoom.py),
never from an exit code. Their categories map to the core's vocabulary of infrastructure kinds:

- `agent`: the agent's code hit its own limit (memory, time). The agent's own outcome, not infrastructure.
- `infrastructure`: the host or a component of the sandbox failed. Infrastructure kind `sandbox`, retried; a death of a
  gate or a sidecar whose cause is `undetermined` is a death nothing attributes, kind `unattributed_kill`.
- `unknown`: counters unreadable, evidence conflicting, or a kill nothing attributes. Infrastructure kind
  `unattributed_kill`, retried within the attempt limit.

Both kinds are in the core's own vocabulary (`expcore.errors.CORE_KINDS`), so no kind is registered here.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from expcore.errors import InfraError
from expcore_onedata.sandbox import infradeath

KIND_OF_CATEGORY: Mapping[str, str | None] = {
    "agent": None,
    "infrastructure": "sandbox",
    "unknown": "unattributed_kill",
}


def kind_of(category: str | None) -> str | None:
    """The infrastructure kind of a category; None for the agent's own outcome. An unrecognised category is unknown."""
    return KIND_OF_CATEGORY.get(category or "", "unattributed_kill")


def kill_error(kill: Mapping[str, Any] | None) -> InfraError | None:
    """The error a killed cell makes of its attempt; None when the kill was the agent's own outcome, or absent."""
    if not kill:
        return None
    kind = kind_of(kill.get("category"))
    if kind is None:
        return None
    return InfraError(
        kind, f"the agent's code was killed ({kill.get('reason', 'unknown')}): {kill.get('basis', '')}"[:600]
    )


def death_error(deaths: Iterable[Mapping[str, Any]]) -> InfraError | None:
    """The error that the death of a gate or a sidecar makes of its attempt, or None when no death was infrastructure.

    A death of the agent's own doing (the sidecar hit its own memory limit under the agent's reads) is the agent's
    outcome. One cause is reported when several containers died, the specific before the undetermined. A determined
    cause (the host's pressure, the gate's own limit) is kind `sandbox`; a run whose one cause is `undetermined` (an
    exit code with no counter, kernel line or earlyoom record, a state that could not be read) is a death nothing
    attributes, kind `unattributed_kill`, as an unattributed kill of a cell is (O17).
    """
    deaths = list(deaths)
    detail = infradeath.infra_detail(deaths)
    if not detail:
        return None
    cause = infradeath.run_cause(
        d.get("cause") for d in deaths if isinstance(d, dict) and d.get("category") == "infrastructure"
    )
    kind = "unattributed_kill" if cause == infradeath.UNDETERMINED else "sandbox"
    return InfraError(kind, detail[:600])


__all__ = ["KIND_OF_CATEGORY", "death_error", "kill_error", "kind_of"]
