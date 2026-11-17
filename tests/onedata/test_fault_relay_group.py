"""O58: each verb the fault relay runs is a process group of its own, and at the verb's bound the whole group goes.

The relay runs a verb (`hpcfed.py stop|start|status|exec`) as a process that launches an `srun` step. At the bound the
old runner ended only that process: its `srun` child was orphaned and went on waiting on the Slurm controller, which
can stall for its message timeout, and created and launched the step long after the relay had answered `timed_out`.
These tests run real processes (a stand-in `hpcfed.py` that starts a child, then waits) through the relay's own runner.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_relay import run_clean

ENVIRONMENT = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/home/someone"}
BOUND_S = 2.0

# The stand-in verb: starts a child that outlives it, writes the child's pid, then waits (a sleep, never a busy loop).
VERB = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-I", "-c", sys.argv[2]])
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write(f"{child.pid}\\n")
    time.sleep(300)
    """
)
SLEEPER = "import time; time.sleep(300)"
DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"


def alive(pid: int) -> bool:
    """Whether a process runs: a zombie nobody has reaped yet is dead."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[0] not in ("Z", "X")


@pytest.fixture
def children():
    """The pids a test started, ended at its end whatever the test found."""
    found: list[int] = []
    yield found
    for pid in found:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def run_verb(tmp_path, children, child_code, **options):
    """Run the stand-in verb to its bound through `run_clean`; the child's pid (None if never written)."""
    script, pidfile = tmp_path / "verb.py", tmp_path / "child.pid"
    script.write_text(VERB, encoding="utf-8")
    with pytest.raises(subprocess.TimeoutExpired):
        run_clean(ENVIRONMENT, **options)(
            [sys.executable, "-I", str(script), str(pidfile), child_code], timeout_s=BOUND_S
        )
    assert pidfile.exists(), "the stand-in verb never started its child"
    pid = int(pidfile.read_text(encoding="utf-8"))
    children.append(pid)
    return pid


def gone_within(pid: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def test_o58_a_child_that_outlives_its_verb_does_not_survive_the_verbs_bound(tmp_path, children):
    """O58: the verb's process and the srun it launched are one group; at the bound both are ended, so no srun client
    survives to create the step after the relay has answered `timed_out`."""
    pid = run_verb(tmp_path, children, SLEEPER)
    assert gone_within(pid, 3.0), "the verb's child outlived the verb's bound (an orphaned srun would start its step)"


def test_o58_a_child_that_ignores_the_first_signal_is_killed_after_the_grace(tmp_path, children):
    """O58: TERM first, KILL once the grace has passed: a child that ignores TERM (an srun waiting on its step) does
    not outlive the bound either."""
    pid = run_verb(tmp_path, children, DEAF, grace_s=0.5)
    assert gone_within(pid, 3.0), "a child that ignored TERM survived the verb's bound"


def test_o58_a_verb_runs_in_a_session_and_group_of_its_own():
    """O58: the group the bound ends is the verb's alone: never the relay's own."""
    code = "import os; print(os.getpgid(0) == os.getpid(), os.getsid(0) == os.getpid())"
    done = run_clean(ENVIRONMENT)([sys.executable, "-I", "-c", code], timeout_s=30)
    assert done == CommandResult(0, "True True\n", "")


def test_o58_a_verb_that_ends_within_its_bound_is_answered_as_it_was():
    """O58: a verb that finishes in time keeps its answer: its status, its output and its error stream, read as text."""
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    done = run_clean(ENVIRONMENT)([sys.executable, "-I", "-c", code], timeout_s=30)
    assert done == CommandResult(3, "out\n", "err\n")


def test_o58_a_descendant_that_holds_the_verbs_pipes_never_holds_the_relay(tmp_path, children):
    """O58: a descendant that left the group (a session of its own) and still holds the verb's output pipes cannot make
    the relay wait for their end: the bound's answer comes within the bound and the grace, and the descendant is not
    the relay's to end (a start's detached service client is one)."""
    pidfile = tmp_path / "escaped.pid"
    code = (
        "import subprocess, sys, time\n"
        "sleeper = 'import time; time.sleep(300)'\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c', sleeper], start_new_session=True)\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(300)\n"
    )
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_clean(ENVIRONMENT, grace_s=0.5)([sys.executable, "-I", "-c", code], timeout_s=BOUND_S)
    assert time.monotonic() - began < BOUND_S + 15
    escaped = int(pidfile.read_text(encoding="utf-8"))
    children.append(escaped)
    assert alive(escaped)  # it left the group on purpose: ended by the fixture, never by the relay
