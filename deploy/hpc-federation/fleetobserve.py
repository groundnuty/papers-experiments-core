"""fleetobserve.py: one read of a site for the elastic fleet's controller (#462 step 5, C43; README.md, "The elastic
fleet's controller"), run on the site's login node by `fleetcontrol.py observe --fleet DIR`, with `site.env` exported.

It reads, and never writes, submits or cancels:

- the fleet's record (`fleet.json`) and each member's state file: its status, root, phase (linked once its up
  finished), its held jobs' ids and names, and whether each ingested space is verified (`hpcfed.py verify-data`);
- each held job of a member that may hold one, by the id its up recorded, with `scontrol show job -o <id>`: its state,
  submit, start and end. A job Slurm no longer knows, or whose id now names another job (Slurm reuses ids), is `GONE`:
  ours ended. Times are Slurm's zone-less local times, read on the login node, whose zone is the cluster's;
- the partition's idle and mixed node counts (`sinfo -h -p <partition> -o "%T %D"`): under `--exclusive` a federation
  takes whole nodes, so only an exactly `idle` node is free (a mixed one, or one powered down or not responding, is
  not);
- the account's inode usage and quota (`hpcquota.read_quota`, the guard's own read) and one federation's estimate
  (`hpcquota.estimate_inodes`, the guard's own), when the providers' storage is on scratch (`HPC_STORAGE_ROOT`);
- one federation's nodes (the layout's roles: services and the agent's node) and its `--time`.

A job read that fails, or a job state that is neither waiting, running nor ended, fails the whole read: unknown is
never taken for ended. A quota that cannot be read is reported (`inodes_error`) and leaves no room for an add.
"""

import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import hpcdata  # noqa: E402
import hpcquota  # noqa: E402
import hpcstate  # noqa: E402
from fedlayout import Layout  # noqa: E402
from fedspec import load_spec  # noqa: E402
from fleetrecord import HOLDS_NOTHING, LINKED, Record, jobs_of, root_of, sandbox_name, slot_of  # noqa: E402
from hpcsettings import agent_limits, load_settings, site_from_environment  # noqa: E402
from hpcslurm import ENDED_STATES, SLURM_TIME, WAITING_STATES, Slurm, SlurmError  # noqa: E402

GONE = "GONE"  # `expcore.campaign.elastic.control.GONE`
ENDING = ("COMPLETING",)  # ending now: as good as ended for the controller
NODE_LINE = re.compile(r"([a-z_]+[^\s]*) ([0-9]{1,9})")
SINFO_TIMEOUT_S = 60.0


class ObserveError(RuntimeError):
    """The site could not be read as a whole; the message names why, never a host or a path."""


def slurm_epoch(value: Optional[str]) -> Optional[float]:
    """A Slurm time (`2026-11-18T10:00:00`, zone-less, the cluster's local time) as seconds since the epoch; None for
    `Unknown`, `None` or nothing."""
    if not value or not SLURM_TIME.fullmatch(value):
        return None
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%S"))


def read_nodes(runner: Any, partition: str) -> Tuple[int, int]:
    """The partition's (idle, mixed) node counts: only a state that is exactly `idle` or `mixed` counts (`idle~`, a
    powered-down node, and `idle*`, one not responding, are neither)."""
    done = runner.run(["sinfo", "-h", "-p", partition, "-o", "%T %D"], SINFO_TIMEOUT_S)
    if done.returncode != 0:
        raise ObserveError(f"sinfo failed with status {done.returncode}")
    idle = mixed = 0
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        found = NODE_LINE.fullmatch(line.strip())
        if found is None:
            raise ObserveError("sinfo printed a line that is not a state and a node count")
        state, count = found.group(1), int(found.group(2))
        idle += count if state == "idle" else 0
        mixed += count if state == "mixed" else 0
    return idle, mixed


def job_view(slurm: Slurm, job: str, name: Optional[str]) -> Dict[str, Any]:
    """One held job as Slurm records it now: its state (first word), submit, start and end; `GONE` when Slurm no
    longer knows it or the id names a job of another name (never ours: never touched)."""
    try:
        found = slurm.job(job)
    except (SlurmError, OSError, ValueError, StopIteration) as error:
        raise ObserveError(f"a held job could not be read ({type(error).__name__})") from None
    if found is None or (name is not None and found.get("JobName") != name):
        return {"state": GONE}
    state = (found.get("JobState") or "").split(" ")[0]
    if state not in (*WAITING_STATES, "RUNNING", *ENDED_STATES, *ENDING):
        raise ObserveError(f"a held job is in a state the controller does not read ({state or 'none'})")
    started = None if state in WAITING_STATES else slurm_epoch(found.get("StartTime"))
    return {"state": state, "submitted_at": slurm_epoch(found.get("SubmitTime")), "started_at": started,
            "ends_at": slurm_epoch(found.get("EndTime"))}  # fmt: skip


