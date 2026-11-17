"""B1 (live slot 5): every Python the HPC driver and the shard driver launch runs with `-I -B`, isolated and writing no
bytecode. A node step's first import of a module not yet compiled wrote its `.pyc` into the core's interpreter, which
uv had installed in the account's home, shared by every job and agent of the account (`statistics.cpython-312.pyc`, 2 s
after a shard step started); the login node's `plan`, `collect` and merge did the same. `-I` ignores
PYTHONDONTWRITEBYTECODE, so only the flag stops it. The driver's own commands are checked by the fake Slurm itself
(`hpc_fed_support.flagged`, on every command any test gives it); here, the shard step's, the dispatcher's, the
sandbox's helpers on the node and the bundle's scripts. Nothing runs srun or reaches a node."""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from apptainer_support import make_settings, start
from fake_node import FakeNode
from hpc_fed_support import NO_BYTECODE, flagged, unflagged

from expcore_onedata.sandbox import copyout
from expcore_onedata.sandbox.apptainer import gaterun, hostmount
from expcore_onedata.sandbox.hostops import CmdResult

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
SCRIPT = HPC / "shards.py"


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_no_bytecode", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_no_bytecode"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_no_bytecode", None)


def test_b1_the_check_finds_a_python_without_both_flags():
    """O57 (B1): the check itself: a Python by any of its names, followed by anything but `-I -B`, is found."""
    assert unflagged(["/venv/bin/python", "-I", "x.py"]) == ["/venv/bin/python"]
    assert unflagged(["env", "-i", "A=b", "python3.11", "-B", "-I", "x.py"]) == ["python3.11"]
    assert unflagged(["srun", "/usr/bin/env", "-i", "python3", "-I", "-B", "-m", "expcore"]) == []
    with pytest.raises(AssertionError, match="without -I -B"):
        flagged(["python", "-c", "pass"])


def _plan(shards, tmp_path):
    two = (shards.Shard(1, 2, "fed-a", tmp_path / "a"), shards.Shard(2, 2, "fed-b", tmp_path / "b"))
    return shards.Plan(tmp_path / "study", "/shared/venv/bin/python", tmp_path / "src", tmp_path / "results",
                       Path("/tmp"), "/usr/bin:/bin", 2, "4g", two)  # fmt: skip


def test_b1_the_shard_step_line_and_the_merge_run_their_python_with_both_flags(shards, tmp_path):
    """O57 (B1): the step's line (its Python on the node) and the merge `collect` prints or runs on the login node."""
    plan = _plan(shards, tmp_path)
    fed = {"job": "4343", "name": "hpcop-p-1234-sandbox", "node": "node07", "het_group": None}
    line = shards.step_argv(plan, plan.shards[0], fed, SCRIPT)
    assert flagged(line)[line.index(plan.python) + 1 : line.index(plan.python) + 3] == NO_BYTECODE
    assert flagged(shards.merge_argv(plan, [tmp_path / "x" / "out"]))[1:3] == NO_BYTECODE


def test_b1_the_dispatcher_and_the_expcore_it_runs_take_both_flags(shards, tmp_path, monkeypatch):
    """O57 (B1): the dispatcher's own interpreter, and the `expcore run` it runs in its own process (so its imports are
    under `-B` too): its first words still take that argv for `python -I -B -m expcore` and run it in process."""
    seen = []

    class Child:
        def __init__(self, words, **_):
            seen.append(list(words))

        def wait(self):
            return 0

        def send_signal(self, signum):
            pass

    monkeypatch.setattr(shards.subprocess, "Popen", Child)
    monkeypatch.setattr(shards.signal, "signal", lambda *a: None)  # never this process's own handlers
    shards.run_dispatcher([sys.executable, "-I", "-B", "-m", "expcore", "run", "x"], {}, tmp_path)
    (words,) = seen
    assert words[1:4] == ["-I", "-B", "-c"]
    assert words[6:] == [sys.executable, "-I", "-B", "-m", "expcore", "run", "x"]
    assert flagged(words)
    assert '["-I", "-B", "-m", "expcore"]' in shards.DISPATCHER


