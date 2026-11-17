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
when this module is first imported. Seven are the sandbox's and the ingest's, and none is treatable:

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
- `ingest_held`: a preallocated ingest's start held back by its baseline's mark, before it read or sent anything
  (`expcore_onedata.ingesthealth`, O44): the baseline is held open by another live attempt, or an earlier attempt left
  it not whole. A hold, never a failure of this run: set aside, never treatable, and it costs the run no attempt
  (`register_infra_kind(..., charged=False)`), so a campaign that meets a held baseline loses no run to its limit.
- `cell_unkillable`: a process of a cell outlived its kill at the cell's deadline (a read waiting in the kernel on the
  FUSE mount, as one of a file only a stopped provider holds does), so the sandbox aborted the mount or stopped
  (`deadline.py`, O34). The harness could not bound the cell, and the run's mount or sandbox is gone: the run says
  nothing about the agent or the manipulation, so it is set aside, and no condition may declare it, an outage's
  included. A cell killed cleanly at its deadline is the agent's own time limit, never this kind.
- `cell_not_isolated`: the Apptainer backend's watch ended the run because memory past a limit could not be told
  this cell's, another run's cell being charged to the same cgroup (`scope: run`, `attributed: false`, O72). The
  memory may be the other run's, so the run says nothing about the agent or the manipulation: it is set aside and
  costs an attempt as a `sandbox` failure does, no condition may declare it, and it is counted apart, so a campaign
  sees how many runs ended so (the run's own record of it is `infrastructure_facts.run_ended`).
- `io_mode_integrity`: the mount run's effective oneclient I/O mode, as oneclient's own start lines and detection
  lines show it (`iomode`, O49), was not the mode the run declared, or the log could not say. The run ran in another
  mode, or in none that can be shown, so it says nothing about the manipulation: set aside and never scored as the
  declared mode, and no condition may declare it.

This is the one module of the platform layer that registers kinds, so a study names it alone in `[study] platforms` to
make every kind the layer raises known before its conditions are validated, and `check` imports it alone for a record
that names one. The fault injector's two kinds are registered here too (`expcore_onedata.faults`):
`provider_outage`, a provider of the federation that was unavailable, which a condition declares with the target
it takes down and never without it (`requires_target=True`, C34); and `injector_failure`, the injector's own failure to
inject, clear or verify, which is not treatable, so a run whose fault was never verified is set aside and never scored
as the outage (O10, O11, C01). The federation's lock has one more (`expcore_onedata.admission`): `federation_busy`, a
run's bounded wait for the federation that ran out while another run held it, an outage run through its restore and
heal say, or while an outage run's sessions drain. A hold like `ingest_held`: never treatable, it costs no attempt, so
waits piled up behind a long heal never exhaust a run (O11).

The module also declares the platform's longest deferral for the core (`declared_deferral_from_config`, O35): the core
calls it with the study's platform.toml when a campaign starts and holds the health check to the answer, so a study's
own wrapper around the platform's check or its run admission cannot hide it. The platform's loaders (`load_platform`,
`load_faults`) also record this module, with the file they read, while a study's build is open
(`expcore.config.platform_loads`), and a study whose build loaded the platform without listing the module is refused at
its start, so a plugin that hides both declarations cannot leave the platform out of `[study] platforms`.
"""

import os
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from expcore.errors import PROVIDER_OUTAGE, InfraError, register_infra_kind
from expcore.records.injections import INJECTOR_FAILURE  # the core names it: it sets an attempt aside as this kind
from expcore_onedata.sandbox import deadline, infradeath, killcause

if TYPE_CHECKING:
    from expcore.campaign.healthgate import DeferralBound

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
register_infra_kind(PROVIDER_OUTAGE, "a provider of the federation was unavailable", requires_target=True)
register_infra_kind(INJECTOR_FAILURE, "the fault injector could not inject, clear or verify its fault", treatable=False)

FEDERATION_BUSY = "federation_busy"
register_infra_kind(
    FEDERATION_BUSY,
    "a run's bounded wait for the federation ran out while another run legitimately held it (an outage run's restore "
    "and heal, say): nothing was started or taken down, so the run costs no attempt and asks again",
    treatable=False,
    charged=False,
)

INGEST_FAILURE = "ingest_failure"
register_infra_kind(
    INGEST_FAILURE,
    "a progressive ingest failed on its own (its start state, a write or confirmation of an arrival, or the restore "
    "of the dataset's baseline): data the plan had not delivered yet is the manipulation, but a plan the harness could "
    "not carry out says nothing about the agent",
    treatable=False,
)

INGEST_HELD = "ingest_held"
register_infra_kind(
    INGEST_HELD,
    "a progressive ingest held its start back, with nothing read or sent: its space's baseline was held open by "
    "another live attempt, or left not whole by an earlier one, which a recovery must bring back; the run costs no "
    "attempt and is tried again",
    treatable=False,
    charged=False,
)

CELL_UNKILLABLE = "cell_unkillable"
register_infra_kind(
    CELL_UNKILLABLE,
    "a process of the agent's code outlived its kill at the cell's deadline (a read waiting in the kernel on the FUSE "
    "mount), so the sandbox aborted the mount or stopped: the harness could not bound the cell, and the run says "
    "nothing about the agent or the manipulation",
    treatable=False,
)

CELL_NOT_ISOLATED = "cell_not_isolated"
register_infra_kind(
    CELL_NOT_ISOLATED,
    "the watch ended the run because memory past a limit could not be told this cell's (another run's cell was charged "
    "to the same cgroup, so it may be that run's): the run says nothing about the agent or the manipulation, and is "
    "counted apart from every other sandbox failure",
    treatable=False,
)

IO_MODE_INTEGRITY = "io_mode_integrity"
register_infra_kind(
    IO_MODE_INTEGRITY,
    "a mount run's effective oneclient I/O mode, read from oneclient's own start lines, was not the declared one, or "
    "its log could not say: a run that did not run in the mode it declares says nothing about the manipulation",
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
    cell: kind `cell_unkillable`, with how the deadline ended. The run's ending is never raised in place of that cell's
    own result (O72): the code tool returns the cell's timeout and uses this only to tell such a cell, whose gate
    refusals and kill it then reads no further: after an abort or a stop, nothing read from the run is the cell's own
    evidence. The run's end is `run_ended_error`'s, raised at the next call, by `launch` and by the collection."""
    if not kill or kill.get("reason") != killcause.KILL_SURVIVED:
        return None
    outcome = (kill.get("deadline") or {}).get("outcome", "unknown")
    return InfraError(CELL_UNKILLABLE, f"a process of the agent's code outlived its kill at its deadline ({outcome})")


def run_ended_error(record: Mapping[str, Any]) -> InfraError:
    """The error of every later cell of a run a cell's deadline ended (O34), and of its collection once the sandbox was
    stopped: kind `cell_unkillable`, with how that deadline ended, and the cell it was when the record knows it (the
    detail then says that cell ran, and hit its deadline: its own result stands, O72). A run the Apptainer backend's
    watch ended, because
    what passed a limit could not be told one cell's (`scope: run`, O72), is kind `cell_not_isolated` instead: set
    aside, and counted apart from every other sandbox failure."""
    if record.get("scope") == "run":
        why = record.get("why") or "what passed a limit could not be told one cell's"
        reason = record.get("reason")
        return InfraError(CELL_NOT_ISOLATED, f"the run was ended by the watch ({reason}: {why}): nothing more runs")
    outcome = record.get("outcome", "unknown")
    if record.get("by") == deadline.SWEEP:  # the cell itself ran to its end: its own result stands apart from this
        return InfraError(
            CELL_UNKILLABLE,
            f"the run was ended by the end-of-cell sweep after cell {record.get('cell')} ({outcome}): "
            f"{record.get('why')}; the cell's own result stands: nothing more runs",
        )
    if record.get("cell") is not None:  # the cell ran and hit its deadline: its own result stands, a timeout (O72)
        why = record.get("why") or "a process of the cell outlived its kill"
        return InfraError(
            CELL_UNKILLABLE,
            f"the run was ended at cell {record['cell']}'s deadline ({outcome}): {why}; "
            "the cell's own result stands: nothing more runs",
        )
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
    if kill.get("scope") == "run":  # the kill that ended the run because the cell could not be isolated (O72)
        kind = CELL_NOT_ISOLATED
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


def declared_deferral_from_config(path: "str | os.PathLike[str]") -> "DeferralBound | None":
    """The longest the platform's health check may stay deferred under the platform.toml at `path`
    (`fault_deferral.declared_from_file`): what the core reads through `[study] platforms`
    (`expcore.study.platform_deferral`), from the study's own file, to hold the campaign's `max_defer_s` and its health
    check to. None with the heal off or no `[faults]`. The platform layer is imported when this is called, so
    importing this module for its kinds stays cheap."""
    from expcore_onedata.fault_deferral import declared_from_file

    return declared_from_file(path)


__all__ = [
    "CELL_NOT_ISOLATED",
    "CELL_UNKILLABLE",
    "DATA_GATE_INTEGRITY",
    "FEDERATION_BUSY",
    "INGEST_FAILURE",
    "INJECTOR_FAILURE",
    "IO_MODE_INTEGRITY",
    "KIND_OF_CATEGORY",
    "OUTPUT_COLLECTION",
    "PROVIDER_OUTAGE",
    "collection_error",
    "deadline_error",
    "death_error",
    "declared_deferral_from_config",
    "gate_error",
    "kill_error",
    "kind_of",
    "run_ended_error",
]
