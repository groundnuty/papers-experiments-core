"""shards.py: one shard of a campaign per HPC federation (#154, milestone M5), from launch to merge.

    uv run python deploy/hpc-federation/shards.py plan    --manifest shards.toml
    uv run python deploy/hpc-federation/shards.py lines   --manifest shards.toml
    <python> -I deploy/hpc-federation/shards.py step   --study DIR --index I --count N --label L --tmpfs DIR \
        --results DIR --core-src DIR                    # inside the step on the sandbox node; `lines` writes it
    uv run python deploy/hpc-federation/shards.py collect --manifest shards.toml --into DIR [--run]
    <python> -I deploy/hpc-federation/shards.py step ... --archive-left  # an out/ a step could not archive
    # a step's line may end in: --pre-step REL --pre-step-sha256 HEX --pre-step-timeout S  (`lines` writes them)
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
`EXPCORE_FED_ROOT_PARENT` and `EXPCORE_FED_ROOT`, and no home: `HOME`, the XDG base directories, Apptainer's, uv's and
pip's directories under a path nobody can make, Python's user site off and no git configuration (`home_env`), so no
tool falls back to the account's home, which every job and agent of the account shares. The dispatcher's are in the
step's own directory on the tmpfs, made private there. `step` copies the federation's `<root>/platform.toml`, where its
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

**The study's own refusals: a pre-step guard.** A study that refuses a campaign it must not run (one worker, each
model's cap exactly 1, a federation of its own) can have every step assert that again at its start, before anything is
made. The manifest's `pre_step` names a script of the study, a path relative to the study's root: refused when it is
absolute, has an empty, `.` or `..` part, or leads through a link (a part, or the file itself), is not a regular file or
is over 1 MiB. `plan` and `lines` read it and record its SHA-256 (`plan` prints it with the guard's path and bound) and
each line carries `--pre-step REL --pre-step-sha256 HEX --pre-step-timeout S`; no line is made from a plan that has not
pinned it, and with no `pre_step` the line and the `plan` output are what they were. `step` does this before anything
else, before the core check, before a `--resume` removes a killed step's directory, and before it makes the sandbox's
work root, its own directory, the journals or an archive. It opens the study's root (`--study`, the stamped copy, since
its tmpfs copy does not exist yet) once and holds it: refused unless it is a directory of this user's that no group or
other can write to; reads the guard part by part below that descriptor (no link followed), refuses a file whose
SHA-256 is not the one on its line, and runs those same bytes, never the file read again, as
`<python> -I -B - <label> <campaign.toml>` (the script on the interpreter's stdin), with the held root as its working
directory and `campaign.toml` named through it (a path under `/proc/self/fd`, valid in the guard alone), so a root
renamed after the check is never the one it runs in. Its environment is `ALLOWLIST` and the step's home below a path
nobody can make, and it runs in a session of its own, killed with everything it started when it does not end within
`pre_step_timeout_s` (60 s by default, above 0 and at most 600), or when the step is told to stop. A non-zero exit, a
signal, a timeout, a hash mismatch or a guard that cannot be read refuses the step with `PRE_STEP_REFUSED` (73), naming
the guard and why, and nothing is made. A step that passes records `{path, sha256, exit_status}` as `pre_step` in
its `step.json` and in its archive's manifest. The line carries the guard, so a line run as it was printed runs it; a
hand-edited line that drops the three words runs without it, and `--archive-left`, which starts no work, ignores them.

**The study's own check of its results: a post-run script.** Symmetrical, for the check that needs the live
federation (the study's own owner check of what the shard left in it). The manifest's `post_run` names a script of
the study (the same path rules; `post_run_timeout_s`, 300 s by default, above 0 and at most 1800), `plan` and `lines`
record its SHA-256 and each line also carries `--post-run REL --post-run-sha256 HEX --post-run-timeout S`. `step` reads
and hashes it with the guard, before anything is made (one that is a link, missing or not the plan's hash refuses the
step with 73, so a campaign is never spent on a check that cannot run), holds its bytes, and runs them once the
dispatcher has exited 0, while the federation is up and before the archive is written: `<python> -I -B - <label>
<out/> <dispatcher's exit status>`, the study copy's root on the node's tmpfs as its working directory, in the
dispatcher's own environment (`ALLOWLIST`, the step's home and TMPDIR), in a session of its own, killed at its bound.
Its exit 0 makes the shard's results final. A non-zero exit, a signal, a timeout, or a dispatcher that did not exit 0
(the script is not run) leaves them NOT final: they are archived all the same, so nothing is lost, with `final: false`
and its reason in the manifest, a `NOT-FINAL.json` in the archived `out/`, and the step ends with `POST_RUN_FAILED`
(74) unless the dispatcher's own status was not 0. The record of the script (`post_run`: path, SHA-256, whether it ran,
its exit status, `final`) is in `step.json` and the manifest. `collect` refuses a not-final shard, naming it and its
reason, and, when the plan declares a post-run, a shard whose manifest does not carry that script's hash with `final`
true (a line that dropped the words); the core's `merge` refuses an `out/` holding `NOT-FINAL.json`. `--resume` starts
from such an archive as from any and decides afresh; `--archive-left` archives a kept `out/` as final only when the
step's record says its script passed.

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
    pre_step = "guards/pre_step.py"   # optional: a script of the study that each step runs first (see above)
    pre_step_timeout_s = 60           # optional, with pre_step: its bound in seconds (above 0, at most 600)
    post_run = "guards/post_run.py"   # optional: a script of the study that each step runs after its dispatcher exits 0
    post_run_timeout_s = 300          # optional, with post_run: its bound in seconds (above 0, at most 1800)
    [[shards]]                        # in order: shard i is the i-th entry
    label = "fed-a"                   # the shard's label (no host name: it travels with the export)
    root = "/shared/fed/a"            # the federation's root, holding hpc-federation.json and platform.toml
    sandbox_node = "..."              # optional when the state file names the sandbox's node
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
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
from dataclasses import dataclass, replace
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
PRE_STEP_REFUSED = 73  # a script the study declared refused the step, or could not be run as declared: nothing made
POST_RUN_FAILED = 74  # the dispatcher exited 0 and the study's post-run script did not: archived, marked not final
PRE_STEP_TIMEOUT_S = 60.0  # a guard's bound when the manifest names none
PRE_STEP_MAX_S = 600.0  # the longest bound a manifest may give a guard
POST_RUN_TIMEOUT_S = 300.0  # a post-run script's bound when the manifest names none
POST_RUN_MAX_S = 1800.0  # the longest bound a manifest may give a post-run script
HOOK_MAX_BYTES = 1 << 20  # a declared script is at most this long
CAMPAIGN = "campaign.toml"  # the study's stamped campaign file: the guard's second argument
NOT_FINAL = "NOT-FINAL.json"  # expcore.campaign.shard.NOT_FINAL_FILE: in a not-final out/, which the merge refuses
NOT_FINAL_FORMAT = "expcore-hpc-shard-not-final/1"
HOOKS = {  # what each declared script is called in a message, its default bound and its longest
    "pre_step": ("pre-step guard", PRE_STEP_TIMEOUT_S, PRE_STEP_MAX_S),
    "post_run": ("post-run script", POST_RUN_TIMEOUT_S, POST_RUN_MAX_S),
}
CHUNK = 1 << 20
SNAPSHOT_EVERY_S = 900.0  # a running shard's out/ is snapshotted this often, and after each run's end
POLL_S = 30.0  # how often the snapshot thread looks for a run's end in the store
SNAPSHOTS, JOURNALS = "snapshots", "journals"  # under <results>/<label>/
WORK_PREFIX = "expcore-shard-"  # a step's directory on the node's tmpfs: <tmpfs>/expcore-shard-<label>.<random>
OWNER, OWNER_FORMAT = "step.json", "expcore-hpc-shard-step/1"  # in it: the step's process, campaign and federation
OWNER_ENDING = ".step.ended.json"  # written at the start, renamed over step.json when the dispatcher ends
OWNER_STATUS = ".step.status.json"  # the same with the dispatcher's exit status, written when it ends, if it can be
OWNER_POST = ".step.post.json"  # the same with the post-run script's record, written when it ends
RUNNING, ARCHIVING = "running", "archiving"  # the step's phase: its dispatcher runs; it ended and out/ is whole
ARCHIVE_FAILED_MARK = "ARCHIVE-FAILED.json"  # in it: the out/ in it could not be archived, so it is kept
PLATFORM = "platform.toml"  # the federation's, in its root; the step's copy of the study has it beside study.toml


# The step's HOME, and every directory a tool writes under one, until it has made its directory on the tmpfs: a path
# under a file, which nobody can make (`hpclaunch.NO_HOME`); the dispatcher's are in that directory (`step`).
NO_HOME = "/dev/null/hpcop-home"
HOME_DIRS = (".config", ".cache", ".local", ".local/share", ".local/state", ".apptainer", ".apptainer/cache")


def home_env(home: str) -> dict[str, str]:
    """A step's own home, so that no tool falls back to the account's home, which every job and agent of the account
    shares: as `hpcslurm.home_env` gives it (this file runs on a node by itself, so it keeps its own copy; a test
    compares the two)."""
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "APPTAINER_CONFIGDIR": f"{home}/.apptainer",
        "APPTAINER_CACHEDIR": f"{home}/.apptainer/cache",
        "UV_CACHE_DIR": f"{home}/.cache/uv",
        "PIP_CACHE_DIR": f"{home}/.cache/pip",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


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
    pre_step: str | None = None  # the study's pre-step guard: a path relative to its root
    pre_step_sha256: str | None = None  # its SHA-256, recorded by `pinned` when `plan` and `lines` read it
    pre_step_timeout_s: float = PRE_STEP_TIMEOUT_S
    post_run: str | None = None  # the study's post-run script: a path relative to its root
    post_run_sha256: str | None = None  # its SHA-256, recorded by `pinned`
    post_run_timeout_s: float = POST_RUN_TIMEOUT_S


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
    optional = {"snapshot_every_s", "pre_step", "pre_step_timeout_s", "post_run", "post_run_timeout_s"}
    if not keys <= set(data) <= keys | optional:
        raise ShardsError(
            f"the manifest's keys must be: {', '.join(sorted(keys))}, and perhaps {', '.join(sorted(optional))}"
        )
    every = data.get("snapshot_every_s", SNAPSHOT_EVERY_S)
    if isinstance(every, bool) or not isinstance(every, int | float) or not 60 <= every < float("inf"):
        raise ShardsError("snapshot_every_s must be a number of seconds, at least 60")
    bounds = {}
    for key, (_, default, maximum) in HOOKS.items():
        if key in data and (problem := guard_path_problem(data[key])):
            raise ShardsError(f"{key} {problem}")
        if f"{key}_timeout_s" in data and key not in data:
            raise ShardsError(f"{key}_timeout_s is the bound of a {key}, which the manifest does not declare")
        bounds[key] = data.get(f"{key}_timeout_s", default)
        if problem := bound_problem(bounds[key], maximum):
            raise ShardsError(f"{key}_timeout_s {problem}")
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
                float(every), data.get("pre_step"), None, float(bounds["pre_step"]), data.get("post_run"), None,
                float(bounds["post_run"]))  # fmt: skip


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
           f"EXPCORE_FED_ROOT={shard.root}", *(f"{k}={v}" for k, v in home_env(NO_HOME).items())]  # fmt: skip
    step = [plan.python, "-I", str(script), "step", "--study", str(plan.study), "--index", str(shard.index),
            "--count", str(shard.count), "--label", shard.label, "--tmpfs", str(plan.tmpfs),
            "--results", str(plan.results), "--core-src", str(plan.core_src),
            "--snapshot-every", f"{plan.snapshot_every_s:g}"]  # fmt: skip
    if plan.pre_step is not None:  # the study's guard rides on the line: `step` runs it first (`run_pre_step`)
        if plan.pre_step_sha256 is None:
            raise ShardsError(
                "the pre-step guard is not pinned: no line is made from a plan that has not recorded its hash"
            )
        step += ["--pre-step", plan.pre_step, "--pre-step-sha256", plan.pre_step_sha256,
                 "--pre-step-timeout", f"{plan.pre_step_timeout_s:g}"]  # fmt: skip
    if plan.post_run is not None:  # and the study's post-run script: `step` runs it before it archives (`run_post_run`)
        if plan.post_run_sha256 is None:
            raise ShardsError("the post-run script is not pinned: no line is made from a plan that has not hashed it")
        step += ["--post-run", plan.post_run, "--post-run-sha256", plan.post_run_sha256,
                 "--post-run-timeout", f"{plan.post_run_timeout_s:g}"]  # fmt: skip
    return [
        "srun", f"--jobid={fed['job']}", *([f"--het-group={fed['het_group']}"] if fed.get("het_group") else []),
        "--exact", "-N1", "-n1", "-w", fed["node"], "-c", str(plan.cpus),
        f"--mem={plan.memory}", "-J", f"{fed['name']}-shard-{shard.index}", "--export=PATH",
        "/usr/bin/env", "-i", *env, *step,
    ]  # fmt: skip


# ------------------------------------------------------------------------------------------------ the pre-step guard
def guard_path_problem(path: Any) -> str | None:
    """Why `path` cannot name a file of the study (a plain relative path: no empty, `.` or `..` part), or None."""
    if isinstance(path, bool) or not isinstance(path, str) or not path or "\0" in path:
        return "must be a path, a string, relative to the study's root"
    if path.startswith("/"):
        return "must be relative to the study's root, never absolute"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "must name a file inside the study: no empty, `.` or `..` part"
    return None


def bound_problem(bound: Any, maximum: float = PRE_STEP_MAX_S) -> str | None:
    """Why `bound` is not a declared script's bound (seconds above 0 and at most `maximum`), or None."""
    if isinstance(bound, bool) or not isinstance(bound, int | float) or not 0 < bound <= maximum:
        return f"must be a number of seconds above 0 and at most {maximum:g}"
    return None


