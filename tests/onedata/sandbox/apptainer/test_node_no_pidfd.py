"""O56: the real node on a kernel where `pidfd_open` answers ENOSYS (an EL8 4.18 kernel; `pidfd_send_signal` exists),
with `hidepid` on `/proc` and a setuid starter whose entry `/proc` hides.

The tests here make the kernel's answer by patching `pidfd_open`, and drive the real call sites with a real `LocalNode`:
the cell's kill at its cap, the watch's kill at the memory limit, the gate's run-start mark, and the teardown of helpers
and holders. A process the node spawned (the gate, a helper) is a child of the test, one it did not (a cell's process, a
holder) is not: each takes the branch it takes in production."""

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox.api import DataPathMode, SandboxError
from expcore_onedata.sandbox.apptainer import helpers, workdisk
from expcore_onedata.sandbox.apptainer import node as N
from expcore_onedata.sandbox.apptainer.cells import AppCellRunner, _RunProcesses
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.apptainer.watch import Watch

SLEEP = "import time\ntime.sleep(60)\n"
# a gate stand-in: ready, and the mark of the run's start in its log on SIGUSR1, as the real gate writes it
GATE = (
    "import json, signal, sys, time\n"
    "def mark(*_):\n"
    "    with open(sys.argv[1], 'a') as log:\n"
    "        log.write(json.dumps({'event': 'run_start', 'before': []}) + '\\n')\n"
    "signal.signal(signal.SIGUSR1, mark)\n"
    "print('gate ready', flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)
TAG = "EXPCORE_SANDBOX=run-1-abcd1234"


@pytest.fixture
def enosys(monkeypatch):
    """`pidfd_open` answers ENOSYS, as on a kernel before 5.3; the pidfd's other call must never be reached."""

    def no_pidfd(pid):
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    def forbidden(fd, sig):
        raise AssertionError("pidfd_send_signal was used where pidfd_open answers ENOSYS")

    monkeypatch.setattr(N, "pidfd_open", no_pidfd)
    monkeypatch.setattr(N, "pidfd_send_signal", forbidden)


@pytest.fixture
def sent(monkeypatch):
    """Every signal other than 0 that `os.kill` is asked to send, still sent."""
    calls, real = [], os.kill

    def kill(pid, sig):
        if sig != 0:
            calls.append((pid, sig))
        real(pid, sig)

    monkeypatch.setattr(N.os, "kill", kill)
    return calls


@pytest.fixture
def stand_in():
    """A process of this uid that the node did not spawn (a cell's process, a holder), reaped by a thread as the cell's
    init would reap it: the process, its start tick."""
    made = []

    def make(env=None):
        proc = subprocess.Popen([sys.executable, "-c", SLEEP], env=env)
        threading.Thread(target=proc.wait, daemon=True).start()
        made.append(proc)
        until = time.monotonic() + 10
        while (start := N._start_of(proc.pid)) is None and time.monotonic() < until:
            time.sleep(0.02)
        assert start is not None
        return proc, start

    yield make
    for proc in made:
        if N._start_of(proc.pid) is not None:
            proc.kill()
        proc.wait(10)


@pytest.fixture
def child(tmp_path):
    """A helper the node spawned, so a child of this process that the node holds unreaped."""
    node = N.LocalNode()
    process = node.spawn([sys.executable, "-c", SLEEP], stdout=tmp_path / "o", stderr=tmp_path / "e")
    yield node, process
    node.signal(process.pid, process.start, signal.SIGKILL)  # whichever way: through the pidfd or the fallback
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)


class Scoped(N.LocalNode):
    """The real node, listing only the stand-ins of the test: the run's PID namespace is not this machine's, and the
    watch must never be shown (and kill) the test's own processes. No cgroup is read."""

    def __init__(self):
        super().__init__()
        self.only: set[int] = set()

    def processes(self, tags=()):
        found = super().processes(tags)
        return None if found is None else [p for p in found if p.pid in self.only]

    def own_cgroup(self):
        return None


def _gone(proc, wait_s=10.0):
    """How the process ended (its returncode), the thread that reaps it having waited for it."""
    return proc.wait(wait_s)


# ------------------------------------------------------------------------------------------ the node's own children
def test_o56_without_pidfd_open_a_helper_the_node_spawned_is_signalled_while_it_is_its_unreaped_child(enosys, child):
    """O56: the gate and the helpers are the node's own children: signalled while `waitid` shows the pid still its
    unreaped child, which no other process can hold."""
    node, process = child
    assert node.signal(process.pid, process.start, signal.SIGTERM) is True
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)
    assert node.alive(process.pid, process.start) is False
    assert node.exit_status(process) == 128 + signal.SIGTERM


