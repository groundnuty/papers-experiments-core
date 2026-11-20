"""O56: the real node's own operations on this machine: apptainer's environment named whole, and a helper process
spawned, listed, signalled only as the process it is, and reaped. Nothing here runs apptainer."""

import os
import signal
import sys
import time

import pytest

from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.hostops import run_bounded

SLEEPER = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
# a main thread on one core and a second thread on another: what a process's cores are, task by task
SPLIT = (
    "import os, threading, time\n"
    "cores = sorted(os.sched_getaffinity(0))\n"
    "os.sched_setaffinity(0, {cores[0]})\n"
    "done = threading.Event()\n"
    "def other():\n"
    "    os.sched_setaffinity(0, {cores[1]})\n"
    "    done.set()\n"
    "    time.sleep(30)\n"
    "threading.Thread(target=other, daemon=True).start()\n"
    "done.wait()\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


def test_run_bounded_without_inheriting_gives_exactly_the_environment_named():
    """O56: `inherit_env=False` passes the mapping alone: nothing of this process's own environment."""
    done = run_bounded(["/usr/bin/env"], 10, env={"ONLY": "1"}, inherit_env=False)
    assert done.stdout == "ONLY=1\n"


def test_apptainer_is_given_the_node_s_path_home_and_locale_alone(monkeypatch):
    """O56: no APPTAINER* variable of the driver's reaches the apptainer command."""
    monkeypatch.setenv("APPTAINER_BIND", "/somewhere")
    monkeypatch.setenv("APPTAINERENV_SECRET", "x")
    done = LocalNode().apptainer(["/usr/bin/env"], 10)
    assert sorted(line.split("=")[0] for line in done.stdout.splitlines()) == ["HOME", "LC_ALL", "PATH"]


@pytest.fixture
def child(tmp_path):
    node = LocalNode()
    argv = [sys.executable, "-c", SLEEPER, "expcore-test-tag"]
    process = node.spawn(argv, stdout=tmp_path / "o", stderr=tmp_path / "e")
    yield node, process
    node.signal(process.pid, process.start, signal.SIGKILL)
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)  # reaped by the node once it has ended: no process is left behind


def test_a_spawned_helper_is_alive_listed_with_its_tag_and_in_this_namespace(child):
    node, process = child
    assert node.alive(process.pid, process.start) is True
    found = {p.pid: p for p in node.processes(["expcore-test-tag"])}
    assert "expcore-test-tag" in found[process.pid].tags
    assert found[process.pid].pidns == node.own_pidns()


def test_o56_a_listed_process_has_its_anonymous_memory_and_its_cgroup(child):
    """O56: the hidden-memory check subtracts each process's RssAnon alone, and only of those in the driver's cgroup."""
    node, process = child
    found = {p.pid: p for p in node.processes()}
    assert found[process.pid].anon is not None
    assert found[process.pid].anon <= found[process.pid].rss
    own = node.own_cgroup()
    if own is not None:  # a cgroup v2 host: the helper was started in the driver's own cgroup
        assert found[process.pid].cgroup == own


def test_o56_a_visible_child_is_never_a_hidden_one(child):
    """O56: this process's helper is its child and /proc shows it: no hidden child; a gone process has none."""
    node, _process = child
    me = node.identity()
    assert node.hidden_children(me.pid, me.start) == []
    assert node.hidden_children(me.pid, str(int(me.start) + 1)) == []


def test_o56_the_cgroup_s_anonymous_memory_is_read_from_its_memory_stat_or_is_unknown(tmp_path, monkeypatch):
    from expcore_onedata.sandbox.apptainer import node as N

    (tmp_path / "job" / "step").mkdir(parents=True)
    (tmp_path / "job" / "step" / "memory.stat").write_text("anon 536870912\nfile 0\n")
    monkeypatch.setattr(N, "CGROUP_ROOT", tmp_path)
    assert LocalNode().cgroup_anon("/job/step") == 536870912
    assert LocalNode().cgroup_anon("/job/other") is None


def test_a_signal_reaches_only_the_process_that_started_when_it_was_named(child):
    """O56: a pid with another start time is another process: never signalled."""
    node, process = child
    assert node.signal(process.pid, str(int(process.start) + 1), signal.SIGTERM) is False
    assert node.alive(process.pid, process.start) is True
    assert node.signal(process.pid, process.start, signal.SIGTERM) is True
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)
    assert node.alive(process.pid, process.start) is False
    assert node.exit_status(process) == 128 + signal.SIGTERM


