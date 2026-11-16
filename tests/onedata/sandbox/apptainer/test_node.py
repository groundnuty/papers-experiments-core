"""O56: the real node's own operations on this machine: apptainer's environment named whole, and a helper process
spawned, listed, signalled only as the process it is, and reaped. Nothing here runs apptainer."""

import signal
import sys
import time

import pytest

from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.hostops import run_bounded

SLEEPER = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"


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
