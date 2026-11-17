"""The HPC federation's services as Slurm shows them now: an observation, not the state file's word (O86).

    python -I -B hpcobserve.py --root <the federation's root>

prints one JSON line `{"format": "expcore-hpc-observe/1", "at", "services": {<service>: {...}}}` for the zone and each
provider the state file lists (the agent's node is no service of the federation). It runs where Slurm's `squeue` and
`sacct` answer (the login node), and only those two: it changes no job, no step and no file, and takes no lock.

The state names each service's step (`services.<n>.step`, its name and id, and its `launch` counter). That is where the
read starts, not what it reports: `squeue -s` lists the job's steps now, and a service whose own step name is listed
under another id than the state's is read as the step that is listed (`state_differs`). Each step is then read by its
EXACT id through `hpcslurm.Slurm.step_observed`: whether it runs, its start, its end and its exit.

Per service: `kind` (`oz` or `op`), `launch` (the state's counter, or None), `step` (the id read), `running` (true,
false, or null when it could not be told), `state`, `started_at`, `ended_at`, `exit_code`, `state_differs`, `stops`
(the stops the state records for the service: `requested_utc`, `confirmed_utc` and `stopped`, nothing else),
`unknown` (null, or why the step could not be told) and `accounting_wait_s_used` (seconds, 0 when the read did not
wait). A command that fails, a step id missing, accounting that does not settle it: `running` is null with the reason,
never true. The document names no node, address or path; the state's own words for them are never copied.

**Slurm's accounting trails a stop.** The driver confirms a stop once `squeue -s` no longer lists the step, and `sacct`
follows seconds later; a read in between finds the step neither listed nor recorded as ended. When the STATE says the
step was stopped (the service's `state` is `stopped`, its last recorded stop is a confirmed one, and the step the
state names is the one for its `launch`) and the read is of that very step but Slurm's two commands do not yet agree
that it ended (`hpcslurm.accounting_behind`), the read waits: bounded by `--accounting-wait-s` (default 60 s, at most
300, 0 for none), polling every 2 s, for every such service at once under the one bound, until the commands agree. Each
service records the seconds it waited. At the bound it reads `unknown` with the reason `accounting_lag`, and nothing
that claims the step ran or ended: never running, never stopped by assumption. It never waits for a service the state
does not say was stopped (a restart leaves `stops` behind, so a stop alone says nothing), for a step squeue lists
under another id than the state's, or for a command that failed: those keep their own readings and reasons.

Python 3.8 syntax, the standard library and the driver's own modules (`hpcslurm`, `hpcstate`) only.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import hpcslurm
import hpcstate

FORMAT = "expcore-hpc-observe/1"
KINDS = ("oz", "op")  # the services of the federation: the zone and the providers
LAG = "accounting_lag"  # the reason of a stopped step whose accounting did not catch up within the bound
POLL_S = 2.0  # between two reads of a stopped step that accounting is behind on
# Kept equal to `expcore_onedata.fedwatch.DEFAULT_ACCOUNTING_WAIT_S` and `MAX_ACCOUNTING_WAIT_S` (a test holds them).
DEFAULT_ACCOUNTING_WAIT_S = 60.0
MAX_ACCOUNTING_WAIT_S = 300.0
UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _why(error: Exception) -> str:
    """A Slurm error's first clause: the command and how it ended, never what it printed on stderr."""
    return str(error).split(":")[0]


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _stops(value: Any) -> List[Dict[str, Any]]:
    """The stops the state records for a service, reduced to when they were asked for and confirmed and whether they
    were: each field only when it is what a stop writes, so nothing else of the state (a node, a path) is carried."""
    found = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        found.append(
            {
                key: (item.get(key) if isinstance(item.get(key), str) and UTC.fullmatch(item[key]) else None)
                for key in ("requested_utc", "confirmed_utc")
            }
        )
        found[-1]["stopped"] = item.get("stopped") if isinstance(item.get("stopped"), bool) else None
    return found


def _unknown(
    reason: str, *, kind: str, launch: Optional[int], step: Optional[str], stops: List[Dict[str, Any]]
) -> Dict:
    return {
        "kind": kind,
        "launch": launch,
        "step": step,
        "running": None,
        "state": None,
        "started_at": None,
        "ended_at": None,
        "exit_code": None,
        "state_differs": False,
        "stops": stops,
        "unknown": reason,
    }


def _seconds(value: Any) -> float:
    """A bound for the wait, validated: a number of seconds from 0 to `MAX_ACCOUNTING_WAIT_S`, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_ACCOUNTING_WAIT_S:
        raise ValueError(
            "accounting_wait_s must be a number of seconds from 0 to %g, not %r" % (MAX_ACCOUNTING_WAIT_S, value)
        )
    return float(value)


def _service(
    record: Dict[str, Any], listed: Optional[Dict[str, str]], listing_error: Optional[str], slurm: hpcslurm.Slurm
) -> Tuple[Dict[str, Any], bool]:
    """One service's reading: the step the state names, or the one squeue lists under its name, read by its exact id.
    And whether the answer is one that Slurm's accounting being behind explains (`hpcslurm.accounting_behind`)."""
    step = record.get("step") if isinstance(record.get("step"), dict) else {}
    name, recorded = step.get("name"), step.get("id")
    common = {"kind": record["kind"], "launch": _count(record.get("launch")), "stops": _stops(record.get("stops"))}
    recorded = recorded if isinstance(recorded, str) else None
    if listing_error is not None:
        return _unknown(listing_error, step=recorded, **common), False
    listed_id = listed.get(name) if listed is not None and isinstance(name, str) else None
    observed = listed_id or recorded
    if observed is None:
        reason = "the state records no step id for the service and squeue lists none"
        return _unknown(reason, step=None, **common), False
    seen = slurm.step_observed(observed, name if isinstance(name, str) else None)
    doc = {
        **common,
        "step": observed,
        "running": seen["running"],
        "state": seen["state"],
        "started_at": seen["started_at"],
        "ended_at": seen["ended_at"],
        "exit_code": seen["exit_code"],
        "state_differs": listed_id is not None and listed_id != recorded,
        "unknown": seen["unknown"],
    }
    return doc, hpcslurm.accounting_behind(seen)