def read_below(root_fd: int, path: str, name: str) -> bytes:
    """The bytes of the file `path` below the directory `root_fd` holds: each part opened with `O_NOFOLLOW` from the
    directory before it, so that a link anywhere on the way, or a part swapped for one after a check, never leads out of
    the study; a regular file of at most `HOOK_MAX_BYTES`. `ShardsError` naming `name`, never the study's own path."""
    if problem := guard_path_problem(path):
        raise ShardsError(f"{name} {problem}")
    parts, fd = path.split("/"), os.dup(root_fd)
    try:
        for n, part in enumerate(parts):
            last = n == len(parts) - 1  # the file itself may be a FIFO that blocks: never waited on, and refused below
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            below = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = below
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ShardsError(f"{name} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, CHUNK):
            size += len(chunk)
            if size > HOOK_MAX_BYTES:
                raise ShardsError(f"{name} is over {HOOK_MAX_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        why = {errno.ENOENT: "it does not exist", errno.ELOOP: "it is, or lies behind, a link",
               errno.ENOTDIR: "a part of its path is a link or not a directory"}  # fmt: skip
        raise ShardsError(f"{name} cannot be read: {why.get(error.errno, type(error).__name__)}") from None
    finally:
        os.close(fd)


def read_in_study(study: Path, key: str, path: str) -> bytes:
    """The bytes of the study's declared script `path` (`key` says which), read below the study's root."""
    name = f"{HOOKS[key][0]} {path}"
    try:
        fd = os.open(study, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise ShardsError(f"{name} cannot be read: the study's root cannot be opened") from None
    try:
        return read_below(fd, path, name)
    finally:
        os.close(fd)


def pinned(plan: Plan) -> Plan:
    """The plan with the SHA-256 of each declared script (its pre-step guard, its post-run script) recorded, from the
    study as it is now: `plan` and `lines` call it, so that a script that leaves the study, is a link or is missing
    refuses them before a line is printed. The same plan when none is declared."""
    found = {}
    for key in HOOKS:
        path = getattr(plan, key)
        if path is not None:
            found[f"{key}_sha256"] = hashlib.sha256(read_in_study(plan.study, key, path)).hexdigest()
    return replace(plan, **found)


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


def archive(
    out: Path,
    results: Path,
    spec: Mapping[str, Any],
    exit_status: int,
    pre_step: Mapping[str, Any] | None = None,
    post_run: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write `out/` whole to `<results>/<label>.out.tar` and its manifest; the manifest, as written. `pre_step`, the
    record of the guard the step ran (`start_hooks`), and `post_run`, the record of the post-run script, are in it only
    when there is one; with a post-run record it also says `final`, whether that script passed."""
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
        **({"pre_step": dict(pre_step)} if pre_step is not None else {}),
        **({"post_run": dict(post_run), "final": post_run.get("final") is True} if post_run is not None else {}),
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


def final_problem(shard: Shard, manifest: Mapping[str, Any], wanted: str | None) -> str | None:
    """Why a verified shard's results are not to be merged, or None: its manifest says they are not final (the
    study's post-run script did not pass), or, with `wanted` (the plan's SHA-256 of the post-run script), does not say
    that this script passed (a step whose line dropped it)."""
    record = manifest.get("post_run")
    if manifest.get("final") is False:
        reason = record.get("reason") if isinstance(record, Mapping) else None
        return f"shard {shard.label}: its results are not final ({reason or 'its post-run script did not pass'})"
    if wanted is not None and not (
        manifest.get("final") is True and isinstance(record, Mapping) and record.get("sha256") == wanted
    ):
        return (
            f"shard {shard.label}: its step did not run the plan's post-run script to a pass: its results are not final"
        )
    return None


def collect(plan: Plan, into: Path) -> list[Path]:
    """Every shard's out/, verified and unpacked read-only into `<into>/<label>/out`; refused whole if any fails, and
    when any is not final (`final_problem`), naming it."""
    if into.exists() and any(into.iterdir()):
        raise ShardsError("the directory to collect into must be new or empty")
    wanted = None
    if plan.post_run is not None:  # the script the plan declares, as the study holds it now
        wanted = pinned(replace(plan, pre_step=None)).post_run_sha256
    created = not into.exists()
    into.mkdir(parents=True, exist_ok=True)
    try:
        found = []
        for shard in plan.shards:
            found.append(verified(plan.results, shard, into))
            if problem := final_problem(shard, found[-1][1], wanted):
                raise ShardsError(problem)
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
        if os.path.lexists(out / NOT_FINAL):
            raise ShardsError(f"shard {shard.label}: its out/ is marked not final ({NOT_FINAL})")
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
    guard, post = left.owner.get("pre_step"), left.owner.get("post_run")  # what that step ran; this one runs none
    post = dict(post) if isinstance(post, Mapping) else None
    if post is not None and post.get("final") is not True:  # its script did not pass, or the step ended before it did
        post["reason"] = post.get("reason") or "the step ended before its post-run script passed"
        mark_not_final(out, str(post["reason"]))
    manifest = archive(out, args.results, spec, int(status), guard if isinstance(guard, Mapping) else None, post)
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


class StepRefused(Exception):
    """A script the study declared refused the step, or could not be run as declared; the message names it."""


@dataclass(frozen=True)
class Hook:
    """A script of the study that this step's line declared (`--pre-step`, `--post-run`), its bytes read and hashed at
    the step's start: what runs is these bytes, never the file read again."""

    key: str  # `pre_step` or `post_run`
    path: str
    sha256: str
    timeout: float
    source: bytes

    @property
    def name(self) -> str:
        return f"{HOOKS[self.key][0]} {self.path}"


def pre_step_env() -> dict[str, str]:
    """The guard's environment: the step's allowlist as this step was given it, and the home its line gives (below a
    path nobody can make: its own directory does not exist yet), nothing else."""
    return {k: os.environ[k] for k in ALLOWLIST if k in os.environ} | home_env(NO_HOME)


def run_hook(
    source: bytes,
    args: Sequence[str],
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float,
    *,
    pass_fds: Sequence[int] = (),
) -> int | None:
    """Run a script's `source` as this interpreter's `-I -B -` (its script on its stdin) with `args`, in `cwd`, in a
    session of its own, and return its exit status (negative for a signal). None when it did not end within `timeout`
    seconds. Past the bound, or when this step is told to stop (SIGTERM, SIGINT) while it runs, the script's whole
    process group is killed, whether its leader is alive still or not: the leader is reaped only afterwards, so its
    pid names its group until then. Its stdout and stderr are this step's."""
    child = subprocess.Popen([sys.executable, "-I", "-B", "-", *args], stdin=subprocess.PIPE, env=dict(env), cwd=cwd,
                             start_new_session=True, pass_fds=tuple(pass_fds))  # fmt: skip

    def kill_group(*_: Any) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)

    previous: dict[int, Any] = {}
    with contextlib.suppress(ValueError):  # only the main thread sets handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, kill_group)
    try:
        try:
            child.communicate(source, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group()
            child.wait()
            return None
        except BaseException:
            kill_group()
            child.wait()
            raise
        return child.returncode
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def read_hook(args: argparse.Namespace, key: str, root_fd: int) -> Hook | None:
    """The script this step's line declares under `key`, its bytes read below the study's root `root_fd` and shown to
    be the file the plan hashed; None when the line declares none. `StepRefused` naming it for a declaration that is
    incomplete or not a plain path of the study, a file that cannot be read, or a hash that is not the plan's."""
    label, default, maximum = HOOKS[key]
    flag = key.replace("_", "-")
    path, wanted, bound = (getattr(args, f"{key}{suffix}", None) for suffix in ("", "_sha256", "_timeout"))
    if path is None:
        if wanted is not None or bound is not None:
            raise StepRefused(f"--{flag}-sha256 and --{flag}-timeout belong to a --{flag}, which is not given")
        return None
    name = f"{label} {path}"
    if problem := guard_path_problem(path):
        raise StepRefused(f"{name} {problem}")
    if not isinstance(wanted, str) or not re.fullmatch(r"[0-9a-f]{64}", wanted):
        raise StepRefused(f"{name} has no SHA-256 from the plan: --{flag}-sha256 is 64 hex digits")
    bound = default if bound is None else bound
    if problem := bound_problem(bound, maximum):
        raise StepRefused(f"{name}: --{flag}-timeout {problem}")
    try:
        source = read_below(root_fd, path, name)
    except ShardsError as error:
        raise StepRefused(str(error)) from None
    digest = hashlib.sha256(source).hexdigest()
    if digest != wanted:
        raise StepRefused(f"{name} is not the file the plan hashed: its sha256 is {digest}, the plan's {wanted}")
    return Hook(key, path, wanted, float(bound), source)


def run_guard(guard: Hook, label: str, root_fd: int) -> dict[str, Any]:
    """Run the pre-step guard: in the study's root as `root_fd` holds it (a directory of this user's that no group or
    other can write to), as its working directory and the base of the `campaign.toml` it is given, so a root renamed
    since is never the one it runs in; the record `{path, sha256, exit_status: 0}` when it exits 0, else `StepRefused`
    naming the guard and why (a root that is not ours, no campaign.toml of its own, a guard that cannot start, does not
    end in its bound or does not exit 0)."""
    info = os.fstat(root_fd)
    if info.st_uid != os.getuid():
        problem = "the study's root is not this user's"
    elif stat.S_IMODE(info.st_mode) & 0o022:
        problem = "the study's root can be written by its group or others"
    else:
        problem = None
    if problem:
        raise StepRefused(f"{guard.name} cannot run: {problem}; its working directory must be this user's alone")
    try:
        found = stat.S_ISREG(os.lstat(CAMPAIGN, dir_fd=root_fd).st_mode)
    except OSError:
        found = False
    if not found:
        raise StepRefused(f"{guard.name} cannot run: the study's {CAMPAIGN} is missing, a link or not a file")
    held = f"/proc/self/fd/{root_fd}"
    try:
        status = run_hook(guard.source, [label, f"{held}/{CAMPAIGN}"], held, pre_step_env(), guard.timeout,
                          pass_fds=(root_fd,))  # fmt: skip
    except OSError as error:
        raise StepRefused(f"{guard.name} could not be started ({type(error).__name__})") from None
    if status is None:
        raise StepRefused(f"{guard.name} did not end within {guard.timeout:g} s: it was killed")
    if status < 0:
        raise StepRefused(f"{guard.name} refused the step: it was killed by signal {-status}")
    if status != 0:
        raise StepRefused(f"{guard.name} refused the step: exit status {status}")
    return {"path": guard.path, "sha256": guard.sha256, "exit_status": status}


def start_hooks(args: argparse.Namespace) -> tuple[dict[str, Any] | None, Hook | None]:
    """The step's first act (the module's docstring): the record `{path, sha256, exit_status: 0}` of the pre-step guard
    that passed (None when the line declares none) and the post-run script, read and shown to be the plan's, for the
    step to run after its dispatcher (None when none). Both are read before the guard runs, so that a campaign is never
    started that cannot be checked at its end. `StepRefused` for any that is not as declared; nothing is made."""
    keys = [
        key for key in HOOKS if any(getattr(args, f"{key}{s}", None) is not None for s in ("", "_sha256", "_timeout"))
    ]
    if not keys:
        return None, None
    try:
        root_fd = os.open(os.path.abspath(args.study), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        names = ", ".join(f"{HOOKS[key][0]} {getattr(args, key)}" for key in keys)
        raise StepRefused(f"the study's root cannot be opened, so {names} cannot be read") from None
    try:
        guard, post = read_hook(args, "pre_step", root_fd), read_hook(args, "post_run", root_fd)
        return (None if guard is None else run_guard(guard, args.label, root_fd)), post
    finally:
        os.close(root_fd)


def run_post_run(hook: Hook, label: str, copy: Path, out: Path, env: Mapping[str, str], status: int) -> dict[str, Any]:
    """The post-run script's turn, once the dispatcher has exited with `status`: its record `{path, sha256, ran,
    exit_status, final}`, `final` only when the script ran and exited 0, with a `reason` when it is not. The script
    gets the shard's label, `out` and `status`, runs in `copy` (the study's copy, on the node's tmpfs), and gets the
    dispatcher's own environment. It is not run for a dispatcher that did not exit 0."""
    record: dict[str, Any] = {"path": hook.path, "sha256": hook.sha256, "ran": False, "exit_status": None,
                              "final": False}  # fmt: skip
    if status != 0:
        return {**record, "reason": f"the dispatcher exited {status}: the post-run script was not run"}
    try:
        code = run_hook(hook.source, [label, str(out), str(status)], copy, env, hook.timeout)
    except OSError as error:
        return {**record, "reason": f"the post-run script could not be started ({type(error).__name__})"}
    record["ran"] = True
    if code is None:
        return {**record, "reason": f"the post-run script did not end within {hook.timeout:g} s: it was killed"}
    record["exit_status"] = code
    if code < 0:
        return {**record, "reason": f"the post-run script was killed by signal {-code}"}
    if code != 0:
        return {**record, "reason": f"the post-run script exited with status {code}"}
    return {**record, "final": True}


def note_post_run(work: Path, post: Mapping[str, Any]) -> None:
    """Put the post-run script's record in the step's record (`step.json`), written whole into a file of its own, read
    back and swapped in by rename, as `_ended` does; should that fail (a full tmpfs) the record written at the start
    stands, which says the script did not pass."""
    current = _record(work / OWNER)
    if current is None:
        return
    record = {**current, "post_run": dict(post)}
    try:
        (work / OWNER_POST).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        if _record(work / OWNER_POST) == record:
            os.replace(work / OWNER_POST, work / OWNER)
    except OSError as error:
        print(f"shards: the post-run record could not be written to the step's record ({type(error).__name__})",
              file=sys.stderr)  # fmt: skip


def mark_not_final(out: Path, reason: str) -> None:
    """Mark the shard's `out/` not final, in a file the merge refuses (`NOT_FINAL`); said when it cannot be written
    (the manifest of the archive says it as well, and `collect` reads that)."""
    mark = {"format": NOT_FINAL_FORMAT, "final": False, "reason": reason}
    try:
        (out / NOT_FINAL).write_text(json.dumps(mark, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as error:
        print(
            f"shards: ERROR: the not-final mark could not be written to out/ ({type(error).__name__})", file=sys.stderr
        )


def step(
    args: argparse.Namespace,
    *,
    runner: Callable[[Sequence[str], Mapping[str, str], Path], int] = run_dispatcher,
    core_found: Callable[[Path], bool] = imports_core_from,
    snapshots: Snapshots | None = None,
) -> int:
    """One shard on its node (the module's docstring): copy, restore, run, archive, clean up; the dispatcher's exit
    status. `snapshots` says when `out/` is snapshotted; by default every `--snapshot-every` seconds and at each run's
    end, into `<results>/<label>/snapshots/`. Its first act is the study's pre-step guard, when its line declares one
    (`start_hooks`): nothing is made, removed or read before it, and `PRE_STEP_REFUSED` ends a step it refuses. A
    declared post-run script runs after the dispatcher exits 0 and before the archive (`run_post_run`): a step whose
    script does not pass archives its results marked not final and ends with `POST_RUN_FAILED`."""
    try:
        guard, post_hook = start_hooks(args)
    except StepRefused as error:
        print(f"shards: ERROR: {error}; the step is refused, with nothing made", file=sys.stderr)
        return PRE_STEP_REFUSED
    if guard is not None:
        print(f"shards: pre-step guard {guard['path']} (sha256 {guard['sha256']}) passed for shard {args.label}",
              file=sys.stderr)  # fmt: skip
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
                 "phase": RUNNING, **({"pre_step": guard} if guard is not None else {}),
                 **({"post_run": {"path": post_hook.path, "sha256": post_hook.sha256, "ran": False, "exit_status": None,
                                  "final": False}} if post_hook is not None else {})}  # fmt: skip
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
            (copy / "out" / NOT_FINAL).unlink(
                missing_ok=True
            )  # an archive not final is resumed; this step decides afresh
        for name in ("home", "tmp", *(f"home/{sub}" for sub in HOME_DIRS)):
            (work / name).mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
        env |= {**home_env(str(work / "home")), "TMPDIR": str(work / "tmp")}  # never the account's ~
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
        post = None
        if post_hook is not None:  # the dispatcher has exited and the federation is up: the study's own check
            post = run_post_run(post_hook, args.label, copy, out, env, status)
            note_post_run(work, post)
            if post["final"]:
                print(f"shards: {post_hook.name} passed for shard {args.label}: its results are final", file=sys.stderr)
            else:
                mark_not_final(out, post["reason"])
                print(f"shards: ERROR: {post['reason']}; shard {args.label}'s results are archived, NOT final",
                      file=sys.stderr)  # fmt: skip
            settle_store(out)  # a script that read the store may leave its -shm
        try:
            manifest = archive(out, args.results, spec, status, guard, post)
        except (OSError, tarfile.TarError) as error:  # the only whole out/ is the tmpfs copy: kept, never removed
            keep = True
            mark = {"shard": spec, "exit_status": status, "error": type(error).__name__}
            (work / ARCHIVE_FAILED_MARK).write_text(json.dumps(mark, sort_keys=True) + "\n", encoding="utf-8")
            print(f"shards: ERROR: shard {args.label}'s out/ could not be archived ({type(error).__name__}); it is "
                  f"kept at {out} until `shards.py step --archive-left` archives it", file=sys.stderr)  # fmt: skip
            return ARCHIVE_FAILED
        print(f"shards: shard {args.label} archived: {manifest['files']} files, sha256 {manifest['sha256']}")
        if post is not None and not post["final"]:
            return status or POST_RUN_FAILED
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
    one.add_argument("--pre-step", dest="pre_step", default=None)  # the study's guard: `lines` writes all three
    one.add_argument("--pre-step-sha256", dest="pre_step_sha256", default=None)
    one.add_argument("--pre-step-timeout", dest="pre_step_timeout", type=float, default=None)
    one.add_argument("--post-run", dest="post_run", default=None)  # the study's post-run script: all three
    one.add_argument("--post-run-sha256", dest="post_run_sha256", default=None)
    one.add_argument("--post-run-timeout", dest="post_run_timeout", type=float, default=None)
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
        plan = pinned(plan)  # each declared script read and hashed before any line is printed
        script = Path(__file__).resolve()
        for shard in plan.shards:
            fed = federation(shard)
            line = step_argv(plan, shard, fed, script)
            record = {"shard": shard.label, "argv": line}
            if plan.pre_step is not None:
                record["pre_step"] = {"path": plan.pre_step, "sha256": plan.pre_step_sha256,
                                      "timeout_s": plan.pre_step_timeout_s}  # fmt: skip
            if plan.post_run is not None:
                record["post_run"] = {"path": plan.post_run, "sha256": plan.post_run_sha256,
                                      "timeout_s": plan.post_run_timeout_s}  # fmt: skip
            print(json.dumps(record) if args.command == "plan" else shlex.join(line))
        return 0
    except ShardsError as error:
        print(f"shards: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
