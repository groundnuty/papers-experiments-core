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

from expcore_onedata import fault_relay_group
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


LEAVER = textwrap.dedent(
    """
    import subprocess, sys
    nul = subprocess.DEVNULL  # as an srun's, the child's pipes are not the verb's
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", sys.argv[2]],
        stdin=nul, stdout=nul, stderr=nul, start_new_session=sys.argv[3] == "detached",
    )
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write(f"{child.pid}\\n")
    sys.exit(int(sys.argv[4]))
    """
)


def run_leaver(tmp_path, children, how="grouped", status=1, **options):
    """A stand-in verb that starts a child and exits at once with `status`, as an hpcfed that fails with its srun
    waiting does; what `run_clean` answers, the child's pid, and the calls `on_stragglers` got."""
    script, pidfile, calls = tmp_path / "leaver.py", tmp_path / "child.pid", []
    script.write_text(LEAVER, encoding="utf-8")
    runner = run_clean(ENVIRONMENT, on_stragglers=lambda: calls.append(True), **options)
    done = runner([sys.executable, "-I", str(script), str(pidfile), SLEEPER, how, str(status)], timeout_s=30)
    pid = int(pidfile.read_text(encoding="utf-8"))
    children.append(pid)
    return done, pid, calls


def test_o58_a_verb_that_exits_with_its_child_still_running_leaves_nothing_behind_and_is_answered_as_it_ended(
    tmp_path, children
):
    """O58: hpcfed can fail (a squeue error while it ends its own step) with its srun still waiting on the controller:
    the verb exits, the relay answers its status as ever, and what it left in its group is ended, said to the relay
    (`on_stragglers`) so that it opens the watch for the steps the srun may still make."""
    done, pid, calls = run_leaver(tmp_path, children, status=1)
    assert done == CommandResult(1, "", "")
    assert gone_within(pid, 3.0), "a child the verb left in its group outlived the verb (an orphaned srun)"
    assert calls == [True]


def test_o58_a_verb_that_leaves_nothing_in_its_group_says_nothing_to_the_relay(tmp_path):
    """O58: the ordinary end: no child, no call."""
    calls = []
    code = "print('ok')"
    done = run_clean(ENVIRONMENT, on_stragglers=lambda: calls.append(True))(
        [sys.executable, "-I", "-c", code], timeout_s=30
    )
    assert done == CommandResult(0, "ok\n", "")
    assert calls == []


def test_o58_a_child_that_left_the_group_on_purpose_is_not_a_straggler(tmp_path, children):
    """O58: a start's service client is launched in a session of its own and recorded in the state file; it is the
    start, never ended by the relay, and no watch is owed for it."""
    done, pid, calls = run_leaver(tmp_path, children, how="detached", status=0)
    assert done == CommandResult(0, "", "")
    assert alive(pid)
    assert calls == []


def leader_states_at_each_signal(monkeypatch) -> list[str]:
    """Patch the group's signal so that, at each one, the leader's state is noted: `zombie` (ended, unreaped: it holds
    the group id), `running`, or `reaped` (its pid and so the group id are free to be given to another process)."""
    seen: list[str] = []
    real = fault_relay_group._signal_group

    def watching(group: int, signum: signal.Signals) -> None:
        try:
            info = os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            seen.append("running" if info is None else "zombie")
        except ChildProcessError:
            seen.append("reaped")
        real(group, signum)

    monkeypatch.setattr(fault_relay_group, "_signal_group", watching)
    return seen


def test_o58_a_verb_that_returned_has_its_group_signalled_while_its_leader_is_still_unreaped(
    tmp_path, children, monkeypatch
):
    """O58 (Codex, 17 Nov 2026): `communicate()` reaped the leader before the stragglers were signalled, so between the
    look at the group's members and `killpg` the group id could have been given to another process (a pid wrap), which
    the signal would then have reached. The leader is reaped only after the last signal, as the bound's path does."""
    seen = leader_states_at_each_signal(monkeypatch)
    done, pid, calls = run_leaver(tmp_path, children, status=1)
    assert done == CommandResult(1, "", "")
    assert calls == [True]
    assert seen, "the straggler was never signalled"
    assert set(seen) == {"zombie"}, seen
    assert gone_within(pid, 3.0)


def test_o58_a_verb_ended_at_its_bound_has_its_group_signalled_while_its_leader_is_unreaped(
    tmp_path, children, monkeypatch
):
    """O58: the bound's path, which already ended the group before the leader was reaped, is unchanged."""
    seen = leader_states_at_each_signal(monkeypatch)
    run_verb(tmp_path, children, SLEEPER)
    assert seen
    assert "reaped" not in seen


def test_o58_the_output_of_a_verb_that_returned_is_read_whole_as_text_with_its_newlines_translated(tmp_path):
    """O58: reading the pipes without `communicate` changes no answer: output and errors in the locale's text, CRLF and
    CR read as the newlines they stand for, and a byte that is no character replaced, not raised."""
    code = (
        "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n' + b'\\xff' + b'x' * 200000); "
        "sys.stderr.buffer.write(b'e\\r\\n'); sys.exit(2)"
    )
    done = run_clean(ENVIRONMENT)([sys.executable, "-I", "-c", code], timeout_s=30)
    assert done.returncode == 2
    assert done.stdout.startswith("a\nb\nc\n")
    assert done.stdout.endswith("x" * 200000)
    assert len(done.stdout) == 200007
    assert done.stderr == "e\n"
