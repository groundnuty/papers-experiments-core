"""fleetobserve.py: one read of a site for the elastic fleet's controller (#462 step 5, C45; README.md, "The elastic
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
- one federation's nodes (the layout's roles: services and the agent's node) and its `--time`;
- each fault relay its member's up recorded serving: its heartbeat file in the root
  (`<root>/fault-relay/heartbeat.json`, on the shared filesystem) and whether the process it names (pid and start
  tick) is one of this login node's. A heartbeat older than the relay's own staleness bound, or none, is `gone`; a
  live one is `here` or `elsewhere` (relays stay on the login node their up ran on, and the controller budgets this
  login node's memory).

A job read that fails, or a job state that is neither waiting, running nor ended, fails the whole read: unknown is
never taken for ended. A quota that cannot be read is reported (`inodes_error`) and leaves no room for an add.
"""

import json
import os
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
from fleetrecord import HOLDS_NOTHING, LINKED, Record, identity, jobs_of, root_of, sandbox_name, slot_of  # noqa: E402
from hpcsettings import agent_limits, load_settings, site_from_environment  # noqa: E402
from hpcslurm import ENDED_STATES, SLURM_TIME, WAITING_STATES, Client, Slurm, SlurmError  # noqa: E402

GONE = "GONE"  # `expcore.campaign.elastic.control.GONE`
BROKEN = "BROKEN"  # `expcore.campaign.elastic.controlinputs.BROKEN`: some jobs ended or ending, others not
ENDING = ("COMPLETING",)  # ending now: not yet ended, and no longer usable
NODE_LINE = re.compile(r"([a-z_]+[^\s]*) ([0-9]{1,9})")
SINFO_TIMEOUT_S = 60.0
RELAY_DIR, HEARTBEAT = "fault-relay", "heartbeat.json"  # `expcore_onedata.fault_relay.relay_dir`, its heartbeat
RELAY_STALE_S = 30.0  # `expcore_onedata.fault_relay.RELAY_STALE_S`: older, the relay is not answering


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
    """A member's jobs as one: an ended state only when every job ended (only then is its end proven: its agent's job
    may still run, and spend, beside services that ended); BROKEN when some ended or are ending and others not, or one
    is still ending; else a waiting state while any waits; else RUNNING. The earliest submit, the latest start once
    all started, the earliest end."""
    if not views:
        return {"state": None, "submitted_at": None, "started_at": None, "ends_at": None}
    states = [view["state"] for view in views]
    ended = [s for s in states if s in (*ENDED_STATES, GONE)]
    waiting = [s for s in states if s in WAITING_STATES]
    if len(ended) == len(states):
        state = ended[0]
    elif ended or any(s in ENDING for s in states):
        state = BROKEN
    else:
        state = waiting[0] if waiting else "RUNNING"
    times = {key: [view[key] for view in views if view.get(key) is not None]
             for key in ("submitted_at", "started_at", "ends_at")}  # fmt: skip
    all_started = len(times["started_at"]) == len(views)
    return {"state": state, "submitted_at": min(times["submitted_at"], default=None),
            "started_at": max(times["started_at"]) if all_started and times["started_at"] else None,
            "ends_at": min(times["ends_at"], default=None)}  # fmt: skip


def verified(state: Mapping[str, Any]) -> bool:
    """Every ingested space of the member verified by `hpcfed.py verify-data` (True when it ingests none): a marker
    bound to the manifest and the space `up` recorded, as `shards.check_ingested` reads one. A state with no record of
    its ingested spaces (an older up's) is never verified, so its inodes stay reserved."""
    data = state.get("data")
    spaces = data.get("ingested") if isinstance(data, Mapping) else None
    if not isinstance(spaces, Mapping):
        return False
    for record in spaces.values():
        marker = record.get("verified") if isinstance(record, Mapping) else None
        if not isinstance(marker, Mapping) or marker.get("manifest") != record.get("manifest"):
            return False
        if not isinstance(marker.get("space_id"), str) or marker.get("space_id") != record.get("space_id"):
            return False
    return True