def combined(views: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A member's jobs as one: RUNNING when every one runs; a waiting state while any waits; else the first that ended
    (one ended job ends the federation); the earliest submit, the latest start once all started, the earliest end."""
    if not views:
        return {"state": None, "submitted_at": None, "started_at": None, "ends_at": None}
    states = [view["state"] for view in views]
    ended = [s for s in states if s not in (*WAITING_STATES, "RUNNING")]
    waiting = [s for s in states if s in WAITING_STATES]
    state = ended[0] if ended else waiting[0] if waiting else "RUNNING"
    times = {key: [view[key] for view in views if view.get(key) is not None]
             for key in ("submitted_at", "started_at", "ends_at")}  # fmt: skip
    all_started = len(times["started_at"]) == len(views)
    return {"state": state, "submitted_at": min(times["submitted_at"], default=None),
            "started_at": max(times["started_at"]) if all_started and times["started_at"] else None,
            "ends_at": min(times["ends_at"], default=None)}  # fmt: skip


def verified(state: Mapping[str, Any]) -> bool:
    """Every ingested space of the member verified by `hpcfed.py verify-data` (True when it ingests none)."""
    spaces = ((state.get("data") or {}).get("ingested") or {}).values()
    return all(space.get("verified") is not None for space in spaces)


def member_view(directory: Path, member: Mapping[str, Any], slurm: Slurm) -> Dict[str, Any]:
    """One member: its record's status, its root, its state file's phase and jobs, and each job as Slurm has it."""
    label, status = member["label"], member["status"]
    root = root_of(directory, label)
    view = {"label": label, "status": status, "root": str(root), "jobs": [], "linked": False, "verified": False}
    if status in HOLDS_NOTHING and status != "claimed":
        return {**view, **combined([])}
    try:
        state = hpcstate.load(root) if root.is_dir() else None
    except hpcstate.StateError:
        raise ObserveError(f"{label}: its state file cannot be read") from None
    name = (state or {}).get("allocation", {}).get("name") or member.get("name")
    jobs = {**{k: v for k, v in (member.get("jobs") or {}).items() if v}, **{k: v for k, v in
            jobs_of(state or {}).items() if v}}  # fmt: skip
    sandbox = (state or {}).get("sandbox") or {}
    derived = None if name is None else sandbox_name(name, sandbox.get("placement") or "separate")
    names = {"allocation": name, "sandbox": sandbox.get("name") or derived}
    views = [job_view(slurm, job, names.get(key)) for key, job in sorted(jobs.items())]
    view.update(jobs=sorted(set(jobs.values())), linked=(state or {}).get("phase") == LINKED)
    view["verified"] = bool(state) and verified(state)
    return {**view, **combined(views)}


def federation_shape(directory: Path, record: Mapping[str, Any]) -> Tuple[Any, int, int]:
    """The fleet's layout: (one federation's Layout, for the inode estimate alone; its nodes, the services' and the
    agent's; its `--time` in seconds)."""
    spec = load_spec(Path(record["layout"]["path"]))
    settings = load_settings(spec.hpc, agent_limits(spec.platform_template))
    nodes = len(settings.roles(tuple(provider.name for provider in spec.providers)))
    return Layout(root_of(directory, "fed-1"), slot_of(directory, "fed-1"), spec), nodes, settings.time_s


def inodes(runner: Any, site: Any, layout: Any, environ: Mapping[str, str]) -> Dict[str, Any]:
    """The account's inode usage and quota and one federation's estimate, when its storage is on scratch."""
    if not site.storage_root:
        return {"inodes_used": None, "inodes_quota": None, "inode_estimate": 0, "inodes_error": None}
    values = hpcdata.study_values(site.study_artifacts)
    estimate = sum(hpcquota.estimate_inodes(layout, environ, values).values())
    try:
        used, quota = hpcquota.read_quota(runner, site)
    except hpcquota.QuotaRefused as error:
        return {"inodes_used": None, "inodes_quota": None, "inode_estimate": estimate, "inodes_error": str(error)}
    return {"inodes_used": used, "inodes_quota": quota, "inode_estimate": estimate, "inodes_error": None}


def observe(directory: Path, environ: Mapping[str, str], runner: Any) -> Dict[str, Any]:
    """One read of the site: what `expcore.campaign.elastic.control.SiteView` takes, less its label, as JSON."""
    record = Record(directory).read()
    site = site_from_environment(environ)
    layout, nodes, time_s = federation_shape(directory, record)
    slurm = Slurm(runner)
    members = [member_view(directory, member, slurm) for member in record["members"]]
    idle, mixed = read_nodes(runner, site.partition)
    return {"idle_nodes": idle, "mixed_nodes": mixed, "nodes_per_federation": nodes, "time_s": float(time_s),
            **inodes(runner, site, layout, environ), "members": members}  # fmt: skip
