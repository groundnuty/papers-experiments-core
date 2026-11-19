"""O72: the root half of a signal to the agent's processes (`killhelper.py`) signals a process only while it is the one
the listing saw: a pidfd where the kernel has one, else a start tick and command line read at once before `os.kill`.

The harness lists the agent's processes and then signals by pid, so a pid that ends and is given to another process in
between could be the data gate or the dispatcher on the agent's node. These tests run the helper's own code against real
processes of this user, and record every `os.kill` and pidfd send, so a signal that should not go is seen not to."""

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from expcore_onedata.sandbox import killhelper, procsignal
from expcore_onedata.sandbox.hostops import DockerHost

REAL_KILL = os.kill  # the fixture's own cleanup, whatever a test did to `os.kill`
HOST = DockerHost(sudo_command=())  # reads /proc as this user, as the harness does


def start_of(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat, the process's start tick."""
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return text[text.rfind(")") + 2 :].split()[19]


def exec_done(pid: int, within_s: float = 10.0) -> None:
    """Wait until the child has exec'd its own command line. Between fork and exec /proc/<pid>/cmdline is still the
    parent's, so a listing taken then records the parent's digest and the kill later finds the process `changed`
    (seen once on a loaded runner). The start tick does not change at exec; the command line does."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            if b"a-listed-process" in Path(f"/proc/{pid}/cmdline").read_bytes():
                return
        except OSError:
            pass
        time.sleep(0.01)
    raise AssertionError("the child never exec'd its command line")


@pytest.fixture
def sleeper():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "a-listed-process"])
    exec_done(process.pid)
    yield process
    if process.poll() is None:
        REAL_KILL(process.pid, signal.SIGKILL)
    process.wait()


def identity_of(process: subprocess.Popen) -> tuple[int, str, str]:
    """What the listing records of a process: its pid, start tick and the digest of its command line."""
    return process.pid, start_of(process.pid), procsignal.cmd_digest(HOST, process.pid)


def recording(monkeypatch) -> list[tuple]:
    """Every `os.kill`, pidfd send and identity read the helper makes, in order; `os.kill` and the send do nothing."""
    seen: list[tuple] = []
    real_read, real_open = killhelper._read, killhelper._open_pidfd
    monkeypatch.setattr(killhelper.os, "kill", lambda pid, signum: seen.append(("kill", pid, signum)))
    monkeypatch.setattr(killhelper, "_send_through", lambda handle, signum: seen.append(("send", signum)))
    monkeypatch.setattr(killhelper, "_read", lambda pid, name: (seen.append(("read", name)), real_read(pid, name))[1])
    monkeypatch.setattr(killhelper, "_open_pidfd", lambda pid: (seen.append(("open", pid)), real_open(pid))[1])
    return seen


def no_pidfd(monkeypatch) -> None:
    """A kernel older than Linux 5.3: `pidfd_open` answers ENOSYS."""

    def refused(pid):
        raise OSError(errno.ENOSYS, "Function not implemented")

    monkeypatch.setattr(killhelper, "_open_pidfd", refused)


def pidfd_kernel() -> bool:
    try:
        os.close(killhelper._open_pidfd(os.getpid()))
    except OSError as error:
        if error.errno == errno.ENOSYS:
            return False
        raise
    return True


# ------------------------------------------------------------------------------------------------------ the pidfd path
def test_o72_a_pidfd_is_opened_first_and_held_while_the_process_is_checked_and_signalled(monkeypatch, sleeper):
    """O72: with a pidfd the signal goes through it, never through `os.kill`: the descriptor is opened before the
    process's start tick and command line are read, so a pid given to another process after that is never signalled."""
    pid, start, digest = identity_of(sleeper)
    seen: list[tuple] = []
    handle = os.open(os.devnull, os.O_RDONLY)  # stands for the pidfd, which the helper closes
    monkeypatch.setattr(killhelper, "_open_pidfd", lambda target: (seen.append(("open", target)), handle)[1])
    monkeypatch.setattr(killhelper, "_send_through", lambda fd, signum: seen.append(("send", fd, signum)))
    monkeypatch.setattr(killhelper.os, "kill", lambda *args: seen.append(("kill", *args)))
    real_read = killhelper._read
    monkeypatch.setattr(
        killhelper, "_read", lambda target, name: (seen.append(("read", name)), real_read(target, name))[1]
    )
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.SIGNALLED
    assert seen[0] == ("open", pid)
    assert {("read", "stat"), ("read", "cmdline")} <= set(seen[1:-1])
    assert seen[-1] == ("send", handle, signal.SIGKILL)
    assert not [event for event in seen if event[0] == "kill"]
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(handle)  # closed


@pytest.mark.skipif(not pidfd_kernel(), reason="this kernel has no pidfd_open")
def test_o72_a_process_is_ended_through_a_real_pidfd(sleeper):
    pid, start, digest = identity_of(sleeper)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.SIGNALLED
    assert sleeper.wait(timeout=10) == -signal.SIGKILL


@pytest.mark.parametrize("pidfd", [True, False], ids=["with a pidfd", "without one"])
@pytest.mark.parametrize(
    ("what", "outcome"),
    [("start", killhelper.REPLACED), ("command", killhelper.CHANGED), ("both", killhelper.REPLACED)],
)
def test_o72_a_pid_whose_start_time_or_command_line_changed_since_the_listing_is_never_signalled(
    monkeypatch, sleeper, pidfd, what, outcome
):
    """O72: the process now holding the pid is not the one listed: another start tick means the listed process ended
    and its pid was reused; the same tick with another command line means it is the listed process, changed (an exec).
    Neither is signalled, by the pidfd or by `os.kill`."""
    pid, start, digest = identity_of(sleeper)
    if what in ("start", "both"):
        start = str(int(start) + 1)
    if what in ("command", "both"):
        digest = "0" * 64
    if not pidfd:
        no_pidfd(monkeypatch)
    seen = recording(monkeypatch)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == outcome
    assert not [event for event in seen if event[0] in ("kill", "send")]


def test_o72_without_a_pidfd_the_identity_is_read_just_before_one_os_kill(monkeypatch, sleeper):
    """O72: a kernel with no pidfd (Linux before 5.3): the checks are made and the signal sent with `os.kill` at once,
    after the last read, once."""
    pid, start, digest = identity_of(sleeper)
    no_pidfd(monkeypatch)
    seen = recording(monkeypatch)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.SIGNALLED
    assert [event for event in seen if event[0] == "kill"] == [("kill", pid, signal.SIGKILL)]
    assert seen[-1][0] == "kill"
    assert {("read", "stat"), ("read", "cmdline")} <= set(seen[:-1])


def test_o72_a_failure_of_pidfd_open_other_than_enosys_is_never_worked_around_by_os_kill(monkeypatch, sleeper):
    """O72: only a kernel without the call falls back; too many open files, say, signals nothing."""
    pid, start, digest = identity_of(sleeper)

    def exhausted(target):
        raise OSError(errno.EMFILE, "Too many open files")

    seen = recording(monkeypatch)
    monkeypatch.setattr(killhelper, "_open_pidfd", exhausted)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.FAILED
    assert not [event for event in seen if event[0] in ("kill", "send")]


# ------------------------------------------------------------------------------------------------------ gone, refused
@pytest.mark.parametrize("pidfd", [True, False], ids=["with a pidfd", "without one"])
def test_o72_a_process_that_is_gone_is_handled_and_nothing_is_signalled(monkeypatch, sleeper, pidfd):
    """O72: a process that ended (and was reaped) between the listing and the signal is `gone`, never an error; a pid
    already given to another process in that time is `replaced`."""
    pid, start, digest = identity_of(sleeper)
    REAL_KILL(pid, signal.SIGKILL)
    sleeper.wait(timeout=10)
    if not pidfd:
        no_pidfd(monkeypatch)
    seen = recording(monkeypatch)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) in (killhelper.GONE, killhelper.REPLACED)
    assert not [event for event in seen if event[0] in ("kill", "send")]