def heartbeat(root: Path) -> Optional[Dict[str, Any]]:
    """The relay's last heartbeat record: a JSON object in a file of ours that is no link; None when there is none."""
    path = root / RELAY_DIR / HEARTBEAT
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            return None
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def relay_view(root: Path, state: Optional[Mapping[str, Any]], runner: Any, wall: float) -> Tuple[str, Optional[float]]:
    """The member's fault relay: `none` unless its up recorded it serving; `gone` with no live heartbeat; else `here`
    when the process the heartbeat names (pid and start tick) is this login node's, `elsewhere` when not. With the
    last heartbeat's wall time."""
    if not state or hpcstate.relay_of(dict(state)) != "serving":
        return "none", None
    record = heartbeat(root) or {}
    at = record.get("wall")
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    if at is None or abs(wall - at) > RELAY_STALE_S:
        return "gone", at
    pid, start = record.get("pid"), record.get("start")
    start = (
        int(start) if isinstance(start, str) and start.isdigit() else start
    )  # the relay writes /proc's field as text
    whole = isinstance(pid, int) and isinstance(start, int) and not isinstance(start, bool)
    ours = whole and runner.client_alive(Client(pid, start))
    return ("here" if ours else "elsewhere"), at


def snapshot_reason(member: Mapping[str, Any], state: Optional[Mapping[str, Any]], root: Path) -> Optional[str]:
    """Why the state file read for the jobs is not the federation this fleet's up made (it may have been replaced since
    `identity` read it): written by an up of another root, another held job's name, or other jobs than the record's;
    None when it is the one (or there is none yet)."""
    if state is None:
        return None
    words = [str(word) for word in state.get("command") or []]
    if "--root" not in words[:-1] or words[words.index("--root") + 1] != str(root):
        return "its state file was not written by an up of this root"
    name = (state.get("allocation") or {}).get("name")
    if member.get("name") is not None and name != member["name"]:
        return "its state file names another held job"
    found, recorded = jobs_of(dict(state)), member.get("jobs") or {}
    if any(recorded.get(key) is not None and found.get(key) != recorded[key] for key in ("allocation", "sandbox")):
        return "its state file names other jobs than the record"
    return None


def member_view(directory: Path, member: Mapping[str, Any], slurm: Slurm, wall: float) -> Dict[str, Any]:
    """One member: its record's status, its root, its state file's phase and jobs, and each job as Slurm has it."""
    label, status = member["label"], member["status"]
    root = root_of(directory, label)
    view = {"label": label, "status": status, "root": str(root), "jobs": [], "linked": False, "verified": False}
    if status in HOLDS_NOTHING:  # taken or down hold nothing; a claimed slot is not made yet: nothing to read
        return {**view, **combined([])}
    reason, _name = identity(directory, dict(member))  # the fleet's own check: its root, its state, its jobs
    if reason is not None:
        raise ObserveError(f"a member is not the federation this fleet made ({reason})")
    try:
        state = hpcstate.load(root) if root.is_dir() else None
    except hpcstate.StateError:
        raise ObserveError(f"{label}: its state file cannot be read") from None
    changed = snapshot_reason(member, state, root)  # the state read here, checked as `identity` checked its own read
    if changed is not None:
        raise ObserveError(f"a member is not the federation this fleet made ({label}: {changed})")
    name = (state or {}).get("allocation", {}).get("name") or member.get("name")
    jobs = {**{k: v for k, v in (member.get("jobs") or {}).items() if v}, **{k: v for k, v in
            jobs_of(state or {}).items() if v}}  # fmt: skip
    sandbox = (state or {}).get("sandbox") or {}
    derived = None if name is None else sandbox_name(name, sandbox.get("placement") or "separate")
    names = {"allocation": name, "sandbox": sandbox.get("name") or derived}
    views = [job_view(slurm, job, names.get(key)) for key, job in sorted(jobs.items())]
    view.update(jobs=sorted(set(jobs.values())), linked=(state or {}).get("phase") == LINKED)
    view["verified"] = bool(state) and verified(state)
    view["relay"], view["relay_beat_at"] = relay_view(root, state, slurm.runner, wall)
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


def observe(directory: Path, environ: Mapping[str, str], runner: Any, wall: float) -> Dict[str, Any]:
    """One read of the site: what `expcore.campaign.elastic.control.SiteView` takes, less its label, as JSON."""
    record = Record(directory).read()
    site = site_from_environment(environ)
    layout, nodes, time_s = federation_shape(directory, record)
    slurm = Slurm(runner)
    members = [member_view(directory, member, slurm, wall) for member in record["members"]]
    idle, mixed = read_nodes(runner, site.partition)
    return {"idle_nodes": idle, "mixed_nodes": mixed, "nodes_per_federation": nodes, "time_s": float(time_s),
            **inodes(runner, site, layout, environ), "members": members}  # fmt: skip