def test_o56_without_pidfd_open_a_helper_already_reaped_is_never_signalled(enosys, child, sent):
    """O56: once the node has waited for its child the pid is anyone's: nothing is sent, the helper is gone."""
    node, process = child
    assert node.signal(process.pid, process.start, signal.SIGKILL) is True
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)
    sent.clear()
    assert node.signal(process.pid, process.start, signal.SIGKILL) is False
    assert sent == []


def test_o56_without_pidfd_open_a_pid_a_reaped_helper_left_behind_is_never_signalled_in_another_child(
    enosys, child, sent, tmp_path
):
    """O56: the node keeps a helper's pid and start after it reaped it. Another child of this process that was given
    that pid since (here made to stand for it) is no helper: the old identity signals nothing, though `waitid` shows the
    pid a child of ours."""
    node, process = child
    assert node.signal(process.pid, process.start, signal.SIGKILL) is True
    until = time.monotonic() + 10
    while node.alive(process.pid, process.start) and time.monotonic() < until:
        time.sleep(0.02)
    other = subprocess.Popen([sys.executable, "-c", SLEEP])
    try:
        node._children[other.pid] = node._children[process.pid]  # the reaped helper's entry, now under the new pid
        sent.clear()
        assert node.signal(other.pid, process.start, signal.SIGKILL) is False
        assert sent == []
        assert other.poll() is None
    finally:
        other.kill()
        other.wait(10)


def test_o56_without_pidfd_open_a_helper_that_ended_and_is_not_reaped_yet_is_not_reported_signalled(
    enosys, tmp_path, sent
):
    """O56: a gate that ended just before the mark of the run's start is a zombie of ours: `waitid` still shows it, a
    signal to it can never be received, and the mark must not read as signalled."""
    node = N.LocalNode()
    process = node.spawn([sys.executable, "-c", "pass"], stdout=tmp_path / "o", stderr=tmp_path / "e")
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            break  # ended; not reaped
        time.sleep(0.02)
    sent.clear()
    assert node.signal(process.pid, process.start, signal.SIGUSR1) is False
    assert sent == []
    assert node.alive(process.pid, process.start) is False
    assert node.exit_status(process) == 0


def test_o56_without_pidfd_open_a_pid_the_node_spawned_under_another_start_is_never_signalled(enosys, child, sent):
    """O56: a pid the node holds as a child that started at another time is another process."""
    node, process = child
    assert node.signal(process.pid, str(int(process.start) + 1), signal.SIGKILL) is False
    assert sent == []
    assert node.alive(process.pid, process.start) is True


# ------------------------------------------------------------------------------------- a visible process of ours
def test_o56_without_pidfd_open_a_visible_process_of_ours_is_signalled_by_its_uids_and_its_start_tick(enosys, stand_in):
    """O56: a cell's process or a holder is no child of the node: its real and effective uid are ours and the start
    tick, read last, is the one named, and only then is it killed."""
    proc, start = stand_in()
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is True
    assert _gone(proc) == -signal.SIGKILL


def test_o56_without_pidfd_open_a_reused_pid_is_never_signalled(enosys, stand_in, sent):
    """O56: a pid that now shows another start tick (a process that took it over) is not the target."""
    proc, start = stand_in()
    assert N.LocalNode().signal(proc.pid, str(int(start) + 1), signal.SIGKILL) is False
    assert sent == []
    assert N._start_of(proc.pid) == start


def test_o56_without_pidfd_open_a_pid_taken_over_between_the_uid_check_and_the_tick_is_never_signalled(
    enosys, stand_in, sent, monkeypatch
):
    """O56: the tick is read after the uids and last before the kill: one that changed since is not the target."""
    proc, start = stand_in()
    monkeypatch.setattr(N, "_live_start", lambda pid: str(int(start) + 1))
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is False
    assert sent == []


@pytest.mark.parametrize("which", ["real", "effective", "both"])
def test_o56_without_pidfd_open_a_process_of_another_uid_is_never_signalled(enosys, stand_in, sent, monkeypatch, which):
    """O56: a process whose real or effective uid is not ours is refused before its tick is read."""
    proc, start = stand_in()
    mine, other = os.getuid(), os.getuid() + 1
    ids = {"real": (other, mine), "effective": (mine, other), "both": (other, other)}[which]
    monkeypatch.setattr(N, "_uids", lambda pid: ids)
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is False
    assert sent == []
    assert N._start_of(proc.pid) == start


