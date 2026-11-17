"""W1, W2 and the running mark (live slot 5): what a shard step killed outright leaves on its node, and on the shared
results directory.

- W2: the killed run's sandbox leftovers (its directory and `_runs` record under the work root, and a dead
  `fuse.oneclient` mount, "Transport endpoint is not connected") go by the Apptainer run reaper: in down's sweep of
  the node, and in the next shard step on that node before it uses its work root, each mount unmounted and shown gone
  by the mount table; a dead run whose leftovers cannot all be removed fails the sweep, and refuses the step.
- W1: the work root itself is removed by the sweep once nothing is left in it, never while a step of ours may use it.
- The running mark (`<results>/<label>/running.json`): written as a step starts, removed at its own end, refusing a
  second step of the shard on any node unless it is shown gone.

The node is the fake one (`fake_node.FakeNode`: its processes, its mount table and `fusermount3`); the dispatcher a
stand-in; every path and name is invented, and nothing runs srun."""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox.apptainer import runs
from expcore_onedata.sandbox.apptainer.node import Spawned

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
SPEC = {"index": 1, "count": 2, "label": "fed-a"}
STEP = "hpcop-p-1234-sandbox-shard-1"  # the step's name, as `lines` gives it


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_reap", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_reap"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_reap", None)


def killed_run(tmp_path, *, mount=True):
    """A node whose sandbox run was killed outright with its step: its driver gone, its record and directory left
    under the work root (`<tmp_path>/root`, 0700), and with `mount` its oneclient's dead FUSE mount in it."""
    node, settings = FakeNode(), make_settings(tmp_path)
    box = start(node, settings, make_spec(lease_key="slot-a"))
    node.procs[node.owner.pid].alive = False  # the driver is gone, its helpers with it (the step's cgroup)
    for helper in [p for p in node.procs.values() if p.pid != node.owner.pid]:
        helper.alive = False
    mountpoint = str(box.dir / "mount")
    if mount:
        node.mounts.add(mountpoint)
    node.owner = Spawned(1001, "5151")  # the next process on the node
    node.procs[1001] = type(node.procs[999])(1001, "5151", "pid:[4026531836]", "driver", role="driver")
    return node, settings.work_root, box, mountpoint


def federation(tmp_path, work_root) -> Path:
    root = tmp_path / "fed" / "root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "platform.toml").write_text(f'[sandbox]\nbackend = "apptainer"\nwork_root = "{work_root}"\n')
    return root


# -- W2 and W1: down's sweep -----------------------------------------------------------------------------------------


def test_w2_the_sweep_reaps_a_killed_runs_dead_mount_and_directory_and_w1_removes_the_work_root(
    shards, tmp_path, capsys
):
    """O63 (W2, W1): down's sweep of the node: the killed run's dead FUSE mount is unmounted (`fusermount3 -u`) and
    shown gone by the mount table, then its directory and record go, and the work root, empty, goes too."""
    node, work_root, box, mountpoint = killed_run(tmp_path)
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert shards.sweep(tmpfs, str(federation(tmp_path, work_root)), node) == 0
    assert node.unmounts == [["fusermount3", "-u", mountpoint]]
    assert mountpoint not in node.mounts
    assert not box.dir.exists()
    assert not runs.record_path(work_root, box.sid).exists()
    assert not work_root.exists()
    assert "killed sandbox run(s) reaped: 1; the sandbox's work root: removed" in capsys.readouterr().out


def test_w2_a_dead_mount_only_a_lazy_unmount_takes_is_taken_so(shards, tmp_path):
    """O63 (W2): a mount a plain unmount cannot take goes with `fusermount3 -uz`, then shown gone."""
    node, work_root, box, mountpoint = killed_run(tmp_path)
    node.busy_mounts.add(mountpoint)
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert shards.sweep(tmpfs, str(federation(tmp_path, work_root)), node) == 0
    assert node.unmounts == [["fusermount3", "-u", mountpoint], ["fusermount3", "-uz", mountpoint]]
    assert not box.dir.exists()


def test_w2_a_killed_run_the_reaper_cannot_show_unmounted_fails_the_sweep_and_keeps_all(shards, tmp_path, capsys):
    """O63 (W2): the mount table cannot be read, so the dead run is never removed from under a mount: the sweep fails
    (down then keeps the root for a second pass), and the run's directory, its record and the work root stay."""
    node, work_root, box, _ = killed_run(tmp_path)
    node.mount_table_unreadable = True
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert shards.sweep(tmpfs, str(federation(tmp_path, work_root)), node) == 1
    assert "could not be reaped" in capsys.readouterr().out
    assert box.dir.exists()
    assert runs.record_path(work_root, box.sid).exists()


