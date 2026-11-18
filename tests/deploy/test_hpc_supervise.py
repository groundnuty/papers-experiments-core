"""supervise.py on a Python built without `os.pidfd_open` and `signal.pidfd_send_signal` (O58): a standalone build
(uv's, made against an old C library) lacks them on a kernel that has both, so the bundle makes the system calls itself
(434 and 424, through ctypes), and its check, its signal and its run behave as with the calls built in.

And on a kernel with no `pidfd_open` at all (Linux before 5.3, which answers ENOSYS): the check passes and names the
start-tick fallback, `signal` sends by PID, start tick and uid, and `run` supervises its own unreaped child; any other
refusal of `pidfd_open` is no fallback, and on a kernel that has the call the fallback is never entered.

Every process here is a local child of the test that sleeps, and every one is ended before its test returns. A test
that must not send a signal replaces `os.kill` with a recorder for its body, so a fallback that fails sends nothing.
"""

import contextlib
import errno
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SUPERVISE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle" / "supervise.py"
SLEEPER = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
WITHOUT_PIDFD = (
    "import os, runpy, signal, sys\n"
    "for module, name in ((os, 'pidfd_open'), (signal, 'pidfd_send_signal')):\n"
    "    if hasattr(module, name):\n"
    "        delattr(module, name)\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


ENOSYS_PYTHON = (  # a kernel with no pidfd_open: the call answers ENOSYS, and no other pidfd call may follow
    "import errno, os, runpy, signal, sys\n"
    "def refuse(*_a):\n"
    "    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))\n"
    "def forbid(*_a, **_k):\n"
    "    raise AssertionError('a pidfd call after pidfd_open answered ENOSYS')\n"
    "os.pidfd_open = refuse\n"
    "signal.pidfd_send_signal = forbid\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)
PIDFD_ONLY = (  # a kernel that has pidfd_open: the fallback's entries fail loudly, and the pidfds opened are listed
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('hpcop_supervise', sys.argv[1])\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "def entered(*_a, **_k):\n"
    "    raise AssertionError('the start-tick fallback was entered on a kernel that has pidfd_open')\n"
    "m.run_by_tick = m.signal_by_tick = m.kill_child = entered\n"
    "opened, real = [], m.pidfd_open\n"
    "m.pidfd_open = lambda pid: (opened.append(pid), real(pid))[1]\n"
    "code = m.main(sys.argv[2:])\n"
    "sys.stderr.write('pidfds opened for %s\\n' % opened)\n"
    "sys.exit(code)\n"
)


def load():
    spec = importlib.util.spec_from_file_location("hpcop_supervise", SUPERVISE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refused(*_args):
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))


def forbidden(*_args, **_kwargs):
    raise AssertionError("a pidfd call was made after pidfd_open answered ENOSYS")


@pytest.fixture
def supervise(monkeypatch):
    """supervise.py, loaded where this Python has neither pidfd call."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    return load()


@pytest.fixture(params=["os.pidfd_open", "raw system call"])
def enosys(request, monkeypatch):
    """supervise.py on a kernel with no pidfd_open, whether this Python has `os.pidfd_open` or makes the call itself."""
    module = load()
    if request.param == "os.pidfd_open":
        monkeypatch.setattr(os, "pidfd_open", refused, raising=False)
    else:
        monkeypatch.delattr(os, "pidfd_open", raising=False)
        monkeypatch.setattr(
            module, "_syscall", lambda number, *args: refused() if number == module.SYS_PIDFD_OPEN else forbidden()
        )
    monkeypatch.setattr(signal, "pidfd_send_signal", forbidden, raising=False)
    monkeypatch.setattr(module, "pidfd_send_signal", forbidden)
    return module


def kernel_has_pidfd():
    try:
        os.close(load().pidfd_open(os.getpid()))
    except OSError as error:
        if error.errno == errno.ENOSYS:
            return False
        raise
    return True


needs_pidfd = pytest.mark.skipif(not kernel_has_pidfd(), reason="this kernel has no pidfd_open")


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def start_tick(pid):
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return stat[stat.rfind(")") + 2 :].split()[19]


@pytest.fixture
def sleeper(tmp_path):
    """A child that sleeps, once it has written its pid."""
    marker = tmp_path / "sleeper.pid"
    child = subprocess.Popen([sys.executable, "-c", SLEEPER, str(marker)])
    wait_for(marker.exists)
    yield child
    with contextlib.suppress(ProcessLookupError):
        child.kill()
    child.wait(timeout=10)


def test_o58_the_check_passes_through_the_raw_system_calls(supervise):
    """O58: no pidfd call in this Python, and the check still finds both working on this kernel."""
    assert not hasattr(os, "pidfd_open")
    assert not hasattr(signal, "pidfd_send_signal")
    assert supervise.check() == 0
    assert (supervise.SYS_PIDFD_OPEN, supervise.SYS_PIDFD_SEND_SIGNAL) == (434, 424)


def test_o58_the_recorded_process_is_signalled_through_the_raw_calls(supervise, sleeper):
    """O58: its pid and its start tick: the signal is sent, and the process ends of it."""
    assert supervise.signal_recorded(sleeper.pid, start_tick(sleeper.pid), signal.SIGTERM) == 0
    assert sleeper.wait(timeout=10) == -signal.SIGTERM


def test_o58_a_process_of_another_start_tick_is_never_signalled(supervise, sleeper):
    """O58: the pid held by another process than the one recorded: exit 1, nothing sent."""
    other = str(int(start_tick(sleeper.pid)) + 1)
    assert supervise.signal_recorded(sleeper.pid, other, signal.SIGTERM) == 1
    time.sleep(0.2)
    assert sleeper.poll() is None


def test_o58_a_gone_process_is_not_signalled_and_the_raw_call_raises_as_os_does(supervise, sleeper):
    """O58: ESRCH from the raw pidfd_open is ProcessLookupError, as from os.pidfd_open: exit 1."""
    pid, start = sleeper.pid, start_tick(sleeper.pid)
    sleeper.kill()
    sleeper.wait(timeout=10)
    with pytest.raises(ProcessLookupError):
        supervise.pidfd_open(pid)
    assert supervise.signal_recorded(pid, start, signal.SIGTERM) == 1


@pytest.mark.parametrize(("sent", "status"), [(signal.SIGTERM, 143), (signal.SIGUSR1, 137)])
def test_o58_run_forwards_a_stop_to_its_child_on_a_python_without_pidfd(sent, status, tmp_path):
    """O58: `supervise.py run` on such a Python starts its child, forwards SIGTERM to it (and turns SIGUSR1 into
    SIGKILL), and exits with the child's status as the shell reads it."""
    marker = tmp_path / "child.pid"
    argv = [sys.executable, "-I", "-c", WITHOUT_PIDFD, str(SUPERVISE), "run", "--", sys.executable, "-c", SLEEPER]
    run = subprocess.Popen([*argv, str(marker)], stderr=subprocess.PIPE)
    child = None
    try:
        wait_for(lambda: marker.exists() and marker.read_text())  # the handlers are set before the child starts
        child = int(marker.read_text())
        run.send_signal(sent)
        assert run.wait(timeout=10) == status
        assert not Path(f"/proc/{child}").exists()  # supervise.py reaped it
        assert b"sent to the container's starter" in run.stderr.read()
    finally:
        for pid in (run.pid, child):
            with contextlib.suppress(ProcessLookupError, TypeError):
                os.kill(pid, signal.SIGKILL)
        run.wait(timeout=10)
        run.stderr.close()


# ------------------------------------------------------------------------------ a kernel with no pidfd_open (ENOSYS)
def test_o58_the_check_passes_and_names_the_start_tick_fallback_where_pidfd_open_answers_enosys(enosys, capsys):
    """O58: the check died on `[Errno 38] Function not implemented` before every service start; it now reports the
    fallback it will use and exits 0, so the output a run keeps names it."""
    assert enosys.check() == 0
    assert "start-tick fallback" in capsys.readouterr().out


@pytest.mark.parametrize("code", [errno.EPERM, errno.EMFILE])
def test_o58_a_refusal_of_pidfd_open_other_than_enosys_is_no_fallback(monkeypatch, capsys, sleeper, code):
    """O58: only the call's absence falls back; a seccomp refusal or no descriptor left still fails the check and
    refuses every signal, as before."""
    supervise = load()

    def refuse(_pid):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "pidfd_open", refuse, raising=False)
    assert supervise.check() == 2
    seen = capsys.readouterr()
    assert "fallback" not in seen.out
    assert "Linux 5.3 or later is needed" in seen.err
    with pytest.MonkeyPatch.context() as local:
        sent = []
        local.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert supervise.main(["signal", str(sleeper.pid), start_tick(sleeper.pid), "TERM"]) == 2
    assert sent == []
    assert sleeper.poll() is None