@pytest.mark.parametrize("pid", [1, 0, -1, True, "5", None, 2.0])
def test_o72_pid_1_and_below_and_anything_but_an_integer_is_never_signalled(monkeypatch, pid):
    """O72: no init, no process group (pid 0 or below, which `kill` takes for one), and no pid that is not a number."""
    seen = recording(monkeypatch)
    assert killhelper.signal_target(pid, "100", "0" * 64, signal.SIGKILL) == killhelper.REFUSED
    assert seen == []


@pytest.mark.parametrize("start", [None, "", 5, "x1"])
def test_o72_a_start_tick_that_is_not_one_is_refused(monkeypatch, sleeper, start):
    seen = recording(monkeypatch)
    _, _, digest = identity_of(sleeper)
    assert killhelper.signal_target(sleeper.pid, start, digest, signal.SIGKILL) == killhelper.REFUSED
    assert seen == []


@pytest.mark.parametrize("pidfd", [True, False], ids=["with a pidfd", "without one"])
def test_o72_a_process_whose_identity_cannot_be_read_is_never_signalled(monkeypatch, sleeper, pidfd):
    """O72: a listing that could not read the command line records none, and a /proc entry that cannot be read now is
    the same: unknown is never a match."""
    pid, start, digest = identity_of(sleeper)
    if not pidfd:
        no_pidfd(monkeypatch)
    seen = recording(monkeypatch)
    assert killhelper.signal_target(pid, start, None, signal.SIGKILL) == killhelper.UNREADABLE

    def denied(target, name):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(killhelper, "_read", denied)
    assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.UNREADABLE
    assert not [event for event in seen if event[0] in ("kill", "send")]


