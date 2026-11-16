"""shards.py: one shard of a campaign per HPC federation (#154, milestone M5), from launch to merge.

    uv run python deploy/hpc-federation/shards.py plan    --manifest shards.toml
    uv run python deploy/hpc-federation/shards.py lines   --manifest shards.toml
    <python> -I deploy/hpc-federation/shards.py step   --study DIR --index I --count N --label L --tmpfs DIR \
        --results DIR --core-src DIR                    # inside the step on the sandbox node; `lines` writes it
    uv run python deploy/hpc-federation/shards.py collect --manifest shards.toml --into DIR [--run]

**Where the dispatcher runs.** One shard runs per federation, as one job step on that federation's sandbox node: the
Apptainer sandbox's data gate listens on that node's loopback and its cells are that node's `apptainer exec`s, so the
dispatcher (`python -m expcore run STUDY --shard i/N --shard-label L`) must run there, never on the login node. A step
is launched from the login node alone (srun cannot start one on a compute node), as the federation's own services are
(`DESIGN.md`): `lines` prints each shard's `srun --jobid=<the federation's job> --exact ... /usr/bin/env -i <allowlist>
<python> -I shards.py step ...`, to run detached (`setsid nohup ... &`). Nothing here runs srun.

**How it reaches its federation.** The step's environment is an allowlist: `PATH`, `LC_ALL`, and the federation's
`EXPCORE_FED_ROOT_PARENT` and `EXPCORE_FED_ROOT`. The study's plugin loads `<root>/platform.toml`, where the
federation's `up` rendered it, with every path in it relative to the root (secrets, CA, fault journal) on the
filesystem the nodes see. No secret is in the environment: a model's key comes from its key file.

**The study and its store.** The study is one stamped copy at the pinned commit on the shared filesystem. `step` copies
it to the node's tmpfs and runs it there, since the campaign's store (SQLite, and its `campaign.lock`) must not live on
a network filesystem; it checks first that the interpreter imports the core it was given (`--core-src`), relays SIGTERM
to the dispatcher, and ends with the dispatcher's exit status.

**How a shard comes back.** Once the dispatcher has exited (its lock released), `step` writes the copy's `out/` whole,
`campaign.lock` included, to `<results>/<label>.out.tar` with a manifest beside it (`<label>.out.json`: its SHA-256,
bytes, files, the shard's spec and the dispatcher's exit status), each written whole and renamed into place, and removes
its tmpfs directory. A later start of the same shard (`--resume`) unpacks that archive into its copy first. `collect`
verifies every shard's archive against its manifest and the manifest against the plan, unpacks each into
`<into>/<label>/out`, read-only, and prints (or with `--run` runs) `python -m expcore merge`. A shard whose archive has
no `campaign.lock`, does not verify, or is missing is refused, naming its label: the merge would refuse it too.

**Faults.** A campaign with `[faults]` runs here too: the platform layer reaches the federation's login-node fault relay
through request and answer files in `<root>/fault-relay/` on the shared root (M4's `HpcDriver`), never a step of its
own, and `EXPCORE_FED_ROOT` is how it finds that root. Outage runs on HPC depend on that relay.

**Limits.** A step killed outright (SIGKILL, a node's end) loses what its shard did since its last archive, its `out/`
being on the node's tmpfs: `Snapshots` is the hook a periodic snapshot plugs into, not built yet. The shard's step
must be sized for the sandbox, the dispatcher and the store together.

The manifest (`shards.toml`, operator-local, never committed, since it names the site's paths):

    study = "/shared/study"           # the stamped study copy
    python = "/shared/venv/bin/python" # the core's interpreter, on the filesystem the nodes see
    core_src = "/shared/core/src"     # what that interpreter must import the core from
    results = "/shared/results"       # where each shard's archive and manifest come back
    tmpfs = "/tmp"                    # the node's tmpfs, never Slurm's per-job TMPDIR (a link into scratch)
    step_path = "/usr/bin:/bin"       # the step's PATH
    cpus = 6
    memory = "18g"
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
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

FORMAT = "expcore-hpc-shard/1"
STATE_FORMAT = "expcore-hpc-federation/1"
STATE_FILE = "hpc-federation.json"
LOCK_NAME = "campaign.lock"  # expcore.campaign.lock.LOCK_NAME: the merge refuses a shard without it
ALLOWLIST = ("PATH", "LC_ALL", "EXPCORE_FED_ROOT_PARENT", "EXPCORE_FED_ROOT")
CORE_MISMATCH = 97  # the step's interpreter imports another core than the one it was given
CHUNK = 1 << 20


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
    if set(data) != keys:
        raise ShardsError(f"the manifest's keys must be exactly: {', '.join(sorted(keys))}")
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
                Path(data["tmpfs"]), str(data["step_path"]), data["cpus"], data["memory"], tuple(shards))  # fmt: skip


def federation(shard: Shard) -> dict[str, Any]:
    """The federation's state as its `up` wrote it: its held job, the allocation's name, and the sandbox's node."""
    try:
        state = json.loads((shard.root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ShardsError(f"shard {shard.label}: its federation's state file could not be read") from None
    if not isinstance(state, Mapping) or state.get("format") != STATE_FORMAT:
        raise ShardsError(f"shard {shard.label}: its federation's state file is not {STATE_FORMAT}")
    allocation = state.get("allocation") or {}
    job, name = allocation.get("job"), allocation.get("name")
    node = shard.node or (state.get("sandbox") or {}).get("node")
    if not (isinstance(job, str) and job.isdigit() and isinstance(name, str) and name):
        raise ShardsError(f"shard {shard.label}: its federation has no held job on record")
    if not isinstance(node, str) or not node:
        raise ShardsError(f"shard {shard.label}: no sandbox node: the state file names none and the manifest neither")
    return {"job": job, "name": name, "node": node}


def step_argv(plan: Plan, shard: Shard, fed: Mapping[str, str], script: Path) -> list[str]:
    """The login node's `srun` of one shard's step (the module's docstring): `--exact`, never `--overlap`; an
    environment of the allowlist alone, set on the node by `env -i`, with no secret in it."""
    env = [f"PATH={plan.step_path}", "LC_ALL=C", f"EXPCORE_FED_ROOT_PARENT={shard.root.parent}",
           f"EXPCORE_FED_ROOT={shard.root}"]  # fmt: skip
    step = [plan.python, "-I", str(script), "step", "--study", str(plan.study), "--index", str(shard.index),
            "--count", str(shard.count), "--label", shard.label, "--tmpfs", str(plan.tmpfs),
            "--results", str(plan.results), "--core-src", str(plan.core_src)]  # fmt: skip
    return [
        "srun", f"--jobid={fed['job']}", "--exact", "-N1", "-n1", "-w", fed["node"], "-c", str(plan.cpus),
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


def verified(results: Path, shard: Shard) -> tuple[Path, dict[str, Any]]:
    """A shard's archive and manifest, shown to be this shard's and whole; `ShardsError` naming the label otherwise."""
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
    if not tar_path.is_file() or _sha256(tar_path) != manifest.get("sha256"):
        raise ShardsError(f"shard {shard.label}: its archive is missing or not the one its manifest names")
    return tar_path, manifest


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
    found = [verified(plan.results, shard) for shard in plan.shards]
    if into.exists() and any(into.iterdir()):
        raise ShardsError("the directory to collect into must be new or empty")
    outs = []
    for shard, (tar_path, _) in zip(plan.shards, found, strict=True):
        out = unpack(tar_path, into / shard.label)
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


def run_dispatcher(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    """Run the dispatcher, relaying SIGTERM and SIGINT to it, and return its exit status (128 + N for a signal N)."""
    child = subprocess.Popen(list(argv), env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL)

    def relay(signum: int, _frame: Any) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, relay)
    code = child.wait()
    return 128 - code if code < 0 else code


class Snapshots:
    """The hook of a periodic snapshot of a running shard's `out/` (not built yet: the coordinator's limit 1).

    `take(out)` is called every `every_s` seconds from a thread of its own while the dispatcher runs, and once after
    it has exited, before the archive; one that raises is reported and never ends the run. A snapshot is to copy
    `out/runs/` first and the store after it, into a fresh directory, verified, then swapped in by rename, so a kill
    at any point leaves the last good one."""

    def __init__(self, take: Callable[[Path], None], every_s: float = 900.0) -> None:
        self.take, self.every_s = take, every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _once(self, out: Path) -> None:
        try:
            self.take(out)
        except Exception as error:  # a snapshot that failed leaves the last good one: reported, the run goes on
            print(f"shards: a snapshot of out/ failed ({type(error).__name__})", file=sys.stderr)

    def start(self, out: Path) -> None:
        def loop() -> None:
            while not self._stop.wait(self.every_s):
                if out.is_dir():
                    self._once(out)

        self._thread = threading.Thread(target=loop, daemon=True, name="shard-snapshots")
        self._thread.start()

    def stop(self, out: Path) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if out.is_dir():
            self._once(out)


def step(
    args: argparse.Namespace,
    *,
    runner: Callable[[Sequence[str], Mapping[str, str], Path], int] = run_dispatcher,
    core_found: Callable[[Path], bool] = imports_core_from,
    snapshots: Snapshots | None = None,
) -> int:
    """One shard on its node (the module's docstring): copy, run, archive, clean up; the dispatcher's exit status.
    `snapshots` is the hook of limit 1, None until it is built."""
    if not core_found(args.core_src):
        print("shards: ERROR: this interpreter does not import the core from --core-src", file=sys.stderr)
        return CORE_MISMATCH
    if args.tmpfs.is_symlink() or not args.tmpfs.is_dir():
        print("shards: ERROR: --tmpfs must be a directory, not a link", file=sys.stderr)
        return 2
    if (args.study / "out").exists():
        print("shards: ERROR: the study copy holds an out/ of its own", file=sys.stderr)
        return 2
    spec = {"index": args.index, "count": args.count, "label": args.label}
    _label_ok(args.index, args.count, args.label)
    work = Path(tempfile.mkdtemp(prefix=f"expcore-shard-{args.label}.", dir=args.tmpfs))
    try:
        work.chmod(0o700)
        copy = work / "study"
        shutil.copytree(args.study, copy, symlinks=True)
        if args.resume and (args.results / f"{args.label}.out.tar").exists():
            shard = Shard(args.index, args.count, args.label, Path("/"))
            tar_path, _ = verified(args.results, shard)
            shutil.move(unpack(tar_path, work / "previous"), copy / "out")
        for name in ("home", "tmp"):
            (work / name).mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
        env |= {"HOME": str(work / "home"), "TMPDIR": str(work / "tmp")}
        argv = [sys.executable, "-I", "-m", "expcore", "run", str(copy), "--shard", f"{args.index}/{args.count}",
                "--shard-label", args.label]  # fmt: skip
        out = copy / "out"
        if snapshots is not None:
            snapshots.start(out)
        try:
            status = runner(argv, env, work)
        finally:
            if snapshots is not None:
                snapshots.stop(out)
        if out.is_dir():
            manifest = archive(out, args.results, spec, status)
            print(f"shards: shard {args.label} archived: {manifest['files']} files, sha256 {manifest['sha256']}")
        else:
            print(f"shards: shard {args.label} left no out/ to archive (exit status {status})", file=sys.stderr)
        return status
    finally:
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
    args = parser.parse_args(argv)
    try:
        if args.command == "step":
            return step(args)
        plan = load_plan(args.manifest)
        if args.command == "collect":
            line = merge_argv(plan, collect(plan, args.into))
            print(shlex.join(line))
            return subprocess.run(line, check=False).returncode if args.run else 0
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
