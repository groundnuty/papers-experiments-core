"""shardresume.py: `shards.py resume-line`, a shard's step line with `--resume` in a held job of the operator's, on a
node of that job (live slot 5: a shard killed outright on the agent's node was resumed on a node outside the
federation, by a line edited by hand that nothing checked). Login node only; `shards.py` loads it by its path.

    <python> -I -B deploy/hpc-federation/shards.py resume-line --manifest shards.toml --label L --job J --node N

It prints the line only after every check passed, each refusal naming the shard's label (`ShardsError`, exit 2):

- **The job**: J is a job of one component and one node, N, RUNNING, this user's (its `UserId`), not the services' job
  of any federation of the manifest, with the manifest's cores and memory free on it: its own less those of its steps
  still running (`sacct`'s `AllocTRES`; batch and extern steps left out; anything unread fails it). Accounting lags,
  so a step that started a moment ago may be missed: such a line's step then waits for its cores, it never shares them.
- **No live step of the shard anywhere**: no step of this user's (`squeue -s -u`) carries the shard's step name, and
  its running mark (`<results>/<label>/running.json`, kept by every step from its start to its own end) is absent or
  names a step no longer listed. The line then carries `--over <the mark's nonce>`, the proof the step needs to take
  that mark over on another node (`shards.claim_running`); a second launch of the line finds the mark taken.
- **The same campaign**: the newest snapshot that verifies (`shards.restore_newest`, restored into a private copy
  here: every file against its hash, the store's integrity, every ended attempt's marker) is the shard's, names the
  archive its results hold (the archive is not newer), and its store pins the campaign fingerprint the study pins
  with this federation's `platform.toml` (computed by the manifest's Python, `expcore.study.pipeline.
  campaign_fingerprint`, on a copy of the study beside that file, as the step places it), which every other shard's
  newest snapshot or archive pins too. A resume against another federation, image or data is refused before a node is
  taken.
- **Cleanup follows the step**: J and N are recorded in the federation's state file (`shard_nodes`, under the root's
  lifecycle lock: never beside an up or a down), before the line is printed, so its `down` sweeps N too, in J.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
COMMAND_TIMEOUT_S = 60.0
FINGERPRINT_TIMEOUT_S = 600.0
FINGERPRINT = """import json, sys
from expcore.study.pipeline import campaign_fingerprint
print(json.dumps(campaign_fingerprint(sys.argv[1]), sort_keys=True))
"""
SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)")
UNITS = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 * 1024}  # in MiB; Slurm's default unit is MiB


@contextlib.contextmanager
def _driver_modules() -> Iterator[tuple[Any, Any, Any]]:
    """The federation driver's modules, by their plain names as `hpcfed.py` imports them, with this directory on
    `sys.path` only while they are imported: it holds a `facts/` directory a later import of that name would find."""
    added = str(HERE) not in sys.path
    if added:
        sys.path.insert(0, str(HERE))
    try:
        import hpclaunch
        import hpcslurm
        import hpcstate
    finally:
        if added:
            sys.path.remove(str(HERE))
    yield hpclaunch, hpcslurm, hpcstate


def mib(text: str) -> float | None:
    """A Slurm size (`18G`, `17408M`, `1.5G`, `17g`) in MiB; None for anything else, or nothing."""
    found = SIZE.fullmatch(str(text).strip().upper())
    return None if found is None else float(found.group(1)) * UNITS[found.group(2)]


def steps_use(runner: Any, job: str, refuse: Any) -> tuple[int, float]:
    """The cores and MiB the job's steps still running hold, from the accounting: every numbered step (`J.<n>`) that
    is RUNNING, by its `AllocTRES`; one that cannot be read refuses."""
    done = runner.run(["sacct", "-j", job, "-n", "-P", "-o", "JobID,State,AllocTRES"], COMMAND_TIMEOUT_S)
    if done.returncode != 0:
        refuse(f"the accounting of job {job} could not be read (sacct status {done.returncode})")
    cpus, memory = 0, 0.0
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split("|")
        if len(parts) != 3:
            refuse(f"the accounting of job {job} is not JobID|State|AllocTRES")
        ident, state, tres = parts
        if not re.fullmatch(rf"{re.escape(job)}\.[0-9]+", ident) or state.split()[0] != "RUNNING":
            continue  # the job itself, its batch and extern steps, and steps that ended
        held = dict(item.split("=", 1) for item in tres.split(",") if "=" in item)
        size = mib(held.get("mem", ""))
        if not held.get("cpu", "").isdigit() or size is None:
            refuse(f"step {ident}'s cores and memory could not be read from the accounting")
        cpus, memory = cpus + int(held["cpu"]), memory + size
    return cpus, memory


def check_job(
    slurm: Any, errors: tuple, plan: Any, job: str, node: str, services: Mapping[str, str], refuse: Any
) -> str:
    """The checks of J (the module's docstring); its name, which `down` checks before it steps into it."""
    if not re.fullmatch(r"[0-9]{1,12}", job):
        refuse("--job takes one Slurm job id")
    if job in services:
        refuse(f"job {job} is the services' job of federation {services[job]}, never a shard's")
    try:
        records = slurm.records(job)
        if records is None:
            refuse(f"job {job} is not known to Slurm")
        if len(records) != 1 or any("HetJobId" in record for record in records):
            refuse(f"job {job} is heterogeneous: resume-line takes a job of one component")
        record = records[0]
        if record.get("JobState") != "RUNNING":
            refuse(f"job {job} is {record.get('JobState')}, not RUNNING")
        owner = re.fullmatch(r".*\(([0-9]+)\)", record.get("UserId", ""))
        if owner is None or int(owner.group(1)) != os.getuid():
            refuse(f"job {job} is not this user's")
        if record.get("JobName") in services.values():
            refuse(f"job {job} carries the name of a federation's services' job")
        if record.get("NumNodes") != "1" or slurm.hostnames(record.get("NodeList", "")) != [node]:
            refuse(f"job {job} does not hold node {node} alone: resume-line takes a job of that one node")
    except errors as error:
        refuse(f"job {job} could not be read ({error})")
    cpus, memory = record.get("NumCPUs", ""), mib(record.get("MinMemoryNode", ""))
    if not cpus.isdigit() or not memory:
        refuse(f"job {job}'s cores and memory could not be read")
    used_cpus, used_memory = steps_use(slurm.runner, job, refuse)
    if int(cpus) - used_cpus < plan.cpus or memory - used_memory < (mib(plan.memory) or float("inf")):
        refuse(f"job {job} has not the manifest's {plan.cpus} cores and {plan.memory} free (its steps hold "
               f"{used_cpus} of {cpus} cores and {used_memory:.0f} of {memory:.0f} MiB)")  # fmt: skip
    return str(record["JobName"])


def listed_steps(runner: Any, user: str, refuse: Any) -> dict[str, list[str]]:
    """Every step of this user's that `squeue -s` lists, by its name: the ids it is listed under."""
    done = runner.run(["squeue", "-s", "-u", user, "-h", "-o", "%i %j"], COMMAND_TIMEOUT_S)
    if done.returncode != 0:
        refuse(f"this user's steps could not be listed (squeue status {done.returncode})")
    found: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        words = line.split()
        if len(words) == 2:
            found.setdefault(words[1], []).append(words[0])
    return found


def check_no_live_step(
    shards: Any, slurm: Any, errors: tuple, plan: Any, shard: Any, name: str, user: str, refuse: Any
) -> str | None:
    """No step of the shard listed anywhere, and its running mark, when there is one, naming no step still listed:
    that mark's nonce (the step's `--over`), or None when there is none."""
    listed = listed_steps(slurm.runner, user, refuse)
    if name in listed:
        refuse(f"a step of it runs: {name} is listed as {', '.join(listed[name])}")
    path = plan.results / shard.label / shards.RUNNING_MARK
    if not os.path.lexists(path):
        return None
    spec = {"index": shard.index, "count": shard.count, "label": shard.label}
    found = shards._record(path)
    if found is None or found.get("format") != shards.RUNNING_FORMAT or found.get("shard") != spec:
        refuse(f"its running mark ({path.name}) cannot be read as this shard's")
    job, step, nonce = found.get("job"), found.get("step"), found.get("nonce")
    if not (isinstance(job, str) and isinstance(step, str) and isinstance(nonce, str) and nonce):
        refuse(f"its running mark names no step to show gone: remove {path.name} by hand once its step is shown gone")
    try:
        still = step in listed or step in slurm.steps(job)
    except errors as error:
        refuse(f"the step its running mark names could not be looked up ({error})")
    if still:
        refuse(f"the step its running mark names ({step}, on {found.get('node')}) is still listed")
    return nonce


def study_fingerprint(
    shards: Any, plan: Any, shard: Any, scratch: Path, refuse: Any, out: Path | None = None
) -> dict[str, Any]:
    """The campaign fingerprint the study pins beside this federation's `platform.toml`, by the manifest's Python on a
    private copy, with the step's own environment (the allowlist, a home of its own), and with `out` (a restored
    snapshot's) moved in as the copy's `out/`, as the step places it: what the step's dispatcher would pin."""
    copy = scratch / "study"
    shutil.copytree(plan.study, copy, symlinks=True)
    platform = shard.root / shards.PLATFORM
    if platform.is_file():
        shutil.copyfile(platform, copy / shards.PLATFORM)
    if out is not None:
        shutil.move(out, copy / "out")
    for name in ("home", "tmp", *(f"home/{sub}" for sub in shards.HOME_DIRS)):
        (scratch / name).mkdir(mode=0o700)
    env = {"PATH": plan.step_path, "LC_ALL": "C", "EXPCORE_FED_ROOT_PARENT": str(shard.root.parent),
           "EXPCORE_FED_ROOT": str(shard.root), **shards.home_env(str(scratch / "home")),
           "TMPDIR": str(scratch / "tmp")}  # fmt: skip
    try:
        done = subprocess.run([plan.python, "-I", "-B", "-c", FINGERPRINT, str(copy)], cwd=scratch, env=env,
                              capture_output=True, text=True, timeout=FINGERPRINT_TIMEOUT_S, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as error:
        refuse(f"the study's fingerprint could not be computed ({type(error).__name__})")
    if done.returncode != 0:
        refuse(f"the study's fingerprint could not be computed with its federation's {shards.PLATFORM} (status "
               f"{done.returncode}): {done.stderr.strip()[-400:]}")  # fmt: skip
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        refuse("the study's fingerprint was not printed whole")


def pinned_of(shards: Any, plan: Any, other: Any, scratch: Path) -> str | None:
    """The SHA-256 of the campaign fingerprint another shard pins: its newest snapshot's (the manifest records it), or
    its archive's store; None when it has neither (not started); "unknown" when what it has cannot be read."""
    from expcore.campaign import out_snapshot
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore.campaign.resume import read_fingerprint

    spec = {"index": other.index, "count": other.count, "label": other.label}
    for snapshot in out_snapshot.snapshots(plan.results / other.label / shards.SNAPSHOTS):
        try:
            manifest = out_snapshot.manifest_of(snapshot)
        except out_snapshot.OutSnapshotError:
            continue
        if manifest["facts"].get("shard") == spec:
            return manifest.get("campaign") or "unknown"
    if not (plan.results / f"{other.label}.out.tar").exists():
        return None
    try:
        tar_path, _ = shards.verified(plan.results, other, scratch)
        out = shards.unpack(tar_path, scratch / f"other-{other.index}")
        pinned = read_fingerprint(out / out_snapshot.STORE)
    except (shards.ShardsError, OSError, ValueError):
        return "unknown"
    return "unknown" if pinned is None else fingerprint_sha256(pinned)


def check_campaign(shards: Any, plan: Any, shard: Any, refuse: Any) -> None:
    """The newest verified snapshot is the shard's, names its archive, and pins the study's fingerprint with this
    federation's platform file, which every other shard pins too."""
    from expcore.campaign import out_snapshot
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore.campaign.resume import read_fingerprint

    spec = {"index": shard.index, "count": shard.count, "label": shard.label}
    scratch = Path(tempfile.mkdtemp(prefix="expcore-resume-line."))  # 0700, on this host: never the shared one
    try:
        base = shards.archive_named(plan.results, shard.label)
        snapshot = shards.restore_newest(plan.results, shard.label, spec, base, scratch / "out")
        if snapshot is None:
            refuse("no snapshot of it verifies that is newer than its archive: resume-line continues a shard killed "
                   "outright from its newest snapshot")  # fmt: skip
        pinned = read_fingerprint(scratch / "out" / out_snapshot.STORE)
        if pinned is None:
            refuse(f"snapshot {snapshot.name}'s store pins no campaign fingerprint")
        want = fingerprint_sha256(pinned)
        if fingerprint_sha256(study_fingerprint(shards, plan, shard, scratch, refuse, scratch / "out")) != want:
            refuse(f"snapshot {snapshot.name} pins another campaign than the study with its federation's "
                   f"{shards.PLATFORM} (another federation, image or data)")  # fmt: skip
        for other in plan.shards:
            found = None if other.label == shard.label else pinned_of(shards, plan, other, scratch)
            if found is not None and found != want:
                refuse(f"it pins another campaign than shard {other.label}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def record_node(hpclaunch: Any, hpcstate: Any, shard: Any, entry: Mapping[str, str], refuse: Any) -> None:
    """J and N into the federation's state (`shard_nodes`), under its lifecycle lock and while it is linked."""
    store = hpclaunch.StateStore(shard.root)

    def add(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("phase") != "linked":
            raise hpcstate.StateError(f"its federation is {state.get('phase')}, not linked")
        entries = state.setdefault("shard_nodes", [])
        if dict(entry) not in entries:
            entries.append(dict(entry))
        return state

    try:
        with store.lifecycle("resume-line"):
            store.update(add)
    except (hpcstate.StateError, OSError) as error:
        refuse(f"its node could not be recorded in its federation's state ({error})")


def resume_line(
    shards: Any, manifest: Path, label: str, job: str, node: str, *, script: Path, runner: Any = None,
    user: str | None = None,
) -> list[str]:  # fmt: skip
    """The shard's step line with `--resume` in job `job` on node `node`, after every check of the module's docstring;
    `shards.ShardsError` naming the shard otherwise. `runner` is the login node's (`hpcslurm.Runner`) unless given."""

    def refuse(why: str) -> None:
        raise shards.ShardsError(f"shard {label}: {why}; no line is printed")

    # the study's scripts hashed and the declared variables read, as `plan` and `lines` do, before anything is recorded
    plan = shards.pin_pass_env(shards.pinned(shards.load_plan(manifest)))
    shard = next((s for s in plan.shards if s.label == label), None)
    if shard is None:
        raise shards.ShardsError(f"the manifest has no shard labelled {label}")
    spec = {"index": shard.index, "count": shard.count, "label": shard.label}
    try:  # the declared variables keep the values the shard's first step passed: the first check, before any query
        shards.check_pass_env_origin(plan.results, spec, dict(plan.pass_env_pins or ()))
    except shards.ShardsError as error:
        raise shards.ShardsError(f"{error}; no line is printed") from None
    fed = shards.federation(shard)
    with _driver_modules() as (hpclaunch, hpcslurm, hpcstate):
        slurm = hpcslurm.Slurm(runner or hpcslurm.Runner())
        errors = (hpcslurm.SlurmError, OSError)
        services = {}
        for each in plan.shards:
            with contextlib.suppress(hpcstate.StateError, OSError):
                state = hpcstate.load(each.root) or {}
                if state.get("allocation", {}).get("job"):
                    services[state["allocation"]["job"]] = state["allocation"]["name"]
        name = check_job(slurm, errors, plan, job, node, services, refuse)
        over = check_no_live_step(shards, slurm, errors, plan, shard, shards.step_name(fed, shard),
                                  user or pwd.getpwuid(os.getuid()).pw_name, refuse)  # fmt: skip
        check_campaign(shards, plan, shard, refuse)
        record_node(hpclaunch, hpcstate, shard, {"job": job, "node": node, "label": label, "name": name}, refuse)
    here = {**fed, "job": job, "het_group": None, "node": node, "listed": job}
    return shards.step_argv(plan, shard, here, script, resume=True, over=over)
