"""shards.py: one shard of a campaign per HPC federation (#154, milestone M5), from launch to merge.

    uv run python deploy/hpc-federation/shards.py plan    --manifest shards.toml
    uv run python deploy/hpc-federation/shards.py lines   --manifest shards.toml
    <python> -I deploy/hpc-federation/shards.py step   --study DIR --index I --count N --label L --tmpfs DIR \
        --results DIR --core-src DIR                    # inside the step on the sandbox node; `lines` writes it
    uv run python deploy/hpc-federation/shards.py collect --manifest shards.toml --into DIR [--run]
    <python> -I deploy/hpc-federation/shards.py step ... --archive-left  # an out/ a step could not archive
    <node python> -I deploy/hpc-federation/shards.py sweep --tmpfs DIR --root ROOT  # hpcfed.py down runs it

**Where the dispatcher runs.** One shard runs per federation, as one job step on that federation's sandbox node: the
Apptainer sandbox's data gate listens on that node's loopback and its cells are that node's `apptainer exec`s, so the
dispatcher (`python -m expcore run STUDY --shard i/N --shard-label L`) must run there, never on the login node. A step
is launched from the login node alone (srun cannot start one on a compute node), as the federation's own services are
(`DESIGN.md`): `lines` prints each shard's `srun --jobid=<job> [--het-group=<g>] --exact ... /usr/bin/env -i
<allowlist> <python> -I shards.py step ...`, to run detached (`setsid nohup ... &`). The job and the component are the
state file's `sandbox` record's: a component of the federation's heterogeneous job (`het`) or a job of its own
(`separate`). Nothing here runs srun.

**How it reaches its federation.** The step's environment is an allowlist: `PATH`, `LC_ALL`, and the federation's
`EXPCORE_FED_ROOT_PARENT` and `EXPCORE_FED_ROOT`. `step` copies the federation's `<root>/platform.toml`, where its
`up` rendered it with every path absolute into the root (secrets, CA, fault journal) on the filesystem the nodes see,
into its copy of the study beside `study.toml`, where the study's plugin loads it: a copy, never a link out of the
study, and never a path of the federation's in `study.toml`, whose bytes and hash are every shard's. A study that holds
a `platform.toml` of its own is refused. No secret is in the environment: a model's key comes from its key file. When
that file
declares the Apptainer sandbox, `step` makes its `work_root` (mode 0700) before the dispatcher, which the sandbox will
not start without; it refuses one that is a link, not this user's, open to others, or not under `--tmpfs` (the node's
tmpfs: never the shared filesystem).

**One campaign across federations (C06).** A space is the same experimental condition across federations only when
its declared identity and its data identity match, and the campaign fingerprint holds a space's data identity only when
its federation's `platform.toml` declares it (`data_manifest`, which `up` renders and checks): otherwise it holds the
space's Onezone id, which every federation's `up` makes anew, and the shards never merge. So `plan` and `lines` refuse
a manifest of more than one federation whose platform files leave a space's data undeclared, naming each shard and
space, before anything runs.

**The node's tmpfs is the node's, not the job's.** Each step's directory there (`expcore-shard-<label>.<random>`)
records its step (`step.json`: its pid and start time, its shard, its federation's root, its results directory, the
archive it started from, and its phase): `running` while its dispatcher runs, `archiving` from the moment it ends
(swapped in by rename from a record written at the start, so a full tmpfs cannot stop it), when its out/ is whole and
the only one until archived. A step killed outright leaves its directory. Only one killed while `running` is ever
removed, and only once its process and every process naming the directory (its dispatcher, which outlives a wrapper
killed alone) are shown gone (the dispatcher dies with its step and records its own end, `DISPATCHER`): `--resume`
removes its own shard's of its own campaign (spec, root and results) and refuses while one runs, and `sweep`, which
`hpcfed.py down` runs on the agent's node, removes its federation's. A directory whose dispatcher ended and that was not
archived (`archiving`; marked `ARCHIVE-FAILED.json` when the mark could be written), or whose record cannot be read, is
never removed: `--resume` refuses while one of its own is there, `sweep` names it, and `step --archive-left` archives it
with the same manifest and that step's exit status, then removes it, but only once that step is gone and while the
published archive is still the one it started from.

**The study and its store.** The study is one stamped copy at the pinned commit on the shared filesystem. `step` copies
it to the node's tmpfs and runs it there, since the campaign's store (SQLite, and its `campaign.lock`) must not live on
a network filesystem; it checks first that the interpreter imports the core it was given (`--core-src`), relays SIGTERM
to the dispatcher, and ends with the dispatcher's exit status.

**How a shard comes back.** Once the dispatcher has exited (its lock released), `step` writes the copy's `out/` whole,
`campaign.lock` included, to `<results>/<label>.out.tar` with a manifest beside it (`<label>.out.json`: its SHA-256,
bytes, files, the shard's spec and the dispatcher's exit status), each written whole and renamed into place, and removes
its tmpfs directory; an archive that cannot be written leaves that directory in place, marked (exit 75), since it holds
the only whole `out/`. A start of a shard that has results is refused unless it is `--resume`, which unpacks that
archive into its copy first, so a repeated launch line never replaces a finished shard. `collect` verifies a private
copy of every shard's archive against its manifest and the manifest against the plan, unpacks that copy into
`<into>/<label>/out`, read-only, and prints (or with `--run` runs) `python -m expcore merge`. A shard whose archive has
no `campaign.lock`, does not verify, or is missing is refused, naming its label: the merge would refuse it too.

**Faults.** A campaign with `[faults]` runs here too: the platform layer reaches the federation's login-node fault relay
through request and answer files in `<root>/fault-relay/` on the shared root (M4's `HpcDriver`), never a step of its
own, and `EXPCORE_FED_ROOT` is how it finds that root. Outage runs on HPC depend on that relay.

**Snapshots, and a step killed outright (O62).** The step's `out/` is on the node's tmpfs, so a step killed outright
(walltime, a node's end, an OOM in the step's cgroup) would lose all of it. While the dispatcher runs, `step`
snapshots it into `<results>/<label>/snapshots/` (`expcore.campaign.out_snapshot`): every `--snapshot-every` seconds
(the manifest's `snapshot_every_s`, 900 by default) and after each run's end (a change in the store's count of ended
attempts, read every `POLL_S`), one at a time, and once more after the dispatcher exits, before the archive. A
snapshot copies the store first (the core's online backup) and `runs/` after it, verifies both, and is swapped in by
rename; the newest two are kept. The dispatcher also journals what it spends to `<results>/<label>/journals/`
(`expcore run --spend-journal`, `expcore.campaign.spend_journal`), a new file per step. `--resume` restores the newest
snapshot that verifies, unless the shard's archive is newer (a snapshot names the archive its step started from, and
one taken before the archive was written names an older one or none): then the archive, as before. Either way the
store's attempt directories are pointed at the new copy's `runs/`, and the dispatcher's start counts what the
journals hold that the restored store lacks (attempts begun, top-ups and probe sends after the snapshot) as
`lost_in_kill`, once, against the shard's share of the budget. A start without `--resume` is refused when the shard
has a snapshot, as when it has results. A snapshot or a journal of another shard is refused, naming it. The shard's
step must be sized for the sandbox, the dispatcher and the store together.

The manifest (`shards.toml`, operator-local, never committed, since it names the site's paths):

    study = "/shared/study"           # the stamped study copy
    python = "/shared/venv/bin/python" # the core's interpreter, on the filesystem the nodes see
    core_src = "/shared/core/src"     # what that interpreter must import the core from
    results = "/shared/results"       # where each shard's archive and manifest come back
    tmpfs = "/tmp"                    # the node's tmpfs, never Slurm's per-job TMPDIR (a link into scratch)
    step_path = "/usr/bin:/bin"       # the step's PATH
    cpus = 6
    memory = "18g"
    snapshot_every_s = 900            # optional: seconds between snapshots of a running shard's out/ (also at run ends)
    [[shards]]                        # in order: shard i is the i-th entry
    label = "fed-a"                   # the shard's label (no host name: it travels with the export)
    root = "/shared/fed/a"            # the federation's root, holding hpc-federation.json and platform.toml
    sandbox_node = "..."              # optional when the state file names the sandbox's node
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

FORMAT = "expcore-hpc-shard/1"
STATE_FORMAT = "expcore-hpc-federation/1"
STATE_FILE = "hpc-federation.json"
LINKED = "linked"  # the state's phase once up has finished: every space's data checked, every link
PLACEMENTS = ("het", "separate")  # the sandbox record's placements: a het component, or a held job of its own
LOCK_NAME = "campaign.lock"  # expcore.campaign.lock.LOCK_NAME: the merge refuses a shard without it
ALLOWLIST = ("PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT")
CORE_MISMATCH = 97  # the step's interpreter imports another core than the one it was given
ARCHIVE_FAILED = 75  # the dispatcher ended but its out/ could not be archived: kept on the node's tmpfs
CHUNK = 1 << 20
SNAPSHOT_EVERY_S = 900.0  # a running shard's out/ is snapshotted this often, and after each run's end
POLL_S = 30.0  # how often the snapshot thread looks for a run's end in the store
SNAPSHOTS, JOURNALS = "snapshots", "journals"  # under <results>/<label>/
WORK_PREFIX = "expcore-shard-"  # a step's directory on the node's tmpfs: <tmpfs>/expcore-shard-<label>.<random>
OWNER, OWNER_FORMAT = "step.json", "expcore-hpc-shard-step/1"  # in it: the step's process, campaign and federation
OWNER_ENDING = ".step.ended.json"  # written at the start, renamed over step.json when the dispatcher ends
OWNER_STATUS = ".step.status.json"  # the same with the dispatcher's exit status, written when it ends, if it can be
RUNNING, ARCHIVING = "running", "archiving"  # the step's phase: its dispatcher runs; it ended and out/ is whole
ARCHIVE_FAILED_MARK = "ARCHIVE-FAILED.json"  # in it: the out/ in it could not be archived, so it is kept
PLATFORM = "platform.toml"  # the federation's, in its root; the step's copy of the study has it beside study.toml


class ShardsError(Exception):
    """A manifest, a federation's state or a shard's archive that cannot be used; the message names what."""


@dataclass(frozen=True)
class Shard:
    index: int
    count: int
    label: str
    root: Path
    node: str | None = None


@dataclass(frozen=True)
class Plan:
    study: Path
    python: str
    core_src: Path
    results: Path
    tmpfs: Path
    step_path: str
    cpus: int
    memory: str
    shards: tuple[Shard, ...]
    snapshot_every_s: float = SNAPSHOT_EVERY_S


# ------------------------------------------------------------------------------------------------ the manifest
def _label_ok(index: int, count: int, label: str) -> None:
    from expcore.campaign.shard import ShardError, ShardSpec

    try:
        ShardSpec(index, count, label)
    except ShardError as error:
        raise ShardsError(f"shard {index}: {error}") from None


def load_plan(path: Path) -> Plan:
    """Read and check the manifest: every key present, absolute paths, at least two shards, labels by the core's rule
    and each once."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ShardsError(f"the manifest could not be read ({type(error).__name__})") from None
    keys = {"study", "python", "core_src", "results", "tmpfs", "step_path", "cpus", "memory", "shards"}
    if not keys <= set(data) <= keys | {"snapshot_every_s"}:
        raise ShardsError(f"the manifest's keys must be: {', '.join(sorted(keys))}, and perhaps snapshot_every_s")
    every = data.get("snapshot_every_s", SNAPSHOT_EVERY_S)
    if isinstance(every, bool) or not isinstance(every, int | float) or not 60 <= every < float("inf"):
        raise ShardsError("snapshot_every_s must be a number of seconds, at least 60")
    for key in ("study", "core_src", "results", "tmpfs"):
        if not isinstance(data[key], str) or not data[key].startswith("/"):
            raise ShardsError(f"{key} must be an absolute path")
    if not isinstance(data["cpus"], int) or isinstance(data["cpus"], bool) or data["cpus"] < 1:
        raise ShardsError("cpus must be a whole number of at least 1")
    if not isinstance(data["memory"], str) or not data["memory"][:-1].isdigit() or data["memory"][-1] not in "mgMG":
        raise ShardsError("memory must be a size in m or g, such as 18g")
    entries = data["shards"]
    if not isinstance(entries, list) or len(entries) < 2:
        raise ShardsError("a sharded campaign needs at least two [[shards]], one per federation")
    shards = []
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, Mapping) or not {"label", "root"} <= set(entry) <= {"label", "root", "sandbox_node"}:
            raise ShardsError(f"shard {n}: an entry has label, root and perhaps sandbox_node")
        _label_ok(n, len(entries), entry["label"])
        if not isinstance(entry["root"], str) or not entry["root"].startswith("/"):
            raise ShardsError(f"shard {n}: root must be an absolute path")
        shards.append(Shard(n, len(entries), entry["label"], Path(entry["root"]), entry.get("sandbox_node")))
    labels = [s.label for s in shards]
    if len(set(labels)) != len(labels):
        raise ShardsError("a label is given to two shards")
    return Plan(Path(data["study"]), str(data["python"]), Path(data["core_src"]), Path(data["results"]),
                Path(data["tmpfs"]), str(data["step_path"]), data["cpus"], data["memory"], tuple(shards),
                float(every))  # fmt: skip


