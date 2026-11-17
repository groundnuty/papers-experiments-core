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
(the stops the state records for the service: `requested_utc`, `confirmed_utc` and `stopped`, nothing else) and
`unknown` (null, or why the step could not be told). A command that fails, a step id missing, accounting that does not
settle it: `running` is null with the reason, never true. The document names no node, address or path; the state's
own words for them are never copied.

Python 3.8 syntax, the standard library and the driver's own modules (`hpcslurm`, `hpcstate`) only.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import hpcslurm
import hpcstate

FORMAT = "expcore-hpc-observe/1"
KINDS = ("oz", "op")  # the services of the federation: the zone and the providers
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


def _service(
    record: Dict[str, Any], listed: Optional[Dict[str, str]], listing_error: Optional[str], slurm: hpcslurm.Slurm
) -> Dict[str, Any]:
    """One service's reading: the step the state names, or the one squeue lists under its name, read by its exact id."""
    step = record.get("step") if isinstance(record.get("step"), dict) else {}
    name, recorded = step.get("name"), step.get("id")
    common = {"kind": record["kind"], "launch": _count(record.get("launch")), "stops": _stops(record.get("stops"))}
    recorded = recorded if isinstance(recorded, str) else None
    if listing_error is not None:
        return _unknown(listing_error, step=recorded, **common)
    listed_id = listed.get(name) if listed is not None and isinstance(name, str) else None
    observed = listed_id or recorded
    if observed is None:
        return _unknown("the state records no step id for the service and squeue lists none", step=None, **common)
    seen = slurm.step_observed(observed, name if isinstance(name, str) else None)
    return {
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


def read(state: Dict[str, Any], slurm: hpcslurm.Slurm, now: Callable[[], float] = time.time) -> Dict[str, Any]:
    """The document for a loaded state (`hpcstate.load`): every service of kind `oz` or `op`, read now."""
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
    services = {
        name: _service(record, listed, listing_error, slurm)
        for name, record in sorted(state["services"].items())
        if isinstance(record, dict) and record.get("kind") in KINDS
    }
    return {"format": FORMAT, "at": _utc(now()), "services": services}


def main(
    argv: Sequence[str],
    *,
    runner: Optional[Any] = None,
    out: Optional[Any] = None,
    err: Optional[Any] = None,
    now: Callable[[], float] = time.time,
) -> int:
    """Print the document as one JSON line; exit 1, with a message that names no path, when the state cannot be read."""
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="hpcobserve.py")
    parser.add_argument("--root", required=True, help="the federation's root, which holds its state file")
    arguments = parser.parse_args(list(argv))
    try:
        state = hpcstate.load(Path(arguments.root))
    except (hpcstate.StateError, OSError):
        state = None
    if state is None:
        err.write("hpcobserve: the federation's state could not be read\n")
        err.flush()
        return 1
    slurm = hpcslurm.Slurm(runner or hpcslurm.Runner(), command_timeout_s=60.0)
    out.write(json.dumps(read(state, slurm, now), sort_keys=True) + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