def test_o58_the_recorded_process_is_signalled_by_its_start_tick_where_pidfd_open_answers_enosys(enosys, sleeper):
    """O58: `signal PID START TERM` on such a kernel sends the signal to the process that holds PID at START, which
    ends of it, where it exited 2 for want of a pidfd."""
    assert enosys.main(["signal", str(sleeper.pid), start_tick(sleeper.pid), "TERM"]) == 0
    assert sleeper.wait(timeout=10) == -signal.SIGTERM


@pytest.mark.parametrize("how", ["another start tick", "gone"])
def test_o58_without_pidfd_open_a_pid_that_is_not_the_recorded_process_is_never_signalled(enosys, sleeper, how):
    """O58: a PID taken over (a stand-in with another start tick) or gone: exit 1, and no signal is sent."""
    pid, start = sleeper.pid, start_tick(sleeper.pid)
    if how == "another start tick":
        start = str(int(start) + 1)
    with pytest.MonkeyPatch.context() as local:
        sent = []
        local.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        if how == "gone":
            local.setattr(enosys, "start_time", lambda _pid: None)  # no /proc entry, or none we may read
        assert enosys.main(["signal", str(pid), start, "TERM"]) == 1
    assert sent == []
    assert sleeper.poll() is None


def test_o58_without_pidfd_open_a_process_of_another_uid_is_never_signalled(enosys, sleeper):
    """O58: the start tick matches, the owner does not (the supervisor's uid is another one): exit 1, no signal."""
    start = start_tick(sleeper.pid)
    with pytest.MonkeyPatch.context() as local:
        sent = []
        local.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        local.setattr(os, "getuid", lambda: os.geteuid() + 1)
        assert enosys.main(["signal", str(sleeper.pid), start, "TERM"]) == 1
    assert sent == []
    assert sleeper.poll() is None


