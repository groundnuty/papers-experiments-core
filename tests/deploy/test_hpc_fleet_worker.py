"""#462 step 6: the elastic worker's launch inside a federation's held job (deploy/hpc-federation/fleetworker.py). The
login node starts it as one detached srun step of the held job; on the node it runs in a private home on the node's
tmpfs with the operator's key files, a sibling of the sandbox's work root, and removes that home on every way out;
`down`'s sweep removes what a killed one left. Everything here is invented: the tree, the file names and the keys;
nothing runs srun, and the worker is a stand-in."""

import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "fleetworker.py"
# Names and contents no record, log or line may ever hold: each carries "zq".
FILES = {".zqsecrets/alpha-zq/key-zq": "sk-zq-first-1111\n", ".zqkeys/beta-zq/lk-zq": "lk-zq-second-2222\n"}
MARKERS = (b"zq", b"1111", b"2222")


@pytest.fixture(scope="module")
def worker():
    spec = importlib.util.spec_from_file_location("hpc_fleetworker_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_fleetworker_test"] = module
    try:
        spec.loader.exec_module(module)
        shards = module._shards()
        shards_core = shards.core_refusal
        shards.core_refusal = lambda report: None  # the suite's core is not what this test asks about
        yield module
        shards.core_refusal = shards_core
    finally:
        sys.modules.pop("hpc_fleetworker_test", None)
        sys.modules.pop("hpc_shards", None)


def key_tree(tmp_path: Path) -> Path:
    root = tmp_path / "optree"
    root.mkdir(mode=0o700, parents=True)
    for name, text in FILES.items():
        path = root / name
        for parent in reversed(path.relative_to(root).parents):
            (root / parent).mkdir(mode=0o700, exist_ok=True)
            (root / parent).chmod(0o700)
        path.write_text(text)
        path.chmod(0o600)
    return root


