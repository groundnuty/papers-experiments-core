# Provenance: new
"""From a kill or a death to the core's infrastructure kind (O17).

The causes come from the kernel's counters and the container's own state (killcause.py, infradeath.py, earlyoom.py),
never from an exit code. Their categories map to the core's vocabulary of infrastructure kinds:

- `agent`: the agent's code hit its own limit (memory, time). The agent's own outcome, not infrastructure.
- `infrastructure`: the host or a component of the sandbox failed. Infrastructure kind `sandbox`, retried; a death of a
  gate or a sidecar whose cause is `undetermined` is a death nothing attributes, kind `unattributed_kill`.
- `unknown`: counters unreadable, evidence conflicting, or a kill nothing attributes. Infrastructure kind
  `unattributed_kill`, retried within the attempt limit.

Both kinds are in the core's own vocabulary (`expcore.errors.CORE_KINDS`). The platform's own kinds are registered here,
when this module is first imported. Four are the sandbox's and the ingest's, and none is treatable:

- `data_gate_integrity`: the data gate's refusal of an answer it cannot pass to the agent (a redirect, an encoded body,
  its token in a header or a body, an answer larger than it checks whole, a complete listing larger than it serves
  whole) or a gate log that cannot be read (`gate_error`). Such a run is neither the agent's doing nor a condition's
  manipulation: the run is set aside and counted per condition, and the factor schema refuses a condition that
  declares the kind (O05, O16, O21, C01).
- `output_collection`: collecting what the run left failed for a reason the agent did not cause (`copyout.py`,
  `collection_error`): the agent's container could not be frozen for the copy, a copy command or the disk failed, a
  copy did not hold what the listing showed (a name swapped after the listing included), the gate's log, in a
  directory only the gate writes, was not a plain file, or a read-only input's copy no longer had the hash the run
  recorded. The run's evidence is missing or not what it should be, so it says nothing
  about the agent or the manipulation: set aside and run again within the attempt limit, and no condition may declare
  it (O19). An object the agent made in its outputs (a link, a special file, more than its disk holds) is not this
  kind: it is the agent's own doing, recorded for the study's scorers, and the attempt is not set aside.
- `ingest_failure`: a progressive ingest (`expcore_onedata.ingest`) could not do what its plan declares: set the start
  state, write or confirm an arrival, or restore the dataset's baseline after the run (O27). Data missing or partial
  because the plan has not finished is the condition's manipulation and the agent's outcome, never this kind; an
  ingest that fails on its own says nothing about the agent, so the run is set aside and no condition may declare it.
- `cell_unkillable`: a process of a cell outlived its kill at the cell's deadline (a read waiting in the kernel on the
  FUSE mount, as one of a file only a stopped provider holds does), so the sandbox aborted the mount or stopped
  (`deadline.py`, O34). The harness could not bound the cell, and the run's mount or sandbox is gone: the run says
  nothing about the agent or the manipulation, so it is set aside, and no condition may declare it, an outage's
  included. A cell killed cleanly at its deadline is the agent's own time limit, never this kind.

This is the one module of the platform layer that registers kinds, so a study names it alone in `[study] platforms` to
make every kind the layer raises known before its conditions are validated, and `check` imports it alone for a record
that names one. The fault injector's two kinds are registered here too (`expcore_onedata.faults`):
`provider_outage`, a provider of the federation that was unavailable, which a condition may declare with the target
it takes down; and `injector_failure`, the injector's own failure to inject, clear or verify, which is not treatable,
so a run whose fault was never verified is set aside and never scored as the outage (O10, O11, C01).
"""

from collections.abc import Iterable, Mapping
from typing import Any

from expcore.errors import InfraError, register_infra_kind
from expcore.records.injections import INJECTOR_FAILURE  # the core names it: it sets an attempt aside as this kind
from expcore_onedata.sandbox import infradeath, killcause

DATA_GATE_INTEGRITY = "data_gate_integrity"
register_infra_kind(
    DATA_GATE_INTEGRITY,
    "the data gate refused an answer of the platform it could not pass to the agent (a redirect, an encoded body, its "
    "token in a header or a body, an answer or a complete listing too large to check or serve whole), or its log could "
    "not be read: the run says nothing about the agent or the manipulation",
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
PROVIDER_OUTAGE = "provider_outage"
register_infra_kind(PROVIDER_OUTAGE, "a provider of the federation was unavailable")
register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)

INGEST_FAILURE = "ingest_failure"
register_infra_kind(
    INGEST_FAILURE,
    "a progressive ingest failed on its own (its start state, a write or confirmation of an arrival, or the restore "
    "of the dataset's baseline): data the plan had not delivered yet is the manipulation, but a plan the harness could "
    "not carry out says nothing about the agent",
    treatable=False,
)

CELL_UNKILLABLE = "cell_unkillable"
register_infra_kind(
    CELL_UNKILLABLE,
    "a process of the agent's code outlived its kill at the cell's deadline (a read waiting in the kernel on the FUSE "
    "mount), so the sandbox aborted the mount or stopped: the harness could not bound the cell, and the run says "
    "nothing about the agent or the manipulation",
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


def deadline_error(kill: Mapping[str, Any] | None) -> InfraError | None:
    """The error of a cell that outlived its kill at its deadline (`killcause.KILL_SURVIVED`), or None for any other
    cell: kind `cell_unkillable`, with how the deadline ended. It comes before every other error of the cell, the
    gate's refusals included: after an abort or a stop, nothing read from the run is the cell's own evidence."""
    if not kill or kill.get("reason") != killcause.KILL_SURVIVED:
        return None
    outcome = (kill.get("deadline") or {}).get("outcome", "unknown")
    return InfraError(CELL_UNKILLABLE, f"a process of the agent's code outlived its kill at its deadline ({outcome})")


def run_ended_error(record: Mapping[str, Any]) -> InfraError:
    """The error of every later cell of a run a cell's deadline ended (O34), and of its collection once the sandbox was
    stopped: kind `cell_unkillable`, with how that deadline ended."""
    outcome = record.get("outcome", "unknown")
    return InfraError(CELL_UNKILLABLE, f"the run was ended at a cell's deadline ({outcome}): nothing more runs")


def kill_error(kill: Mapping[str, Any] | None) -> InfraError | None:
    """The error a killed cell makes of its attempt; None when the kill was the agent's own outcome, or absent."""
    if not kill:
        return None
    survived = deadline_error(kill)
    if survived is not None:
        return survived
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
    "CELL_UNKILLABLE",
    "DATA_GATE_INTEGRITY",
    "INGEST_FAILURE",
    "INJECTOR_FAILURE",
    "KIND_OF_CATEGORY",
    "OUTPUT_COLLECTION",
    "PROVIDER_OUTAGE",
    "collection_error",
    "deadline_error",
    "death_error",
    "gate_error",
    "kill_error",
    "kind_of",
    "run_ended_error",
]