def test_o58_without_pidfd_open_the_tick_is_read_again_after_the_signal_and_a_change_is_said(enosys, sleeper, capsys):
    """O58: the start tick is read just before the signal and once more after it: a tick that changed across the
    signal is said, since the signal may have reached the process that took the PID."""
    start, reads = start_tick(sleeper.pid), []
    ticks = iter([start, "another"])

    def seen(pid):
        reads.append(pid)
        return next(ticks)

    with pytest.MonkeyPatch.context() as local:
        sent = []
        local.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        local.setattr(enosys, "start_time", seen)
        assert enosys.main(["signal", str(sleeper.pid), start, "TERM"]) == 0
    assert sent == [(sleeper.pid, signal.SIGTERM)]
    assert reads == [sleeper.pid, sleeper.pid]
    assert "may have been taken over" in capsys.readouterr().err


def test_o58_without_pidfd_open_a_process_that_is_not_our_unreaped_child_is_never_signalled(enosys, sleeper):
    """O58: `run` signals its child only while the kernel still holds the child unreaped (waitid), so a PID given to
    another process once the child was reaped is never signalled: here our parent, and the reaped child itself."""
    sleeper.kill()
    sleeper.wait(timeout=10)
    with pytest.MonkeyPatch.context() as local:
        sent = []
        local.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        assert enosys.kill_child(os.getppid(), signal.SIGTERM) is False
        assert enosys.kill_child(sleeper.pid, signal.SIGTERM) is False
    assert sent == []