def args_of(tmp_path: Path, tree: Path | None = None, work_root: str | None = None) -> SimpleNamespace:
    """What `node` is given: a study (no out/), the federation's root (with a platform.toml, and a sandbox when
    `work_root` is), the channel, and the node's tmpfs."""
    study, root = tmp_path / "study", tmp_path / "fed" / "root"
    study.mkdir(parents=True, exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    root.mkdir(parents=True, exist_ok=True)
    platform = '[space]\nname = "s"\n'
    if work_root is not None:
        platform += f'[sandbox]\nbackend = "apptainer"\nwork_root = "{work_root}"\n'
    (root / "platform.toml").write_text(platform)
    (tmp_path / "tmpfs").mkdir(exist_ok=True)
    (tmp_path / "channel").mkdir(exist_ok=True)
    return SimpleNamespace(federation="site-a-fed-1", channel=str(tmp_path / "channel"), study=str(study),
                           tmpfs=str(tmp_path / "tmpfs"), root=str(root),
                           home_files=None if tree is None else str(tree))  # fmt: skip


def watcher():
    """A stand-in worker: what it saw of its directory, its home and its argv."""
    seen = {}

    def run(argv, env, cwd):
        home = Path(env["HOME"])
        seen.update(argv=list(argv), env=dict(env), cwd=Path(cwd), home=home)
        seen["owner"] = json.loads((Path(cwd) / "step.json").read_text())
        seen["files"] = {str(p.relative_to(home)): (stat.S_IMODE(p.lstat().st_mode), p.read_text())
                         for p in sorted(home.rglob("*")) if p.is_file()}  # fmt: skip
        seen["dirs"] = {stat.S_IMODE(p.lstat().st_mode) for p in sorted(home.rglob("*")) if p.is_dir()}
        seen["platform"] = (Path(argv[argv.index("--study") + 1]) / "platform.toml").is_file()
        return 0

    return run, seen


def never_run(argv, env, cwd):
    raise AssertionError("a refused start must never run the worker")


def test_the_worker_runs_in_a_private_home_with_the_keys_and_nothing_of_them_is_recorded_or_printed(
    worker, tmp_path, capsys
):
    """C46: The worker's HOME is a directory of its own on the tmpfs holding the operator's tree (files 0600, dirs
    0700) and its XDG directories; it runs `-I -B -m expcore.campaign.elastic.worker` on a copy of the study that has
    the federation's platform.toml; its owner record says `home_files: true` and a count and nothing of the tree; what
    it prints is a count; and the whole directory is gone once it ends."""
    run, seen = watcher()
    assert worker.node(args_of(tmp_path, key_tree(tmp_path)), run) == 0
    assert {name: text for name, (_, text) in seen["files"].items()} == FILES
    assert {mode for mode, _ in seen["files"].values()} == {0o600}
    assert seen["dirs"] == {0o700}
    assert seen["argv"][1:5] == ["-I", "-B", "-m", "expcore.campaign.elastic.worker"]
    assert seen["env"]["HOME"] == str(seen["cwd"] / "home")
    assert seen["env"]["XDG_CONFIG_HOME"].startswith(str(seen["cwd"] / "home"))
    assert seen["platform"] is True
    assert (seen["owner"]["home_files"], seen["owner"]["home_files_count"]) == (True, len(FILES))
    assert not any(marker in json.dumps(seen["owner"]).encode() for marker in MARKERS)
    shown = capsys.readouterr().err
    assert f"{len(FILES)} file" in shown
    assert "zq" not in shown
    assert not list((tmp_path / "tmpfs").iterdir())


def test_the_home_is_a_sibling_of_the_sandboxs_work_root_and_never_under_it_or_the_federations_root(worker, tmp_path):
    """C46: Under the tmpfs, beside the sandbox's work root and apart from the federation's root, so no cell binds it; a
    layout whose work root would hold it (or the other way round) is refused with nothing made."""
    run, seen = watcher()
    args = args_of(tmp_path, key_tree(tmp_path), work_root=str(tmp_path / "tmpfs" / "sandbox-work"))
    assert worker.node(args, run) == 0
    here = seen["cwd"]
    assert here.parent == tmp_path / "tmpfs"
    assert not here.is_relative_to(tmp_path / "tmpfs" / "sandbox-work")
    assert not here.is_relative_to(Path(args.root))
    work_root = tmp_path / "tmpfs" / "sandbox-work"
    assert stat.S_IMODE(work_root.stat().st_mode) == 0o700  # made for the sandbox, which refuses to start without it
    refused = args_of(tmp_path, key_tree(tmp_path / "again"), work_root=str(tmp_path / "tmpfs"))
    assert worker.node(refused, never_run) == 2
    kept = [p.name for p in (tmp_path / "tmpfs").iterdir()]
    assert kept == ["sandbox-work"]  # the sandbox's work root stays (down's sweep removes it); no worker directory does


def test_a_term_removes_the_home_and_restores_the_handlers(worker, tmp_path):
    """C46: A TERM that arrives while the worker runs ends this step through its cleanup: nothing is left on the tmpfs
    and the handlers are the ones the process had."""
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def stopped(argv, env, cwd):
        assert (Path(env["HOME"]) / ".zqsecrets" / "alpha-zq" / "key-zq").is_file()
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the stop must end the step")

    with pytest.raises(SystemExit) as stop:
        worker.node(args_of(tmp_path, key_tree(tmp_path)), stopped)
    assert stop.value.code == 128 + signal.SIGTERM
    assert not list((tmp_path / "tmpfs").iterdir())
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_a_dead_workers_home_is_removed_by_the_sweep_down_runs(worker, tmp_path):
    """C46: A worker killed outright leaves its directory, keys included; `shards.py sweep` (down's, on the node) cleans
    it once its process is shown gone, and leaves one whose process still runs."""
    shards = worker._shards()
    args = args_of(tmp_path, key_tree(tmp_path))

    def copy_then_end(argv, env, cwd):  # the directory as a kill would leave it: copied aside, the step then ends
        shutil.copytree(cwd, tmp_path / "kept", symlinks=True)
        return 0

    assert worker.node(args, copy_then_end) == 0
    tmpfs = tmp_path / "tmpfs"
    assert not list(tmpfs.iterdir())  # the step cleaned its own
    record = json.loads((tmp_path / "kept" / "step.json").read_text())
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    owners = {
        "dead": {"pid": ended.pid, "start": "1"},
        "live": {"pid": os.getpid(), "start": shards._start_of(os.getpid())},
    }
    for name, owner in owners.items():
        left = tmpfs / f"{shards.WORK_PREFIX}worker-site-a-fed-1.{name}"
        shutil.copytree(tmp_path / "kept", left, symlinks=True)
        (left / "step.json").write_text(json.dumps({**record, **owner}))
    assert (tmpfs / f"{shards.WORK_PREFIX}worker-site-a-fed-1.dead" / "home" / ".zqsecrets").is_dir()
    assert shards.sweep(tmpfs, args.root) == 0
    assert [p.name.rsplit(".", 1)[1] for p in tmpfs.iterdir()] == ["live"]
