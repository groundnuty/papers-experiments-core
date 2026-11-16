"""supervise.py on a Python built without `os.pidfd_open` and `signal.pidfd_send_signal` (O58): a standalone build
(uv's, made against an old C library) lacks them on a kernel that has both, so the bundle makes the system calls itself
(434 and 424, through ctypes), and its check, its signal and its run behave as with the calls built in.

Every process here is a local child of the test that sleeps, and every one is ended before its test returns.
"""

import contextlib
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


@pytest.fixture
def supervise(monkeypatch):
    """supervise.py, loaded where this Python has neither pidfd call."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    spec = importlib.util.spec_from_file_location("hpcop_supervise", SUPERVISE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