# ------------------------------------------------------------------------------------------- as sudo runs it, whole
def run_helper(name: str, targets: list[dict]) -> dict:
    done = subprocess.run(
        [sys.executable, "-I", "-c", procsignal.PROGRAM, name],
        input=json.dumps({"targets": targets}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return {entry["pid"]: entry["outcome"] for entry in json.loads(done.stdout)["results"]}


def target(identity: tuple[int, str, str]) -> dict:
    pid, start, digest = identity
    return {"pid": pid, "start": start, "cmd_sha256": digest}


def test_o72_the_program_as_sudo_runs_it_answers_for_each_target_and_signals_only_the_matching_ones(sleeper):
    """O72: the whole program, `PYTHON -I -c SOURCE KILL` with the request on stdin: the listed process ends, the one
    whose pid was taken since is left alone, and pid 1 is refused."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        mine, theirs = identity_of(sleeper), identity_of(other)
        stale = (theirs[0], str(int(theirs[1]) + 1), theirs[2])  # the listing saw another process hold that pid
        answers = run_helper("KILL", [target(mine), target(stale), {"pid": 1, "start": "1", "cmd_sha256": "0" * 64}])
        assert answers == {mine[0]: "signalled", theirs[0]: "replaced", 1: "refused"}
        assert sleeper.wait(timeout=10) == -signal.SIGKILL
        time.sleep(0.2)
        assert other.poll() is None  # not signalled
    finally:
        REAL_KILL(other.pid, signal.SIGKILL)
        other.wait()


def test_o72_the_program_refuses_a_signal_it_was_not_given_a_name_for():
    for argv in (["KILL", "KILL"], ["HUP"], []):
        done = subprocess.run(
            [sys.executable, "-I", "-c", procsignal.PROGRAM, *argv],
            input="{}",
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 2


# ------------------------------------------------------------------- the digest the harness lists and the helper reads
@pytest.mark.parametrize(
    "argument",
    ["plain", "with\rcarriage", "with\r\nline", "tab\tand\nnewline", "é中", os.fsdecode(b"\xff\xfebad")],
    ids=["plain", "CR", "CRLF", "LF", "unicode", "undecodable bytes"],
)
def test_o72_the_digest_the_harness_lists_is_the_one_the_helper_reads(sleeper, argument):
    """O72: the harness reads a command line as text (`HostOps.read_text`: universal newlines, bad bytes replaced) and
    the helper as bytes, so both are put in one form first. An argument the agent chose, a carriage return among them,
    must never make its own leftover look like another process."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", argument])
    try:
        pid, start, digest = identity_of(process)
        assert killhelper.signal_target(pid, start, digest, signal.SIGKILL) == killhelper.SIGNALLED
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            REAL_KILL(process.pid, signal.SIGKILL)
            process.wait()
