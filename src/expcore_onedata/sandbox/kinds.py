# Provenance: new
"""From a kill or a death to the core's infrastructure kind (O17).

The causes come from the kernel's counters and the container's own state (killcause.py, infradeath.py, earlyoom.py),
never from an exit code. Their categories map to the core's vocabulary of infrastructure kinds:

- `agent`: the agent's code hit its own limit (memory, time). The agent's own outcome, not infrastructure.
- `infrastructure`: the host or a component of the sandbox failed. Infrastructure kind `sandbox`, retried; a death of a
  gate or a sidecar whose cause is `undetermined` is a death nothing attributes, kind `unattributed_kill`.
- `unknown`: counters unreadable, evidence conflicting, or a kill nothing attributes. Infrastructure kind
  `unattributed_kill`, retried within the attempt limit.

Both kinds are in the core's own vocabulary (`expcore.errors.CORE_KINDS`). Two kinds are the platform's own, registered
here, when this module is first imported, and neither is treatable:

- `data_gate_integrity`: the data gate's refusal of an answer it cannot pass to the agent (a redirect, an encoded body,
  its token in a header, its token in a body) or a gate log that cannot be read (`gate_error`). Such a run is neither
  the agent's doing nor a condition's manipulation: the run is set aside and counted per condition, and the factor
  schema refuses a condition that declares the kind (O05, O16, C01).
- `output_collection`: collecting what the run left failed for a reason the agent did not cause (`copyout.py`,
  `collection_error`): the agent's container could not be frozen for the copy, a copy command or the disk failed, a
  copy did not hold what the listing showed (a name swapped after the listing included), the gate's log, in a
  directory only the gate writes, was not a plain file, or a read-only input's copy no longer had the hash the run
  recorded. The run's evidence is missing or not what it should be, so it says nothing
  about the agent or the manipulation: set aside and run again within the attempt limit, and no condition may declare
  it (O19). An object the agent made in its outputs (a link, a special file, more than its disk holds) is not this
  kind: it is the agent's own doing, recorded for the study's scorers, and the attempt is not set aside.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from expcore.errors import InfraError, register_infra_kind
from expcore_onedata.sandbox import infradeath

DATA_GATE_INTEGRITY = "data_gate_integrity"
register_infra_kind(
    DATA_GATE_INTEGRITY,
    "the data gate refused an answer of the platform it could not pass to the agent (a redirect, an encoded body, or "
    "its token in a header or a body), or its log could not be read: the run says nothing about the agent or the "
    "manipulation",
    treatable=False,
)
OUTPUT_COLLECTION = "output_collection"
register_infra_kind(
    OUTPUT_COLLECTION,
    "collecting what the run left in its sandbox failed for a reason the agent did not cause (its container could "
    "not be frozen, a copy command or the disk failed, a copy was not what was listed, the gate's log was not a plain "
    "file, or a read-only input's copy lost its hash): the run's evidence is missing or not what it should be, so it "
    "says nothing about the agent or the manipulation",
    treatable=False,
)

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


def gate_error(refusals: Iterable[str]) -> InfraError | None:
    """The error the data gate's own refusals make of an attempt (`gatelog.refusals`), or None when there were none:
    the condition's data read was cut or refused by the gate, never by the agent."""
    reasons = sorted(set(refusals))
    if not reasons:
        return None
    return InfraError(
        DATA_GATE_INTEGRITY,
        f"the data gate refused the platform's answer to the condition's data read ({', '.join(reasons)}): the read "
        "was cut by the gate, not by the agent",
    )


def collection_error(what: str, reason: str) -> InfraError:
    """The error a collection the harness could not make gives its attempt: kind `output_collection`, naming what was
    being collected and why, in the sandbox's own words, which name no path."""
    return InfraError(OUTPUT_COLLECTION, f"collecting {what} failed: {reason}"[:600])


__all__ = [
    "DATA_GATE_INTEGRITY",
    "KIND_OF_CATEGORY",
    "OUTPUT_COLLECTION",
    "collection_error",
    "death_error",
    "gate_error",
    "kill_error",
    "kind_of",
]