def test_w2_a_sweep_by_a_python_without_the_core_fails_closed(shards, tmp_path, capsys, monkeypatch):
    """O63 (W2): the nodes' own Python cannot import the core's reaper: a work root that is there is never taken for
    clean, the sweep fails, naming HPC_CORE_PYTHON."""
    _, work_root, box, _ = killed_run(tmp_path)

    def no_core(*_):
        raise ModuleNotFoundError("No module named 'expcore_onedata'")

    monkeypatch.setattr(shards, "reap_work_root", no_core)
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert shards.sweep(tmpfs, str(federation(tmp_path, work_root)), None) == 1
    assert "HPC_CORE_PYTHON" in capsys.readouterr().out
    assert box.dir.exists()


def test_w1_the_sweep_keeps_the_work_root_of_a_live_run(shards, tmp_path, capsys):
    """O63 (W1): a run whose driver lives is never reaped, and the work root that holds it stays."""
    node, settings = FakeNode(), make_settings(tmp_path)
    box = start(node, settings, make_spec())
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    try:
        assert shards.sweep(tmpfs, str(federation(tmp_path, settings.work_root)), node) == 0
        assert box.dir.is_dir()
        assert "the sandbox's work root: kept" in capsys.readouterr().out
    finally:
        box.stop()


def test_w1_the_sweep_keeps_an_empty_work_root_while_a_step_directory_is_on_the_node(shards, tmp_path, capsys):
    """O63 (W1): a step of ours on the node (its directory, here one whose out/ was never archived) may yet use the work
    root, so an empty one stays."""
    work_root = tmp_path / "root"
    (work_root / "_runs").mkdir(parents=True, mode=0o700)
    work_root.chmod(0o700)
    tmpfs = tmp_path / "tmpfs"
    kept = tmpfs / "expcore-shard-fed-a.kept"
    kept.mkdir(parents=True)
    (kept / "step.json").write_text(json.dumps({"shard": SPEC, "phase": "archiving", "root": "/elsewhere"}))
    assert shards.sweep(tmpfs, str(federation(tmp_path, work_root)), FakeNode()) == 0
    assert (work_root / "_runs").is_dir()
    assert "the sandbox's work root: kept" in capsys.readouterr().out


def test_w1_a_work_root_that_is_a_link_is_never_reaped_or_removed(shards, tmp_path, capsys):
    """O63 (W1, W2): a work root that is a link is followed by nothing: the sweep fails, and what it points at stays."""
    target = tmp_path / "elsewhere"
    (target / "_runs").mkdir(parents=True)
    link = tmp_path / "root"
    link.symlink_to(target)
    tmpfs = tmp_path / "tmpfs"
    tmpfs.mkdir()
    assert shards.sweep(tmpfs, str(federation(tmp_path, link)), FakeNode()) == 1
    assert (target / "_runs").is_dir()
    assert link.is_symlink()


# -- W2: the next step on the node -----------------------------------------------------------------------------------


def step_args(tmp_path, *, resume=False, over=None):
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    (tmp_path / "results").mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path,
                              results=tmp_path / "results", core_src=tmp_path, resume=resume, archive_left=False,
                              snapshot_every=3600.0, job="4343", step_name=STEP, over=over)  # fmt: skip


class Quiet:
    def start(self, out):
        pass

    def stop(self, out):
        pass


def test_w2_the_next_step_on_the_node_reaps_a_killed_runs_dead_mount_before_its_dispatcher(
    shards, tmp_path, monkeypatch
):
    """O63 (W2): the next shard step on the node reaps the earlier step's killed run (its dead mount first, shown gone)
    before its dispatcher, which finds the work root holding nothing of it."""
    node, work_root, box, mountpoint = killed_run(tmp_path)
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    seen = {}

    def runner(argv, env, cwd):
        seen["mounts"], seen["left"] = set(node.mounts), sorted(p.name for p in work_root.iterdir())
        return 0

    assert shards.step(step_args(tmp_path), runner=runner, core_found=lambda p: True, snapshots=Quiet(), node=node) == 0
    assert mountpoint not in seen["mounts"]
    assert seen["left"] == ["_runs"]
    assert not box.dir.exists()


def test_w2_a_step_refuses_to_use_a_work_root_whose_killed_run_cannot_be_reaped(shards, tmp_path, monkeypatch):
    """O63 (W2): a dead run the reaper cannot show unmounted: the step refuses before its dispatcher, and leaves the run
    as it was and no running mark of its own."""
    node, work_root, box, _ = killed_run(tmp_path)
    node.mount_table_unreadable = True
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(federation(tmp_path, work_root)))
    ran = []
    with pytest.raises(shards.ShardsError, match="could not all be removed"):
        shards.step(step_args(tmp_path), runner=lambda *a: ran.append(a) or 0, core_found=lambda p: True,
                    snapshots=Quiet(), node=node)  # fmt: skip
    assert ran == []
    assert box.dir.exists()
    assert not (tmp_path / "results" / "fed-a" / "running.json").exists()