@pytest.mark.parametrize(("sent", "status"), [(signal.SIGTERM, 143), (signal.SIGUSR1, 137)])
def test_o58_run_supervises_forwards_a_stop_and_reaps_its_child_where_pidfd_open_answers_enosys(sent, status, tmp_path):
    """O58: `supervise.py run` on such a kernel started nothing (its check died); it now starts its child, forwards
    SIGTERM to it (SIGUSR1 as SIGKILL), reaps it and exits with its status as the shell reads it, and says which
    fallback it runs under."""
    marker = tmp_path / "child.pid"
    argv = [sys.executable, "-I", "-c", ENOSYS_PYTHON, str(SUPERVISE), "run", "--", sys.executable, "-c", SLEEPER]
    run = subprocess.Popen([*argv, str(marker)], stderr=subprocess.PIPE)
    child = None
    try:
        wait_for(lambda: marker.exists() and marker.read_text())
        child = int(marker.read_text())
        run.send_signal(sent)
        assert run.wait(timeout=10) == status
        assert not Path(f"/proc/{child}").exists()  # supervise.py reaped it
        said = run.stderr.read()
        assert b"sent to the container's starter" in said
        assert b"start-tick fallback" in said
    finally:
        for pid in (run.pid, child):
            with contextlib.suppress(ProcessLookupError, TypeError):
                os.kill(pid, signal.SIGKILL)
        run.wait(timeout=10)
        run.stderr.close()


# ------------------------------------------------------------------------------ a kernel that has pidfd_open
@needs_pidfd
def test_o58_where_pidfd_open_works_the_pidfd_path_is_taken_and_the_fallback_never_entered(sleeper, tmp_path, capsys):
    """O58: on a kernel that has the call the check says `pidfd`, `signal` sends through the pidfd (never `os.kill`),
    and `run` holds its child through a pidfd, with the fallback's entry points failing loudly if reached."""
    supervise = load()
    assert supervise.check() == 0
    said = capsys.readouterr().out
    assert "mode: pidfd" in said
    assert "fallback" not in said
    with pytest.MonkeyPatch.context() as local:
        local.setattr(supervise, "signal_by_tick", forbidden)
        local.setattr(supervise, "kill_child", forbidden)
        sent = []
        local.setattr(os, "kill", lambda target, sig: sent.append((target, sig)))
        assert supervise.main(["signal", str(sleeper.pid), start_tick(sleeper.pid), "TERM"]) == 0
    assert sent == []
    assert sleeper.wait(timeout=10) == -signal.SIGTERM
    marker = tmp_path / "child.pid"
    argv = [sys.executable, "-I", "-c", PIDFD_ONLY, str(SUPERVISE), "run", "--", sys.executable, "-c", SLEEPER]
    run = subprocess.Popen([*argv, str(marker)], stderr=subprocess.PIPE)
    child = None
    try:
        wait_for(lambda: marker.exists() and marker.read_text())
        child = int(marker.read_text())
        run.send_signal(signal.SIGTERM)
        assert run.wait(timeout=10) == 143
        said = run.stderr.read().decode()
        assert f"{child}]" in said  # the check's own pid first, then the child's: held through a pidfd
        assert "fallback" not in said
    finally:
        for pid in (run.pid, child):
            with contextlib.suppress(ProcessLookupError, TypeError):
                os.kill(pid, signal.SIGKILL)
        run.wait(timeout=10)
        run.stderr.close()
