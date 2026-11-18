"""fleetworker.py: the elastic worker's launch inside a federation's held job (#462 step 6; README.md, "The elastic
fleet's worker"). `expcore.campaign.elastic.worker` takes the leases; this starts it on the federation's agent node and
gives its lease runs the operator's key files.

    <python> -I -B deploy/hpc-federation/fleetworker.py launch --fleet DIR --member fed-k --settings FILE \
        --federation LABEL --channel DIR --job-end EPOCH
    <python> -I -B deploy/hpc-federation/fleetworker.py node --federation LABEL --channel DIR --study DIR --tmpfs DIR \
        --root DIR --python EXE [--home-files DIR]

`launch` runs on the site's login node (the controller's join runs it, before the federation joins the ledger): it
starts `node` as a detached `srun` step of the federation's held job (`login_line`), once (a worker whose recorded
client lives is not started again), and records its client in the fleet's record, `worker` of the member.

`node` runs on the node, as a step of the held job (`fleet.py worker` launches it, `login_line` is its `srun`:
`--exact`, never `--overlap`, `--chdir=/`, an environment of the allowlist alone, and `--time` the job's remaining
time). It makes
a private directory on the node's `--tmpfs` named as a shard step's (`shards.WORK_PREFIX`) with the same owner record
(`step.json`: this process, the federation's root, `home_files: true` and a count), so `shards.py sweep`, which `down`
runs on the node, removes what a killed worker leaves once its process is shown gone. In it: `home` (the worker's HOME
and XDG directories, `shards.home_env`), `tmp`, `study` (a copy of the study with the federation's `platform.toml`
beside it, what each lease copies), and `work` (the lease copies). The directory is a sibling of the sandbox's work root
(never under it or the other way round), and never under the federation's root, so no cell sees it. With `--home-files`
the operator's key tree is read once (`shards.read_home_files`) and staged in `home` (directories 0700, files 0600,
`shards.stage_home_files`), and the worker runs with that HOME. The whole directory, the home first and explicitly, is
removed on every way out: the worker's end, a failed start, a stop signal (held while the removal runs, as a shard
step's is). Nothing prints a path, a name or a size of the tree: only `home_files: true` and a count.
"""

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "fleetworker.py"
WORKER_MODULE = "expcore.campaign.elastic.worker"
OWNER_FORMAT_KEY = "worker"  # the owner record's `shard` for a worker: not a shard's spec, never matched as one
REFUSED = 2