def _stopped(record: Dict[str, Any], doc: Dict[str, Any]) -> bool:
    """Whether the STATE says the step this reading is of was stopped: the service is `stopped`, its last recorded stop
    is a confirmed one, the step the state names is the one read (squeue lists no other under the service's name) and
    is the one of its `launch`. Stops alone say nothing: they stay in the state after a restart."""
    stops, step = record.get("stops"), record.get("step") if isinstance(record.get("step"), dict) else {}
    last = stops[-1] if isinstance(stops, list) and stops and isinstance(stops[-1], dict) else {}
    launch, name = _count(record.get("launch")), step.get("name")
    return (
        record.get("state") == "stopped"
        and last.get("stopped") is True
        and isinstance(step.get("id"), str)
        and doc["step"] == step["id"]
        and not doc["state_differs"]
        and launch is not None
        and isinstance(name, str)
        and name.endswith("-run-%d" % launch)
    )


def _lagged(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The reading of a stopped step whose accounting did not catch up in the bound: unknown, with nothing of it."""
    return _unknown(LAG, kind=doc["kind"], launch=doc["launch"], step=doc["step"], stops=doc["stops"])


def _round(
    records: Dict[str, Dict[str, Any]], state: Dict[str, Any], slurm: hpcslurm.Slurm
) -> Dict[str, Tuple[Dict[str, Any], bool]]:
    """One read of the named services: the job's steps listed once, then each service's step by its exact id."""
    job = state["allocation"].get("job")
    listed: Optional[Dict[str, str]] = None
    listing_error: Optional[str] = None
    if not isinstance(job, str):
        listing_error = "the state records no held job"
    else:
        try:
            listed = slurm.steps(job)
        except hpcslurm.SlurmError as error:
            listing_error = _why(error)
    return {name: _service(record, listed, listing_error, slurm) for name, record in sorted(records.items())}


def read(
    state: Dict[str, Any],
    slurm: hpcslurm.Slurm,
    now: Callable[[], float] = time.time,
    *,
    accounting_wait_s: float = DEFAULT_ACCOUNTING_WAIT_S,
) -> Dict[str, Any]:
    """The document for a loaded state (`hpcstate.load`): every service of kind `oz` or `op`, read now, and, for a
    service the state says was stopped whose step Slurm's commands do not yet agree has ended, read again every
    `POLL_S` (on `slurm`'s own `sleep` and `clock`) until they agree or `accounting_wait_s` has passed in all."""
    bound = _seconds(accounting_wait_s)
    records = {
        name: record
        for name, record in state["services"].items()
        if isinstance(record, dict) and record.get("kind") in KINDS
    }
    found = _round(records, state, slurm)
    used = dict.fromkeys(records, 0.0)
    owing = {name for name, (doc, behind) in found.items() if behind and _stopped(records[name], doc)}
    began = slurm.clock()
    elapsed = 0.0
    while owing and elapsed < bound:
        slurm.sleep(min(POLL_S, bound - elapsed))
        again = _round({name: records[name] for name in owing}, state, slurm)
        elapsed = round(slurm.clock() - began, 3)
        for name, (doc, behind) in again.items():
            found[name] = (doc, behind)
            used[name] = elapsed
            if not (behind and _stopped(records[name], doc)):
                owing.discard(name)
    for name in owing:  # still behind at the bound: neither running nor stopped by assumption
        found[name] = (_lagged(found[name][0]), False)
    services = {name: {**doc, "accounting_wait_s_used": used[name]} for name, (doc, _) in found.items()}
    return {"format": FORMAT, "at": _utc(now()), "services": services}


def _bound(text: str) -> float:
    """`--accounting-wait-s`'s value: a number of seconds from 0 to `MAX_ACCOUNTING_WAIT_S`."""
    try:
        return _seconds(float(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main(
    argv: Sequence[str],
    *,
    runner: Optional[Any] = None,
    out: Optional[Any] = None,
    err: Optional[Any] = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Print the document as one JSON line; exit 1, with a message that names no path, when the state cannot be read."""
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="hpcobserve.py")
    parser.add_argument("--root", required=True, help="the federation's root, which holds its state file")
    parser.add_argument(
        "--accounting-wait-s",
        type=_bound,
        default=DEFAULT_ACCOUNTING_WAIT_S,
        help="the longest, in seconds (0 to %g), to wait for Slurm's accounting to show a step the state says was "
        "stopped (default %g)" % (MAX_ACCOUNTING_WAIT_S, DEFAULT_ACCOUNTING_WAIT_S),
    )
    arguments = parser.parse_args(list(argv))
    try:
        state = hpcstate.load(Path(arguments.root))
    except (hpcstate.StateError, OSError):
        state = None
    if state is None:
        err.write("hpcobserve: the federation's state could not be read\n")
        err.flush()
        return 1
    slurm = hpcslurm.Slurm(runner or hpcslurm.Runner(), command_timeout_s=60.0, sleep=sleep, clock=clock)
    document = read(state, slurm, now, accounting_wait_s=arguments.accounting_wait_s)
    out.write(json.dumps(document, sort_keys=True) + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