def test_o56_without_pidfd_open_a_process_hidden_from_us_is_unknown_and_never_signalled(
    enosys, stand_in, sent, monkeypatch
):
    """O56: a process /proc does not show us (hidepid, a setuid starter) that is not our child cannot be shown to be
    the target: None, as it is with a pidfd, and nothing is sent. Every caller reads None as not sent."""
    proc, start = stand_in()
    monkeypatch.setattr(N, "_uids", lambda pid: None)
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is None
    assert sent == []
    assert N._start_of(proc.pid) == start


def test_o56_without_pidfd_open_a_process_that_is_gone_is_gone(enosys, stand_in, sent):
    """O56: no process holds the pid: False, nothing sent."""
    proc, start = stand_in()
    proc.kill()
    _gone(proc)
    sent.clear()
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is False
    assert sent == []


def test_o56_without_pidfd_open_a_pid_that_shows_another_start_after_the_kill_is_not_reported_as_sent(
    enosys, stand_in, monkeypatch
):
    """O56: the tick is read again after the kill: a pid taken over in the instant between is not the target's, so the
    signal is not reported as having reached it."""
    proc, start = stand_in()
    reads = []

    def live(pid):
        reads.append(pid)
        return start if len(reads) == 1 else str(int(start) + 1)

    monkeypatch.setattr(N, "_live_start", live)
    assert N.LocalNode().signal(proc.pid, start, signal.SIGKILL) is None
    assert len(reads) == 2


# -------------------------------------------------------------------------- only ENOSYS, and the pidfd's own path
def test_o56_where_pidfd_open_works_the_pidfd_is_used_and_the_fallback_is_never_entered(child, monkeypatch):
    """O56: with `pidfd_open` working the pidfd opens, holds the pid across the start check, and sends the signal;
    the fallback, `waitid` and the uid read are never reached."""
    node, process = child
    calls = []
    real_open, real_send = N.pidfd_open, N.pidfd_send_signal

    def opened(pid):
        calls.append("open")
        return real_open(pid)

    def sent_through(fd, sig):
        calls.append("send")
        real_send(fd, sig)

    def entered(*args, **kwargs):
        raise AssertionError("the fallback was entered where pidfd_open works")

    monkeypatch.setattr(N, "pidfd_open", opened)
    monkeypatch.setattr(N, "pidfd_send_signal", sent_through)
    monkeypatch.setattr(N.LocalNode, "_signal_without_pidfd", entered)
    monkeypatch.setattr(N.os, "waitid", entered)
    monkeypatch.setattr(N, "_uids", entered)
    assert node.signal(process.pid, process.start, signal.SIGTERM) is True
    assert calls == ["open", "send"]
    assert node.signal(process.pid, str(int(process.start) + 1), signal.SIGTERM) is False


@pytest.mark.parametrize("code", [errno.EPERM, errno.EMFILE, errno.EINVAL])
def test_o56_a_pidfd_open_that_fails_for_another_reason_is_no_fallback(child, monkeypatch, sent, code):
    """O56: only ENOSYS means the kernel has no pidfd; any other refusal is unknown, as it was, and nothing is sent."""
    node, process = child

    def refused(pid):
        raise OSError(code, os.strerror(code))

    def entered(*args, **kwargs):
        raise AssertionError("the fallback was entered for another failure than ENOSYS")

    monkeypatch.setattr(N, "pidfd_open", refused)
    monkeypatch.setattr(N.LocalNode, "_signal_without_pidfd", entered)
    assert node.signal(process.pid, process.start, signal.SIGKILL) is None
    assert sent == []


# ------------------------------------------------------------------------------------ the call sites, end to end
def test_o56_without_pidfd_open_a_cell_over_its_cap_is_killed(enosys, stand_in):
    """O56: the deadline lists the run's processes at the cap and kills them through `Node.signal`: with the old code
    every signal was None, the process outlived its grace, and the outcome was `survived_kill`."""
    proc, _ = stand_in(env={**os.environ, "EXPCORE_SANDBOX": "run-1-abcd1234"})
    node = Scoped()
    node.only = {proc.pid}
    watch = Watch(node, run_tag=TAG, own_pidns=None, memory_bytes=1 << 40, spaces=(), memory_poll_s=0.02,
                  disk_poll_s=0.0)  # fmt: skip
    runner = object.__new__(AppCellRunner)
    runner.node, runner.watch = node, watch
    record = DL.enforce(_RunProcesses(runner), DL.Escalation(0.5, lambda budget: {"ok": True}, poll_s=0.05))
    assert record["outcome"] == DL.KILLED
    assert _gone(proc) == -signal.SIGKILL