def _shards() -> Any:
    """`shards.py` by its path, without putting this directory on `sys.path` (`-I` leaves it off)."""
    loaded = sys.modules.get("hpc_shards")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("hpc_shards", HERE / "shards.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def login_line(
    settings: Mapping[str, Any], fed: Mapping[str, Any], root: Path, name: str, federation: str, channel: str,
    time_s: float,
) -> List[str]:  # fmt: skip
    """The login node's `srun` of one federation's worker (the module's docstring), from the shard settings
    (`fleetplan.read_settings`: python, study, tmpfs, step_path, cpus, memory, and perhaps home_files) and where the
    federation's agent node is (`shards.federation`). `--time` is the job's remaining time in whole minutes, rounded up,
    so the step ends with the job and is never open-ended."""
    shards = _shards()
    env = [f"PATH={settings['step_path']}", "LC_ALL=C", f"EXPCORE_FED_ROOT_PARENT={root.parent}",
           f"EXPCORE_FED_ROOT={root}", *(f"{k}={v}" for k, v in shards.home_env(shards.NO_HOME).items())]  # fmt: skip
    node = [settings["python"], "-I", "-B", str(SCRIPT), "node", "--federation", federation, "--channel", channel,
            "--study", settings["study"], "--tmpfs", settings["tmpfs"], "--root", str(root),
            "--python", settings["python"]]  # fmt: skip
    if settings.get("home_files"):  # the path alone rides here: `node` reads the tree itself
        node += ["--home-files", settings["home_files"]]
    minutes = max(1, -(-int(time_s) // 60))
    group = [f"--het-group={fed['het_group']}"] if fed.get("het_group") else []
    return ["srun", f"--jobid={fed['job']}", *group, "--exact", "-N1", "-n1", "-w", fed["node"],
            "-c", str(settings["cpus"]), f"--mem={settings['memory']}", f"--time={minutes}", "-J", name,
            "--export=PATH", "--chdir=/", "/usr/bin/env", "-i", *env, *node]  # fmt: skip


def worker_name(fed: Mapping[str, Any]) -> str:
    """The name of a federation's worker step: its held job's name and `-worker`."""
    return f"{fed['name']}-worker"


# ------------------------------------------------------------------------------------------------ on the node
def _apart(work_root: Path, root: Path, home: Path) -> Optional[str]:
    """Why `home` cannot be this worker's directory: under the sandbox's work root (a cell binds it) or holding it, or
    under the federation's root; None when it is a sibling of the first and apart from the second."""
    here, held = Path(os.path.realpath(home)), Path(os.path.realpath(root))
    if here == held or here.is_relative_to(held):
        return "the worker's directory is under the federation's root"
    found = Path(os.path.realpath(work_root.parent)) / work_root.name
    if here == found or here.is_relative_to(found) or found.is_relative_to(here):
        return "the worker's directory is under the sandbox's work root, or holds it"
    return None


def _owner(directory: Path, args: argparse.Namespace, count: int) -> None:
    shards = _shards()
    record = {"format": shards.OWNER_FORMAT, "shard": {OWNER_FORMAT_KEY: args.federation}, "pid": os.getpid(),
              "start": shards._start_of(os.getpid()), "root": args.root, "results": args.channel, "base": None,
              "phase": shards.RUNNING,
              **({"home_files": True, "home_files_count": count} if count else {})}  # fmt: skip
    descriptor = os.open(directory / shards.OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def run_worker(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> int:
    """The worker as a child, TERM and INT relayed to it; its exit status (128 + N for a signal N)."""
    child = subprocess.Popen(list(argv), env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL)

    def relay(signum: int, _frame: Any) -> None:
        child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, relay)
    code = child.wait()
    return 128 - code if code < 0 else code


def node(args: argparse.Namespace, runner: Any = None) -> int:
    """`node`: the worker on this node, as the module's docstring says; its exit status, or 2 for a start refused
    with nothing made."""
    shards = _shards()
    runner = runner or run_worker
    tmpfs, study = Path(args.tmpfs), Path(args.study)
    try:
        tree = None
        if args.home_files:
            problem = shards.home_files_path_problem(args.home_files) or shards.home_files_overlap_problem(
                args.home_files, study
            )
            if problem:
                raise shards.ShardsError(f"home_files {problem}")
            tree = shards.read_home_files(args.home_files)
        if tmpfs.is_symlink() or not tmpfs.is_dir():
            raise shards.ShardsError("--tmpfs must be a directory, not a link")
        if (study / "out").exists() or os.path.lexists(study / shards.PLATFORM):
            raise shards.ShardsError("the study holds an out/ or a platform.toml of its own")
        refusal = shards.core_refusal(shards.this_core())
        if refusal:
            raise shards.ShardsError(refusal)
        work_root = shards.sandbox_work_root(args.root)
    except shards.ShardsError as error:
        print(f"fleetworker: ERROR: {error}; the worker is not started, with nothing made", file=sys.stderr)
        return REFUSED
    count = 0 if tree is None else len(tree.files)
    if count:
        print(f"fleetworker: home_files: true ({count} file(s) go to federation {args.federation}'s worker; a count: "
              "no name or content is shown)", file=sys.stderr)  # fmt: skip
    directory = Path(tempfile.mkdtemp(prefix=f"{shards.WORK_PREFIX}worker-{args.federation}.", dir=tmpfs))
    disarm = None
    try:
        directory.chmod(0o700)
        problem = _apart(work_root, Path(args.root), directory) if work_root is not None else None
        if problem:
            print(f"fleetworker: ERROR: {problem}; the worker is not started", file=sys.stderr)
            return REFUSED
        _owner(directory, args, count)
        for name in ("home", "tmp", "work", *(f"home/{sub}" for sub in shards.HOME_DIRS)):
            (directory / name).mkdir(mode=0o700)
        copy = directory / "study"
        shutil.copytree(study, copy, symlinks=True)
        platform = Path(args.root) / shards.PLATFORM
        if platform.is_file():  # the federation's, beside study.toml: a copy, never a link out of the study
            shutil.copyfile(platform, copy / shards.PLATFORM)
            (copy / shards.PLATFORM).chmod(0o600)
        env = {k: os.environ[k] for k in shards.ALLOWLIST if k in os.environ}
        env.update(shards.home_env(str(directory / "home")))
        env.update({"TMPDIR": str(directory / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"})
        disarm = shards._unwind_on_stop()  # a stop before the worker runs still ends through the cleanup below
        if tree is not None:
            try:
                shards.stage_home_files(tree, directory / "home")
            except shards.ShardsError as error:
                print(f"fleetworker: ERROR: {error}; the worker is not started", file=sys.stderr)
                return REFUSED
        words = [args.python, "-I", "-B", "-m", WORKER_MODULE, "--federation", args.federation,
                 "--channel", args.channel, "--study", str(copy), "--work", str(directory / "work"),
                 "--python", args.python]  # fmt: skip
        return runner(words, env, directory)
    finally:
        try:
            try:
                with shards._stop_held():
                    shards._clean_step(directory, False, True)
            except (
                SystemExit
            ):  # a stop that landed before the signals were held: the cleanup it cut short is made whole
                shards._clean_step(directory, False, True)
                raise
        finally:
            if disarm is not None:
                disarm()


# ------------------------------------------------------------------------------------------------ on the login node
def launch(args: argparse.Namespace, runner: Any = None, clock: Callable[[], float] = time.time) -> int:
    """`launch`: the member's worker started as a detached `srun` step of its held job, its client recorded; 0 when it
    was started or already runs, 2 (with nothing started) when the member is not one this fleet's up made and holds,
    its job is not the one up held and running, or the shard settings cannot be read."""
    for path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import fleetplan
    import fleetrecord
    import hpcslurm

    directory, shards = Path(args.fleet), fleetplan.shards
    runner = runner or hpcslurm.Runner()
    record = fleetrecord.Record(directory)
    try:
        with record.lifecycle("worker"):
            member = fleetrecord.member_of(record.read(), args.member)
            why, _ = fleetrecord.identity(directory, member)
            if why is None and member["status"] != "up":
                why = f"{args.member} is {member['status']}, not up"
            if why is not None:
                raise fleetrecord.FleetError(why)
            held = member.get("worker")
            if held and runner.client_alive(hpcslurm.Client(held["pid"], held["start"])):
                print(f"fleetworker: {args.member}: its worker already runs (client pid {held['pid']})")
                return 0
            settings = fleetplan.read_settings(Path(args.settings))
            root = fleetrecord.root_of(directory, args.member)
            fed = shards.federation(shards.Shard(1, 2, "shard-1", root))
            found = hpcslurm.Slurm(runner).job(fed["job"])
            if found is None or found.get("JobName") != fed["name"] or found.get("JobState") != "RUNNING":
                raise fleetrecord.FleetError(f"job {fed['job']} is not {args.member}'s running job: no worker started")
            argv = login_line(settings, fed, root, worker_name(fed), args.federation, args.channel,
                              args.job_end - clock())  # fmt: skip
            logs = directory / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            log = logs / f"{args.member}.worker.log"
            client = runner.spawn(argv, str(log))
            record.update(
                lambda r: fleetrecord.member_of(r, args.member).update(
                    worker={"pid": client.pid, "start": client.start, "log": str(log), "federation": args.federation}
                )
            )
    except (fleetrecord.FleetError, shards.ShardsError, hpcslurm.SlurmError, OSError, ValueError) as error:
        print(f"error: launch: {error}", file=sys.stderr)
        return REFUSED
    print(f"fleetworker: {args.member}: its worker is launched (client pid {client.pid}); its log {log}")
    return 0


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="fleetworker.py", description=__doc__.split("\n\n")[0])
    sub = found.add_subparsers(dest="command", required=True)
    one = sub.add_parser("node")
    for key in ("channel", "study", "tmpfs", "root"):
        one.add_argument(f"--{key}", required=True)
    one.add_argument("--federation", required=True)
    one.add_argument("--python", required=True)
    one.add_argument("--home-files", dest="home_files", default=None)
    login = sub.add_parser("launch")
    for key in ("fleet", "member", "settings", "federation", "channel"):
        login.add_argument(f"--{key}", required=True)
    login.add_argument("--job-end", dest="job_end", type=float, required=True)
    return found


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    return launch(args) if args.command == "launch" else node(args)


if __name__ == "__main__":
    sys.exit(main())