# -- the running mark ------------------------------------------------------------------------------------------------


def mark_of(tmp_path):
    return tmp_path / "results" / "fed-a" / "running.json"


def test_a_step_holds_the_running_mark_while_it_runs_and_its_own_end_removes_it(shards, tmp_path):
    """O63: The mark names this step (its node, process, job, step name and a nonce) while its dispatcher runs, and is
    gone once the step has ended by itself."""
    seen = {}

    def runner(argv, env, cwd):
        seen["mark"] = json.loads(mark_of(tmp_path).read_text())
        return 0

    assert shards.step(step_args(tmp_path), runner=runner, core_found=lambda p: True, snapshots=Quiet()) == 0
    mark = seen["mark"]
    assert (mark["shard"], mark["node"], mark["pid"]) == (SPEC, os.uname().nodename, os.getpid())
    assert (mark["job"], mark["step"]) == ("4343", "hpcop-p-1234-sandbox-shard-1")
    assert len(mark["nonce"]) == 32
    assert not mark_of(tmp_path).exists()


def planted(tmp_path, *, node="another-node", pid=1, start="1", nonce="a" * 32):
    """The mark a step killed outright left: on another node, by default."""
    path = mark_of(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mark = {"format": "expcore-hpc-shard-running/1", "shard": SPEC, "nonce": nonce, "node": node, "pid": pid,
            "start": start, "job": "4343", "step": "hpcop-p-1234-sandbox-shard-1"}  # fmt: skip
    path.write_text(json.dumps(mark))
    return mark


@pytest.mark.parametrize("resume", [False, True], ids=["a start", "a resume with no proof"])
def test_a_step_of_a_shard_whose_mark_names_a_step_on_another_node_is_refused(shards, tmp_path, resume):
    """O63: A start, and a resume that resume-line did not show the marked step gone for (`--over`), are refused while
    the mark names a step on another node: that step may still run there, where this node cannot see it. The mark
    stays."""
    mark = planted(tmp_path)
    ran = []
    with pytest.raises(shards.ShardsError, match="running mark"):
        shards.step(step_args(tmp_path, resume=resume), runner=lambda *a: ran.append(a) or 0,
                    core_found=lambda p: True, snapshots=Quiet())  # fmt: skip
    assert ran == []
    assert json.loads(mark_of(tmp_path).read_text()) == mark


def test_a_resume_given_the_marks_nonce_takes_it_over_and_one_given_another_is_refused(shards, tmp_path):
    """O63: A resume whose `--over` is the mark's nonce (resume-line showed its step gone) takes the mark over; one
    whose `--over` names another mark (a second launch of the same line, once the first took it) is refused."""
    planted(tmp_path)
    seen = {}

    def runner(argv, env, cwd):
        seen["nonce"] = json.loads(mark_of(tmp_path).read_text())["nonce"]
        return 0

    args = step_args(tmp_path, resume=True, over="a" * 32)
    assert shards.step(args, runner=runner, core_found=lambda p: True, snapshots=Quiet()) == 0
    assert seen["nonce"] != "a" * 32
    planted(tmp_path, nonce="b" * 32)
    with pytest.raises(shards.ShardsError, match="running mark"):
        shards.step(args, runner=runner, core_found=lambda p: True, snapshots=Quiet())
    assert json.loads(mark_of(tmp_path).read_text())["nonce"] == "b" * 32


def test_a_resume_on_the_same_node_takes_over_the_mark_of_a_step_shown_gone_there(shards, tmp_path):
    """O63: A resume on the node the mark names, whose marked process is gone (this pid with another start time), takes
    it over with no proof from the login node; one whose marked process still runs is refused."""
    planted(tmp_path, node=os.uname().nodename, pid=os.getpid(), start="0")
    assert shards.step(step_args(tmp_path, resume=True), runner=lambda *a: 0, core_found=lambda p: True,
                       snapshots=Quiet()) == 0  # fmt: skip
    planted(tmp_path, node=os.uname().nodename, pid=os.getpid(), start=shards._start_of(os.getpid()))
    with pytest.raises(shards.ShardsError, match="running mark"):
        shards.step(step_args(tmp_path, resume=True), runner=lambda *a: 0, core_found=lambda p: True,
                    snapshots=Quiet())  # fmt: skip


def test_of_two_steps_taking_one_mark_over_only_the_first_runs(shards, tmp_path, monkeypatch):
    """O63: Two launches of one resume line: the second finds the mark the first made, not the one both were shown, and
    is refused; the first's mark stays."""
    planted(tmp_path)
    args = step_args(tmp_path, resume=True, over="a" * 32)
    first = shards.claim_running(args, SPEC)
    with pytest.raises(shards.ShardsError, match="running mark"):
        shards.claim_running(args, SPEC)
    assert json.loads(mark_of(tmp_path).read_text())["nonce"] == first["nonce"]