def test_o56_without_pidfd_open_a_cell_over_its_memory_limit_is_killed(enosys, stand_in):
    """O56: the watch's poll finds the run's memory past its limit and kills its processes through `Node.signal`:
    the kill is recorded as signalled, the process dies."""
    proc, _ = stand_in(env={**os.environ, "EXPCORE_SANDBOX": "run-1-abcd1234"})
    node = Scoped()
    node.only = {proc.pid}
    watch = Watch(node, run_tag=TAG, own_pidns=None, memory_bytes=1 << 20, spaces=(), memory_poll_s=0.02,
                  disk_poll_s=0.0)  # fmt: skip
    watch.begin_cell(1, "nonce")
    watch.poll()
    assert _gone(proc) == -signal.SIGKILL
    kill = watch.end_cell()["kills"][0]
    assert (kill["reason"], kill["signalled"], kill["processes"]) == ("memory_limit", 1, 1)


def test_o56_without_pidfd_open_the_gates_run_start_mark_is_signalled_and_confirmed(enosys, tmp_path):
    """O56, O24: the REST gate is signalled (SIGUSR1) and the mark read back from its log: with the old code the
    signal was None, nothing was marked, and the served bytes were unknown."""
    node = N.LocalNode()
    log = tmp_path / "requests.jsonl"
    gate = node.spawn([sys.executable, "-c", GATE, str(log)], stdout=tmp_path / "o", stderr=tmp_path / "e")
    try:
        until = time.monotonic() + 10
        while not (tmp_path / "o").read_text().startswith("gate ready") and time.monotonic() < until:
            time.sleep(0.02)
        box = SimpleNamespace(
            _phase=threading.RLock(), _cells=object(), _stopped=False, _meta={}, _agent_began=False,
            _steps_running=False, spec=SimpleNamespace(mode=DataPathMode.REST), node=node,
            _helpers={"gate": helpers.Helper("gate", gate, tmp_path / "o", tmp_path / "e")}, _sleep=time.sleep,
            gate_log_lines=lambda: log.read_text().splitlines() if log.exists() else [],
        )  # fmt: skip
        assert ApptainerSandbox.mark_run_start(box) == {"gate": True, "signalled": True, "confirmed": True}
        assert json.loads(log.read_text().splitlines()[0])["event"] == "run_start"
    finally:
        node.signal(gate.pid, gate.start, signal.SIGKILL)
        until = time.monotonic() + 10
        while node.alive(gate.pid, gate.start) and time.monotonic() < until:
            time.sleep(0.02)  # reaped by the node once it has ended


def test_o56_without_pidfd_open_the_teardown_shows_a_helper_gone(enosys, child, monkeypatch):
    """O56: the teardown's TERM reaches the helper the node spawned and it is shown gone: with the old code the TERM
    and the KILL were None and the stop raised "could not be shown gone"."""
    node, process = child
    monkeypatch.setattr(helpers, "KILL_WAIT_S", 1.0)
    helper = helpers.Helper("gate", process, None, None)
    note = helpers.stop(node, helper, term_wait_s=2.0)
    assert note["term"] is True
    assert note["ended_on_term"] is True


def test_o56_without_pidfd_open_the_teardown_shows_a_holder_of_the_work_disk_gone(enosys, stand_in, tmp_path):
    """O56: a process of the uid that carries the run's copy tag is killed and shown gone: with the old code the
    sweep failed at its deadline with "could not be shown gone"."""
    sid = "sid-00000000"
    proc, _ = stand_in(env={**os.environ, **workdisk.copy_env(sid)})
    node = Scoped()
    node.only = {proc.pid}
    image = tmp_path / "work" / workdisk.IMAGE_NAME
    assert workdisk.end_holders(node, image, sid) == 1
    assert _gone(proc) == -signal.SIGKILL


def test_o56_without_pidfd_open_a_holder_that_cannot_be_reached_fails_the_teardown(
    enosys, stand_in, tmp_path, monkeypatch
):
    """O56: a holder /proc hides from us, not our child, cannot be signalled and cannot be shown gone: the sweep fails
    at its deadline, never passing it over."""
    sid = "sid-00000000"
    proc, _ = stand_in(env={**os.environ, **workdisk.copy_env(sid)})
    node = Scoped()
    node.only = {proc.pid}
    monkeypatch.setattr(N, "_uids", lambda pid: None)
    monkeypatch.setattr(workdisk, "GONE_WAIT_S", 0.5)
    with pytest.raises(SandboxError, match="could not be shown gone"):
        workdisk.end_holders(node, tmp_path / "work" / workdisk.IMAGE_NAME, sid)
    assert N._start_of(proc.pid) is not None