def test_spawn_refuses_stream_files_that_exist(tmp_path):
    (tmp_path / "o").write_text("")
    with pytest.raises(FileExistsError):
        LocalNode().spawn([sys.executable, "-c", "pass"], stdout=tmp_path / "o", stderr=tmp_path / "e")


def test_the_node_s_identity_and_a_free_port():
    node = LocalNode()
    assert node.identity().pid > 0
    assert 1024 <= node.free_port() <= 65535


def test_a_pid_an_ended_helper_held_is_not_taken_for_that_helper_once_another_process_holds_it(child, monkeypatch):
    """O56: the node's own record of an ended child answers only for that child's start: another start of the same
    pid (a process that took it over) is read from /proc."""
    from expcore_onedata.sandbox.apptainer import node as N

    node, process = child
    node.signal(process.pid, process.start, signal.SIGKILL)
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)
    other = str(int(process.start) + 7)
    monkeypatch.setattr(N, "_start_of", lambda pid: other if pid == process.pid else None)
    assert node.alive(process.pid, other) is True
    assert node.alive(process.pid, process.start) is False


def test_the_node_reads_its_own_mount_table_for_mounts_under_a_directory(tmp_path):
    """O56: what the reaper unmounts is read from this process's mount table; a directory with none has none."""
    assert LocalNode().mounts_under(tmp_path) == []


def test_o70_a_listed_process_has_its_task_count(child):
    """O70: the pids limit counts tasks, which the listing reads from `Threads:` of the status file."""
    node, process = child
    found = {p.pid: p for p in node.processes()}
    assert found[process.pid].threads >= 1


def test_o70_the_node_reads_its_own_affinity_sorted():
    """O70: the step's cores, from which a cell's pin is taken, are the driver's own CPU affinity."""
    assert LocalNode().own_affinity() == sorted(os.sched_getaffinity(0))


@pytest.mark.skipif(len(os.sched_getaffinity(0)) < 2, reason="needs two cores to put two tasks on different ones")
def test_o70_the_cores_of_a_process_are_those_of_every_one_of_its_tasks(tmp_path):
    """O70: `sched_setaffinity` is per thread, so a process whose main thread is pinned may have a task on another
    core: the node reads the status of every task and gives their union; a process that is gone, or is another now,
    has none."""
    node = LocalNode()
    cores = sorted(os.sched_getaffinity(0))
    process = node.spawn([sys.executable, "-c", SPLIT], stdout=tmp_path / "o", stderr=tmp_path / "e")
    try:
        until = time.monotonic() + 20
        while "ready" not in (tmp_path / "o").read_text() and time.monotonic() < until:
            time.sleep(0.02)
        assert node.task_affinity(process.pid, process.start) == frozenset(cores[:2])
        assert node.task_affinity(process.pid, str(int(process.start) + 1)) == frozenset()
    finally:
        node.signal(process.pid, process.start, signal.SIGKILL)
        until = time.monotonic() + 10
        while node.alive(process.pid, process.start) and time.monotonic() < until:
            time.sleep(0.02)
    assert node.task_affinity(process.pid, process.start) == frozenset()


def _stat_fails(monkeypatch, process, error):
    """`/proc/<pid>/stat` of `process` raises `error` when the node reads it; every other file reads as it is."""
    from expcore_onedata.sandbox.apptainer import node as N

    real = N._read

    def read(path):
        if path.parts[-2:] == (str(process.pid), "stat"):
            raise error
        return real(path)

    monkeypatch.setattr(N, "_read", read)


@pytest.mark.parametrize("error", [PermissionError(13, "denied"), OSError(5, "I/O error")])
def test_o70_a_live_process_whose_stat_cannot_be_read_is_unreadable_not_gone(child, monkeypatch, error):
    """O70: `/proc/<pid>/stat` that exists and cannot be read is a process that is there, not one that is gone: its
    cores and its children are unreadable (None), where a gone one has none ([]/empty). An unreadable live process
    read as gone recorded no cores and no `affinity_unreadable`."""
    node, process = child
    _stat_fails(monkeypatch, process, error)
    assert node.task_affinity(process.pid, process.start) is None
    assert node.hidden_children(process.pid, process.start) is None


@pytest.mark.parametrize("error", [FileNotFoundError(2, "gone"), ProcessLookupError(3, "no such process")])
def test_o70_a_process_whose_stat_is_gone_has_no_cores_and_no_children(child, monkeypatch, error):
    """O70: a process that ended between the listing and the read (no entry, or the kernel's ESRCH) has none."""
    node, process = child
    _stat_fails(monkeypatch, process, error)
    assert node.task_affinity(process.pid, process.start) == frozenset()
    assert node.hidden_children(process.pid, process.start) == []