def test_b1_the_dispatchers_expcore_runs_in_process_and_writes_no_bytecode(shards, tmp_path):
    """O57 (B1): run through the dispatcher's first words, `python -I -B -m expcore` runs in the dispatcher's own
    process, whose `-B` holds for every import (`sys.dont_write_bytecode`), and it still swaps the step's record as it
    ends."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".step.ended.json").write_text(json.dumps({"phase": "archiving"}))
    probe = "import sys; print(sys.dont_write_bytecode)"
    first_words = shards.DISPATCHER.replace("runpy.run_module", f"exec({probe!r}) or runpy.run_module")
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", first_words, str(os.getpid()),
         sys.executable, "-I", "-B", "-m", "expcore", "--version"],
        capture_output=True, text=True, cwd=work, check=False,
    )  # fmt: skip
    assert done.stdout.splitlines()[0] == "True", done.stderr
    assert json.loads((work / "step.json").read_text()) == {"phase": "archiving"}


def test_b1_the_steps_expcore_run_takes_both_flags(shards, tmp_path):
    """O57 (B1): the `expcore run` argv `step` hands its dispatcher."""
    study = tmp_path / "study"
    study.mkdir()
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir()
    args = argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, over=None, job=None, step_name=None)  # fmt: skip
    seen = {}

    def runner(argv, env, cwd):
        seen["argv"] = list(argv)
        return 0

    nothing = SimpleNamespace(start=lambda out: None, stop=lambda out: None)
    shards.step(args, runner=runner, core_found=lambda src: True, snapshots=nothing)
    assert seen["argv"][:5] == [sys.executable, "-I", "-B", "-m", "expcore"]


def test_b1_the_sandboxs_helpers_on_the_node_take_both_flags(tmp_path):
    """O57 (B1): the gate and the egress collector (the node's interpreter, or the core's), the mount's watcher and the
    copy helper: each a Python the dispatcher starts on the node."""
    node = FakeNode()
    box = start(node, make_settings(tmp_path))
    try:
        assert [flagged(argv)[1:3] for argv in node.spawned] == [NO_BYTECODE] * len(node.spawned)
        assert len(node.spawned) >= 2  # the gate and the collector
    finally:
        box.stop()
    assert flagged(gaterun.argv("python3.11", tmp_path / "gate.json"))[1:3] == NO_BYTECODE
    where = hostmount.Layout(tmp_path / "run")
    watcher = hostmount.watcher_argv("/venv/bin/python", where, "n" * 32, SimpleNamespace(pid=1, start="2"), 1.0)
    assert flagged(watcher)[1:3] == NO_BYTECODE
    seen = []

    class Host:
        def run(self, argv, timeout_s, *, sudo=False, input=None):
            seen.append(list(argv))
            return CmdResult(0, "[]")

    copyout._helper(Host(), "list", {"root": "/x"}, 5.0)
    assert flagged(seen[0])[1:3] == NO_BYTECODE


# A Python the bundle's scripts run on a node: the nodes' own (HPCOP_PY), never one in a container's rootfs.
NODE_PYTHON = re.compile(r'"\$HPCOP_PY"(?P<rest>[^\n]*)')


def test_b1_every_python_the_bundles_scripts_run_on_a_node_takes_both_flags():
    """O57 (B1): every `"$HPCOP_PY"` the bundle's scripts run takes `-I -B`; its `--version` (the probe's) and the list
    of tools node-init checks are no runs of a script. The Pythons of a service's rootfs (`python3 -I /root/...`, which
    patch.sh writes and check-defaults.sh pins byte for byte) are the image's, not the node's, and stay as they are."""
    found = 0
    for script in sorted((HPC / "bundle").glob("*.sh")) + sorted((HPC / "bundle").glob("*.sbatch")):
        for match in NODE_PYTHON.finditer(script.read_text(encoding="utf-8")):
            rest = match.group("rest")
            if rest.startswith((" --version", ")")):
                continue
            found += 1
            assert rest.startswith(" -I -B "), f"{script.name}: {match.group(0)}"
    assert found >= 10