def federation(shard: Shard) -> dict[str, Any]:
    """Where the shard's step goes, from the federation's state as its `up` wrote it: the job, the heterogeneous
    component, the step name's prefix and the sandbox's node.

    The state's `sandbox` record says how the sandbox node was placed: `het`, a component of the federation's own
    heterogeneous job (the step needs its `--het-group`: the site's Slurm refuses one that names none), or
    `separate`, a held job of its own (its `job`). A state with no `sandbox` record is the federation's job alone, its
    node the `sandbox` role `up` placed there (`services.sandbox`), or the manifest's. Either way the manifest's
    `sandbox_node`, when given, must be the state's."""
    try:
        state = json.loads((shard.root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ShardsError(f"shard {shard.label}: its federation's state file could not be read") from None
    if not isinstance(state, Mapping) or state.get("format") != STATE_FORMAT:
        raise ShardsError(f"shard {shard.label}: its federation's state file is not {STATE_FORMAT}")
    allocation = state.get("allocation") or {}
    job, name = allocation.get("job"), allocation.get("name")
    if not (_job_id(job) and isinstance(name, str) and name):
        raise ShardsError(f"shard {shard.label}: its federation has no held job on record")
    if state.get("phase") != LINKED:  # an up that failed (its data check, its link check) left a federation not ready
        raise ShardsError(f"shard {shard.label}: its federation's up has not finished (phase {state.get('phase')})")
    record = state.get("sandbox")
    placement, group = "allocation", None
    if record is not None:
        if not isinstance(record, Mapping) or record.get("placement") not in PLACEMENTS:
            raise ShardsError(f"shard {shard.label}: its sandbox record names no placement of {', '.join(PLACEMENTS)}")
        placement = record["placement"]
        if placement == "het":
            group = record.get("het_group")
            if isinstance(group, bool) or not isinstance(group, int) or group < 1:
                raise ShardsError(f"shard {shard.label}: its sandbox is a het component with no het_group above 0")
        else:
            job, name = record.get("job"), record.get("name") or name
            if not _job_id(job):
                raise ShardsError(f"shard {shard.label}: its separate sandbox has no held job on record")
    if record is not None:  # the record's own node: never one placed in another job, nor the manifest's alone
        node = record.get("node")
    else:  # the federation's own job: the sandbox role its up placed there (`services.sandbox`), or the manifest's
        held = (state.get("services") or {}).get("sandbox") or {}
        node = (held.get("node") if held.get("kind") == "sandbox" else None) or shard.node
    if not isinstance(node, str) or not node:
        raise ShardsError(f"shard {shard.label}: no sandbox node: the state file names none and the manifest neither")
    if shard.node and node != shard.node:
        raise ShardsError(f"shard {shard.label}: the manifest's sandbox_node is not the one its state file names")
    return {"job": job, "name": name, "node": node, "placement": placement, "het_group": group}


def undeclared_data(plan: Plan) -> list[str]:
    """For a campaign on more than one federation, each shard whose federation's `platform.toml` leaves a space's data
    undeclared (`data_manifest`, C06), as `<label>: <space>, ...`; empty on one federation, or when every space of every
    federation declares it. A space is one experimental condition across federations only by its data identity, so
    such shards would never merge: refused before anything runs, never found at the merge after the spend."""
    if len({shard.root for shard in plan.shards}) < 2:
        return []
    found = []
    for shard in plan.shards:
        try:
            data = tomllib.loads((shard.root / PLATFORM).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            found.append(f"{shard.label}: its federation's {PLATFORM} could not be read")
            continue
        entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
        missing = [str(e.get("name")) if isinstance(e, Mapping) else "[space]" for e in entries
                   if not (isinstance(e, Mapping) and e.get("data_manifest"))]  # fmt: skip
        if missing:
            found.append(f"{shard.label}: {', '.join(missing)}")
    return found


def _job_id(job: Any) -> bool:
    return isinstance(job, str) and job.isdigit()


def step_argv(plan: Plan, shard: Shard, fed: Mapping[str, str], script: Path) -> list[str]:
    """The login node's `srun` of one shard's step (the module's docstring): `--exact`, never `--overlap`; an
    environment of the allowlist alone, set on the node by `env -i`, with no secret in it."""
    env = [f"PATH={plan.step_path}", "LC_ALL=C", f"EXPCORE_FED_ROOT_PARENT={shard.root.parent}",
           f"EXPCORE_FED_ROOT={shard.root}"]  # fmt: skip
    step = [plan.python, "-I", str(script), "step", "--study", str(plan.study), "--index", str(shard.index),
            "--count", str(shard.count), "--label", shard.label, "--tmpfs", str(plan.tmpfs),
            "--results", str(plan.results), "--core-src", str(plan.core_src),
            "--snapshot-every", f"{plan.snapshot_every_s:g}"]  # fmt: skip
    return [
        "srun", f"--jobid={fed['job']}", *([f"--het-group={fed['het_group']}"] if fed.get("het_group") else []),
        "--exact", "-N1", "-n1", "-w", fed["node"], "-c", str(plan.cpus),
        f"--mem={plan.memory}", "-J", f"{fed['name']}-shard-{shard.index}", "--export=PATH",
        "/usr/bin/env", "-i", *env, *step,
    ]  # fmt: skip


# ------------------------------------------------------------------------------------------------ archives
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _write_whole(path: Path, write: Callable[[Path], None]) -> None:
    """Write `path` through a draft beside it, fsynced and renamed: a reader sees the old file or the whole new one."""
    draft = path.with_name(path.name + ".part")
    draft.unlink(missing_ok=True)
    write(draft)
    with open(draft, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(draft, path)


def archive(out: Path, results: Path, spec: Mapping[str, Any], exit_status: int) -> dict[str, Any]:
    """Write `out/` whole to `<results>/<label>.out.tar` and its manifest; the manifest, as written."""
    label = str(spec["label"])
    tar_path = results / f"{label}.out.tar"
    files = sum(1 for p in out.rglob("*") if p.is_file() and not p.is_symlink())

    def write_tar(draft: Path) -> None:
        with tarfile.open(draft, "w", format=tarfile.PAX_FORMAT) as tar:
            tar.add(out, arcname="out", recursive=True)

    _write_whole(tar_path, write_tar)
    manifest = {
        "format": FORMAT,
        "label": label,
        "shard": dict(spec),
        "sha256": _sha256(tar_path),
        "bytes": tar_path.stat().st_size,
        "files": files,
        "lock": (out / LOCK_NAME).is_file(),
        "exit_status": exit_status,
    }
    _write_whole(results / f"{label}.out.json", lambda d: d.write_text(json.dumps(manifest, sort_keys=True) + "\n"))
    return manifest


def verified(results: Path, shard: Shard, scratch: Path) -> tuple[Path, dict[str, Any]]:
    """A shard's archive, as a private copy in `scratch` whose bytes are the ones hashed, and its manifest, shown to be
    this shard's and whole; `ShardsError` naming the label otherwise. The copy, never the original, is what is
    unpacked, so an archive replaced after its check is never taken for the one checked."""
    tar_path, json_path = results / f"{shard.label}.out.tar", results / f"{shard.label}.out.json"
    try:
        manifest = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ShardsError(f"shard {shard.label}: no readable manifest of its out/") from None
    spec = manifest.get("shard") if isinstance(manifest, Mapping) else None
    if manifest.get("format") != FORMAT or not isinstance(spec, Mapping):
        raise ShardsError(f"shard {shard.label}: its manifest is not {FORMAT}")
    if (spec.get("index"), spec.get("count"), spec.get("label")) != (shard.index, shard.count, shard.label):
        raise ShardsError(
            f"shard {shard.label}: its archive is of another shard ({spec.get('index')}/{spec.get('count')})"
        )
    if manifest.get("lock") is not True:
        raise ShardsError(f"shard {shard.label}: its out/ has no {LOCK_NAME}, which the merge needs")
    copy = scratch / f".{shard.label}.out.tar"
    digest = hashlib.sha256()
    try:
        with open(tar_path, "rb") as source, open(copy, "xb") as target:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                target.write(chunk)
    except FileNotFoundError:
        copy.unlink(missing_ok=True)
        raise ShardsError(f"shard {shard.label}: its archive is missing") from None
    if digest.hexdigest() != manifest.get("sha256"):
        copy.unlink()
        raise ShardsError(f"shard {shard.label}: its archive is not the one its manifest names")
    return copy, manifest


def unpack(tar_path: Path, into: Path) -> Path:
    """Unpack an archive's `out/` into `into` (new), refusing any member outside it (`tarfile`'s data filter)."""
    into.mkdir(parents=True)
    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
        if not names or any(n != "out" and not n.startswith("out/") for n in names):
            raise ShardsError("an archive holds something besides out/")
        tar.extractall(into, filter="data")
    return into / "out"


def _read_only(root: Path) -> None:
    for base, dirs, files in os.walk(root, topdown=False):
        for name in (*files, *dirs):
            path = Path(base, name)
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def collect(plan: Plan, into: Path) -> list[Path]:
    """Every shard's out/, verified and unpacked read-only into `<into>/<label>/out`; refused whole if any fails."""
    if into.exists() and any(into.iterdir()):
        raise ShardsError("the directory to collect into must be new or empty")
    created = not into.exists()
    into.mkdir(parents=True, exist_ok=True)
    try:
        found = [verified(plan.results, shard, into) for shard in plan.shards]
    except ShardsError:  # refused whole: nothing of it is left behind
        for leftover in into.glob(".*.out.tar"):
            leftover.unlink()
        if created:
            into.rmdir()
        raise
    outs = []
    for shard, (tar_path, _) in zip(plan.shards, found, strict=True):
        out = unpack(tar_path, into / shard.label)
        tar_path.unlink()
        if not (out / LOCK_NAME).is_file():
            raise ShardsError(f"shard {shard.label}: its unpacked out/ has no {LOCK_NAME}")
        _read_only(out)
        outs.append(out)
    return outs


def merge_argv(plan: Plan, outs: Sequence[Path]) -> list[str]:
    return [plan.python, "-m", "expcore", "merge", str(plan.study / "out"), *map(str, outs)]


# ------------------------------------------------------------------------------------------------ the step
def remove_tree(path: Path) -> None:
    """Remove a tree the run left read-only: directories made writable first (never through a link), then removed."""
    for base, dirs, _ in os.walk(path, topdown=True, followlinks=False):
        for name in (base, *(os.path.join(base, d) for d in dirs)):
            info = os.lstat(name)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(name, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
    shutil.rmtree(path)


def imports_core_from(core_src: Path, find: Callable[[], str] | None = None) -> bool:
    """Whether this interpreter imports `expcore` from `core_src` (lane H's trap: a step that silently ran an
    installed core)."""
    if find is None:
        import expcore

        def find() -> str:
            return expcore.__file__

    try:
        return Path(find()).resolve().is_relative_to(core_src.resolve())
    except (OSError, ValueError):
        return False


# The dispatcher's first words, in its own process. It asks the kernel to kill it when its step dies
# (PR_SET_PDEATHSIG) and, when that cannot be asked or the step is already gone, it never starts (exit 71): a step
# killed alone never leaves it running on. It then runs `expcore` itself (as `python -m expcore` would) and, when that
# ends by any path but a kill, swaps the step's record to `archiving` before the process exits, in its working
# directory, the step's: so a dispatcher that ended is never under a record saying `running`, which a cleanup would
# take for a step killed mid-run. Any other command is exec'd as it is.
DISPATCHER = f"""import ctypes, os, runpy, signal, sys
try:
    tied = ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) == 0
except (AttributeError, OSError):
    tied = False
if not tied or os.getppid() != int(sys.argv[1]):
    sys.stderr.write("shards: ERROR: the dispatcher could not be tied to its step, so it does not start\\n")
    os._exit(71)
argv = sys.argv[2:]
if argv[1:4] != ["-I", "-m", "expcore"]:
    os.execv(argv[0], argv)
sys.argv = ["expcore", *argv[4:]]
try:  # its SystemExit, its message and its exit status pass as `python -m expcore`'s own
    runpy.run_module("expcore", run_name="__main__", alter_sys=True)
finally:
    try:
        os.replace("{OWNER_ENDING}", "{OWNER}")
    except OSError:
        pass
"""
NOT_TIED = 71  # the dispatcher could not be tied to its step: it never started


def run_dispatcher(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    """Run the dispatcher, relaying SIGTERM and SIGINT to it, and return its exit status (128 + N for a signal N). It
    dies with this step, and records its own end in the step's record (`DISPATCHER`); `cwd` is the step's directory."""
    words = [sys.executable, "-I", "-c", DISPATCHER, str(os.getpid()), *argv]
    child = subprocess.Popen(words, env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL)

    def relay(signum: int, _frame: Any) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, relay)
    code = child.wait()
    return 128 - code if code < 0 else code


class Snapshots:
    """When a running shard's `out/` is snapshotted (O62; `snapshot` is what a snapshot is).

    `take(out)` is called from a thread of its own while the dispatcher runs: every `every_s` seconds, and, given
    `progress` (a reading that changes when a run ends, None while there is none), within `poll_s` of a change, one
    snapshot at a time; and once after the dispatcher has exited, before the archive. One that raises is reported
    and never ends the run: the last good snapshot stands."""

    def __init__(
        self,
        take: Callable[[Path], None],
        every_s: float = SNAPSHOT_EVERY_S,
        *,
        progress: Callable[[], Any] | None = None,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.take, self.every_s, self.progress, self.clock = take, every_s, progress, clock
        self.poll_s = min(poll_s, every_s) if progress is not None else every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _once(self, out: Path) -> None:
        try:
            self.take(out)
        except Exception as error:  # a snapshot that failed leaves the last good one: reported, the run goes on
            print(f"shards: a snapshot of out/ failed ({type(error).__name__}: {error})", file=sys.stderr)

    def _seen(self) -> Any:
        try:
            return None if self.progress is None else self.progress()
        except Exception:  # a reading that fails says nothing ended: the clock still takes the next one
            return None

    def start(self, out: Path) -> None:
        def loop() -> None:
            last, seen = self.clock(), self._seen()
            while not self._stop.wait(self.poll_s):
                now = self._seen()
                ended = now is not None and now != seen
                if (ended or self.clock() - last >= self.every_s) and out.is_dir():
                    self._once(out)
                    last, seen = self.clock(), now if now is not None else seen

        self._thread = threading.Thread(target=loop, daemon=True, name="shard-snapshots")
        self._thread.start()

    def stop(self, out: Path) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if out.is_dir():
            self._once(out)


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def taker(snapdir: Path, scratch: Path, facts: Mapping[str, Any], live: threading.Event) -> Callable[[Path], None]:
    """The default snapshot of `out/` into `snapdir` (`expcore.campaign.out_snapshot.take`): its store read in place
    while `live` is set (the dispatcher holds it), from a private copy once it is not."""

    def take(out: Path) -> None:
        from expcore.campaign import out_snapshot

        made = out_snapshot.take(out, snapdir, scratch=scratch, facts=facts, live=live.is_set(), now=_stamp())
        print(f"shards: snapshot {made.name} of shard {facts['shard']['label']} taken", file=sys.stderr)

    return take


def settle_store(out: Path) -> None:
    """Close the store as its dispatcher's own last close does, when a snapshot's read in place, made as the
    dispatcher exited, left its -shm (and -wal) beside it: one connection, opened, read and closed, checkpoints the
    -wal into the store and removes both, so the archive holds what the dispatcher left. Nothing for a store without
    a -shm, which a dispatcher that closed its store leaves none of."""
    import sqlite3

    store = out / "state.sqlite"
    if not store.is_file() or not store.with_name(store.name + "-shm").exists():
        return
    connection = sqlite3.connect(store)
    try:
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


def archive_named(results: Path, label: str) -> str | None:
    """The SHA-256 the shard's archive manifest names: None when there is none; a value no snapshot names when it
    cannot be read, so that the archive, which then fails its check, is never passed over for an older snapshot."""
    path = results / f"{label}.out.json"
    if not path.exists():
        return None
    try:
        named = json.loads(path.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError, AttributeError):
        named = None
    return named if isinstance(named, str) else "unreadable"


def resume_into(args: argparse.Namespace, spec: Mapping[str, Any], base: str | None, work: Path, out: Path) -> None:
    """Put the shard's latest `out/` at `out`: the newest snapshot that verifies, unless the archive is newer (a
    snapshot that does not name it was taken before it), else the archive, else nothing (a fresh start, only when the
    shard has neither). Either way the store's attempt directories are pointed at `out/runs`. A snapshot of another
    shard, and a shard whose every snapshot fails its check and that has no archive, are `ShardsError`: its results
    would otherwise be dropped for a fresh start."""
    from expcore.campaign import out_snapshot
    from expcore.campaign.snapshot import rebase_run_dirs

    found = out_snapshot.snapshots(args.results / args.label / SNAPSHOTS)
    for snapshot in found:
        try:
            facts = out_snapshot.manifest_of(snapshot)["facts"]
        except out_snapshot.OutSnapshotError as error:
            print(f"shards: snapshot {snapshot.name} is not used: {error}", file=sys.stderr)
            continue
        if facts.get("shard") != dict(spec):
            raise ShardsError(f"shard {args.label}: snapshot {snapshot.name} is of shard {facts.get('shard')}")
        if base is not None and facts.get("base") != base:
            break  # taken before the archive: the archive is newer than it and every older one
        try:
            out_snapshot.restore(snapshot, out)
        except out_snapshot.OutSnapshotError as error:
            print(f"shards: snapshot {snapshot.name} is not used: {error}", file=sys.stderr)
            continue
        print(f"shards: shard {args.label} resumes from snapshot {snapshot.name}", file=sys.stderr)
        return
    if found and not (args.results / f"{args.label}.out.tar").exists():
        raise ShardsError(
            f"shard {args.label}: no snapshot of its {len(found)} verifies and it has no archive; a fresh start would "
            "drop its results, so the resume is refused"
        )
    if (args.results / f"{args.label}.out.tar").exists():
        shard = Shard(args.index, args.count, args.label, Path("/"))
        tar_path, _ = verified(args.results, shard, work)
        shutil.move(unpack(tar_path, work / "previous"), out)
        tar_path.unlink()
        if (out / out_snapshot.STORE).is_file():
            rebase_run_dirs(out / out_snapshot.STORE, out / out_snapshot.RUNS)
        print(f"shards: shard {args.label} resumes from its archive", file=sys.stderr)


# ------------------------------------------------------------------------------------------------ the agent's node
def sandbox_work_root(root: str | None) -> Path | None:
    """The Apptainer sandbox's `work_root` that the federation's `platform.toml` names (relative to that file, as the
    platform layer reads it), or None: no federation named, no platform file, or no Apptainer sandbox in it."""
    if not root:
        return None
    platform = Path(root) / PLATFORM
    try:
        data = tomllib.loads(platform.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ShardsError(f"the federation's platform.toml could not be read ({type(error).__name__})") from None
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, Mapping) or sandbox.get("backend") != "apptainer":
        return None
    value = sandbox.get("work_root")
    if not isinstance(value, str) or not value:
        raise ShardsError("the federation's platform.toml names no [sandbox] work_root")
    return Path(os.path.normpath(platform.parent / value))


def make_work_root(path: Path, tmpfs: Path) -> None:
    """Make the sandbox's work root on this node, mode 0700, before the dispatcher (the sandbox refuses a start
    without it); an existing one must be a directory of this user's, not a link, with no access for others. It must lie
    under `--tmpfs`, the node's tmpfs: never on the shared filesystem, where the runs' private files would be seen
    from every node. `ShardsError` names what is wrong, never the path."""
    node_tmp = Path(os.path.realpath(tmpfs))
    where = Path(os.path.realpath(path.parent)) / path.name
    if where == node_tmp or not where.is_relative_to(node_tmp):
        raise ShardsError("[sandbox] work_root is not on the node's tmpfs (under --tmpfs): it is never shared")
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise ShardsError("[sandbox] work_root's parent is not a directory on this node") from None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ShardsError("[sandbox] work_root is a link or not a directory")
    if info.st_uid != os.getuid():
        raise ShardsError("[sandbox] work_root is not this user's")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ShardsError("[sandbox] work_root gives others access: it must be 0700")


def _start_of(pid: int) -> str | None:
    """A process's start time, field 22 of /proc/<pid>/stat (as `apptainer.node.LocalNode` reads it); None without."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = text.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def alive(pid: Any, start: Any) -> bool | None:
    """Whether the process that started at `start` still holds `pid`, as M5's reaper tells (`LocalNode.alive`): None
    when that cannot be told, which is never gone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 or not isinstance(start, str):
        return None
    now = _start_of(pid)
    if now is not None:
        return now == start
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return None


def naming(path: Path) -> bool | None:
    """Whether a live process of this node names `path` (or a path in it) in its argv, as the step's dispatcher names
    its copy of the study (`run <dir>/study`) and any process it starts does: None when /proc cannot be listed, which is
    never none. A wrapper killed alone leaves its dispatcher running in the directory, so the wrapper's pid is not
    enough."""
    text = str(path).encode()
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit() and int(name) != os.getpid()]
    except OSError:
        return None
    for pid in pids:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # gone since, or another user's that hidepid hides: none of this user's steps
        if any(word == text or word.startswith(text + b"/") for word in argv):
            return True
    return False


@dataclass(frozen=True)
class Left:
    """A step's directory on the node's tmpfs: its owner record (`step.json`, None when it cannot be read) and its
    archive-failed mark (informational: the record's phase is what decides)."""

    path: Path
    owner: Mapping[str, Any] | None
    failed: Mapping[str, Any] | None

    @property
    def phase(self) -> str | None:
        return None if self.owner is None else self.owner.get("phase")

    @property
    def removable(self) -> bool:
        """Only a step killed while its dispatcher ran (`running`: its out/ was never whole, and the snapshots and the
        journals hold what it reached), whose wrapper and every process naming its directory are shown gone. A step
        whose dispatcher ended (`archiving`) holds the only whole out/ until it is archived, and one whose record
        cannot be read could be either: never removable."""
        if self.phase != RUNNING:
            return False
        return alive(self.owner.get("pid"), self.owner.get("start")) is False and naming(self.path) is False

    def ours(self, spec: Mapping[str, Any], root: str | None, results: Path) -> bool:
        """Whether it is a step of this shard of this campaign: its whole spec, its federation's root and its results
        directory as this step's. The node's tmpfs is the node's: another campaign may use the same labels."""
        return self.owner is not None and (
            self.owner.get("shard"),
            self.owner.get("root"),
            self.owner.get("results"),
        ) == (dict(spec), root, str(results))


def _record(path: Path) -> Mapping[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, Mapping) else None


def leftovers(tmpfs: Path, label: str | None = None) -> list[Left]:
    """The steps' directories under `tmpfs` (named for one shard's label, or every one), never a link."""
    prefix = f"{WORK_PREFIX}{label}." if label is not None else WORK_PREFIX
    found = []
    for path in sorted(tmpfs.glob(f"{prefix}*")):
        if not path.is_symlink() and path.is_dir():
            found.append(Left(path, _record(path / OWNER), _record(path / ARCHIVE_FAILED_MARK)))
    return found


def clear_leftovers(tmpfs: Path, spec: Mapping[str, Any], root: str | None, results: Path) -> None:
    """Before a resume: remove each earlier step's directory of this shard of this campaign whose step was killed while
    its dispatcher ran and is shown gone; refuse while one holds an out/ its dispatcher ended with and that was not
    archived (`--archive-left` archives it), or while one still runs or cannot be shown gone. A directory of another
    campaign or federation, or whose record cannot be read, is kept, and said."""
    label = spec["label"]
    for left in leftovers(tmpfs, label):
        if not left.ours(spec, root, results):
            print(f"shards: {left.path.name} is not a step of this shard of this campaign, or names none: kept",
                  file=sys.stderr)  # fmt: skip
            continue
        if left.phase != RUNNING:
            raise ShardsError(
                f"shard {label}: an earlier step's out/ was not archived and is kept on this node in "
                f"{left.path.name}; archive it with `shards.py step --archive-left` before resuming"
            )
        if not left.removable:
            raise ShardsError(f"shard {label}: a step of it still runs on this node, or cannot be shown gone")
        remove_tree(left.path)
        print(f"shards: shard {label}: an earlier step's directory on this node is removed", file=sys.stderr)


def archive_left(args: argparse.Namespace) -> int:
    """Archive the out/ an earlier step of this shard of this campaign ended with and did not archive (`archiving`,
    with an `ARCHIVE_FAILED` mark or not), as that step would have: `<results>/<label>.out.tar` and its manifest, with
    that step's exit status; then remove its directory. Refused unless the published archive is still the one that
    step started from: a later one would be replaced by an older out/."""
    spec = {"index": args.index, "count": args.count, "label": args.label}
    root = os.environ.get("EXPCORE_FED_ROOT")
    found = [left for left in leftovers(args.tmpfs, args.label) if left.ours(spec, root, args.results)
             and left.phase == ARCHIVING]  # fmt: skip
    if not found:
        raise ShardsError(
            f"shard {args.label}: no out/ of this shard of this campaign waits on this node to be archived"
        )
    left = found[0]
    if alive(left.owner.get("pid"), left.owner.get("start")) is not False or naming(left.path) is not False:
        raise ShardsError(f"shard {args.label}: the step of {left.path.name} still runs (it archives it itself)")
    if archive_named(args.results, args.label) != left.owner.get("base"):
        raise ShardsError(
            f"shard {args.label}: an archive was published after the step of {left.path.name} started; its out/ is "
            "not archived over it (remove the directory by hand once its out/ is shown superseded)"
        )
    out = left.path / "study" / "out"
    if not out.is_dir():
        raise ShardsError(f"shard {args.label}: {left.path.name} holds no out/")
    settle_store(out)
    status = left.owner.get("exit_status", (left.failed or {}).get("exit_status", 1))
    manifest = archive(out, args.results, spec, int(status))
    remove_tree(left.path)
    print(f"shards: shard {args.label}'s kept out/ archived: {manifest['files']} files, sha256 {manifest['sha256']}")
    return 0


def sweep(tmpfs: Path, root: str) -> int:
    """On the agent's node, at the federation's down: remove every step's directory of this federation (its owner
    record names `root`) whose step was killed while its dispatcher ran and is shown gone; name each one kept (an out/
    not archived, a step still running). Stdlib alone: it runs under the node's own Python."""
    removed, kept = 0, []
    for left in leftovers(tmpfs):
        if left.owner is None or left.owner.get("root") != root:
            continue  # not this federation's, or not to be told
        if left.phase != RUNNING:
            kept.append(f"{left.path.name} (its out/ was not archived: `shards.py step --archive-left`)")
        elif not left.removable:
            kept.append(f"{left.path.name} (its step still runs, or cannot be shown gone)")
        else:
            remove_tree(left.path)
            removed += 1
    print(f"shards: sweep: {removed} step directory(ies) removed; kept: {'; '.join(kept) or 'none'}")
    return 0


def _ended(work: Path, status: int) -> bool:
    """Record that the step's dispatcher ended (`archiving`), with its exit status: from here its out/ is whole and the
    only one until archived, so no cleanup removes it. The dispatcher swapped in the record written at the start as
    it ended (`DISPATCHER`); a runner of another kind did not, and it is swapped in here. The record with the status is
    written whole into a file of its own, read back, and swapped in by rename, which needs no space; should that fail
    (a full tmpfs), the record without the status stands. False when no record says the dispatcher ended, said."""
    if (work / OWNER_ENDING).exists():
        try:
            os.replace(work / OWNER_ENDING, work / OWNER)
        except OSError as error:
            print(f"shards: ERROR: the step's end could not be recorded ({type(error).__name__})", file=sys.stderr)
            return False
    current = _record(work / OWNER)
    if current is None or current.get("phase") != ARCHIVING:
        print("shards: ERROR: the step's record does not say its dispatcher ended", file=sys.stderr)
        return False
    record = {**current, "exit_status": status}
    try:
        (work / OWNER_STATUS).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        if _record(work / OWNER_STATUS) == record:
            os.replace(work / OWNER_STATUS, work / OWNER)
    except OSError:
        pass  # the record without the status stands, whole
    return True


def step(
    args: argparse.Namespace,
    *,
    runner: Callable[[Sequence[str], Mapping[str, str], Path], int] = run_dispatcher,
    core_found: Callable[[Path], bool] = imports_core_from,
    snapshots: Snapshots | None = None,
) -> int:
    """One shard on its node (the module's docstring): copy, restore, run, archive, clean up; the dispatcher's exit
    status. `snapshots` says when `out/` is snapshotted; by default every `--snapshot-every` seconds and at each run's
    end, into `<results>/<label>/snapshots/`."""
    if not core_found(args.core_src):
        print("shards: ERROR: this interpreter does not import the core from --core-src", file=sys.stderr)
        return CORE_MISMATCH
    from expcore.campaign import out_snapshot

    if args.tmpfs.is_symlink() or not args.tmpfs.is_dir():
        print("shards: ERROR: --tmpfs must be a directory, not a link", file=sys.stderr)
        return 2
    if (args.study / "out").exists():
        print("shards: ERROR: the study copy holds an out/ of its own", file=sys.stderr)
        return 2
    if os.path.lexists(args.study / PLATFORM):  # the step places its federation's there
        raise ShardsError(f"the study copy holds a {PLATFORM} of its own: the step places its federation's beside it")
    spec = {"index": args.index, "count": args.count, "label": args.label}
    _label_ok(args.index, args.count, args.label)
    published = [args.results / f"{args.label}.out.{kind}" for kind in ("tar", "json")]
    snapdir = args.results / args.label / SNAPSHOTS
    if not args.resume and (any(path.exists() for path in published) or out_snapshot.snapshots(snapdir)):
        print(f"shards: ERROR: shard {args.label} has results or a snapshot already: --resume continues it",
              file=sys.stderr)  # fmt: skip
        return 2
    if args.resume:  # an earlier step killed outright left its directory here: removed, or the resume refused
        clear_leftovers(args.tmpfs, spec, os.environ.get("EXPCORE_FED_ROOT"), args.results)
    work_root = sandbox_work_root(os.environ.get("EXPCORE_FED_ROOT"))
    if work_root is not None:  # the Apptainer sandbox's, which it refuses to start without
        make_work_root(work_root, args.tmpfs)
    base = archive_named(args.results, args.label)  # the archive this step starts from: its snapshots name it
    work = Path(tempfile.mkdtemp(prefix=f"{WORK_PREFIX}{args.label}.", dir=args.tmpfs))
    keep = False
    try:
        work.chmod(0o700)
        owner = {"format": OWNER_FORMAT, "shard": spec, "pid": os.getpid(), "start": _start_of(os.getpid()),
                 "root": os.environ.get("EXPCORE_FED_ROOT"), "results": str(args.results), "base": base,
                 "phase": RUNNING}  # fmt: skip
        (work / OWNER).write_text(json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8")
        (work / OWNER_ENDING).write_text(json.dumps({**owner, "phase": ARCHIVING}, sort_keys=True) + "\n", "utf-8")
        copy = work / "study"
        shutil.copytree(args.study, copy, symlinks=True)
        platform = Path(os.environ.get("EXPCORE_FED_ROOT") or "/nonexistent") / PLATFORM
        if platform.is_file():  # a copy, never a link out of the study; its paths are the root's, absolute (`up`)
            shutil.copyfile(platform, copy / PLATFORM)
            (copy / PLATFORM).chmod(0o600)
        if args.resume:
            resume_into(args, spec, base, work, copy / "out")
        for name in ("home", "tmp"):
            (work / name).mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
        env |= {"HOME": str(work / "home"), "TMPDIR": str(work / "tmp")}
        journals = args.results / args.label / JOURNALS
        journals.mkdir(parents=True, exist_ok=True)
        journal = journals / f"{_stamp()}-{os.getpid()}-{secrets.token_hex(4)}.jsonl"
        argv = [sys.executable, "-I", "-m", "expcore", "run", str(copy), "--spend-journal", str(journal),
                "--shard", f"{args.index}/{args.count}", "--shard-label", args.label]  # fmt: skip
        out = copy / "out"
        live = threading.Event()
        live.set()
        if snapshots is None:
            facts = {"shard": spec, "base": base}
            snapshots = Snapshots(
                taker(snapdir, work / "snapshot-scratch", facts, live),
                getattr(args, "snapshot_every", SNAPSHOT_EVERY_S),
                progress=lambda: out_snapshot.ended_attempts(out),
            )
        snapshots.start(out)
        try:
            status = runner(argv, env, work)
            _ended(work, status)  # its out/ is whole: no cleanup removes it until it is archived
        finally:
            live.clear()  # the dispatcher has exited: the last snapshot reads a private copy of the store
            snapshots.stop(out)
        settle_store(out)
        if not out.is_dir():
            print(f"shards: shard {args.label} left no out/ to archive (exit status {status})", file=sys.stderr)
            return status
        try:
            manifest = archive(out, args.results, spec, status)
        except (OSError, tarfile.TarError) as error:  # the only whole out/ is the tmpfs copy: kept, never removed
            keep = True
            mark = {"shard": spec, "exit_status": status, "error": type(error).__name__}
            (work / ARCHIVE_FAILED_MARK).write_text(json.dumps(mark, sort_keys=True) + "\n", encoding="utf-8")
            print(f"shards: ERROR: shard {args.label}'s out/ could not be archived ({type(error).__name__}); it is "
                  f"kept at {out} until `shards.py step --archive-left` archives it", file=sys.stderr)  # fmt: skip
            return ARCHIVE_FAILED
        print(f"shards: shard {args.label} archived: {manifest['files']} files, sha256 {manifest['sha256']}")
        return status
    finally:
        if not keep:
            remove_tree(work)


# ------------------------------------------------------------------------------------------------ the command line
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shards.py", description="One campaign shard per HPC federation.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "lines", "collect"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name == "collect":
            command.add_argument("--into", type=Path, required=True)
            command.add_argument("--run", action="store_true")
    one = sub.add_parser("step")
    for key in ("study", "tmpfs", "results", "core_src"):
        one.add_argument(f"--{key.replace('_', '-')}", dest=key, type=Path, required=True)
    one.add_argument("--index", type=int, required=True)
    one.add_argument("--count", type=int, required=True)
    one.add_argument("--label", required=True)
    one.add_argument("--resume", action="store_true")
    one.add_argument("--archive-left", dest="archive_left", action="store_true")
    one.add_argument("--snapshot-every", dest="snapshot_every", type=float, default=SNAPSHOT_EVERY_S)
    left = sub.add_parser("sweep")
    left.add_argument("--tmpfs", type=Path, required=True)
    left.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "sweep":
            return sweep(args.tmpfs, args.root)
        if args.command == "step":
            return archive_left(args) if args.archive_left else step(args)
        plan = load_plan(args.manifest)
        if args.command == "collect":
            line = merge_argv(plan, collect(plan, args.into))
            print(shlex.join(line))
            return subprocess.run(line, check=False).returncode if args.run else 0
        missing = undeclared_data(plan)
        if missing:
            raise ShardsError(
                f"a campaign on {len(plan.shards)} federations is one only where each declares every space's data "
                f"(data_manifest in its platform.toml, C06); undeclared: {'; '.join(missing)}"
            )
        script = Path(__file__).resolve()
        for shard in plan.shards:
            fed = federation(shard)
            line = step_argv(plan, shard, fed, script)
            print(json.dumps({"shard": shard.label, "argv": line}) if args.command == "plan" else shlex.join(line))
        return 0
    except ShardsError as error:
        print(f"shards: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
