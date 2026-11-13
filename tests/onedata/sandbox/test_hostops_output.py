"""O40, O39: a command the host runs is bounded in what it keeps of its output and in how long it can hold the caller.

`hostops.run_bounded` is what `DockerHost` runs every command with. The output of a cell of the agent's code goes
through it: a cell that printed 1.4 MB of a 1 MiB read had all of it read into memory and kept. With a cap, the first
bytes are kept and the rest read and counted, so the command is never blocked on a full pipe and memory stays flat;
what was produced and kept is said (`Counted`). A command that does not end after its kill (a process in an
uninterruptible sleep on a FUSE mount whose provider is gone cannot die) is given up on after a bound, never waited
for for ever. Real child processes here are the Python interpreter running one-line programs.
"""

import io
import subprocess
import sys
import time
import tracemalloc

import pytest

from expcore_onedata.sandbox import hostops
from expcore_onedata.sandbox.hostops import TIMED_OUT, CmdResult, Counted, run_bounded

PY = sys.executable


def py(code: str, *, timeout_s: float = 30, **options) -> CmdResult:
    return run_bounded([PY, "-c", code], timeout_s, **options)


def test_o40_a_command_without_a_cap_keeps_all_its_output_and_counts_nothing():
    got = py("import sys; print('out'); print('err', file=sys.stderr)")
    assert (got.returncode, got.stdout, got.stderr) == (0, "out\n", "err\n")
    assert (got.stdout_count, got.stderr_count) == (None, None)


def test_o40_output_over_the_cap_is_cut_and_the_bytes_produced_and_kept_are_said():
    got = py("import sys; sys.stdout.write('x' * 100_000); sys.stderr.write('y' * 5_000)", output_cap=1000)
    assert got.returncode == 0
    assert got.stdout == "x" * 1000
    assert got.stdout_count == Counted(produced=100_000, kept=1000)
    assert got.stderr == "y" * 1000
    assert got.stderr_count == Counted(produced=5_000, kept=1000)
    assert got.stdout_count.cut
    assert got.stderr_count.cut


def test_o40_output_within_the_cap_is_whole_and_counted_as_not_cut():
    got = py("print('abc')", output_cap=1000)
    assert got.stdout == "abc\n"
    assert got.stdout_count == Counted(produced=4, kept=4)
    assert not got.stdout_count.cut
    assert got.stderr_count == Counted(produced=0, kept=0)


def test_o40_memory_stays_flat_however_much_a_command_writes():
    """O40: 24 MB are written and 1 KiB kept; the caller's peak allocation is far below the output."""
    tracemalloc.start()
    try:
        got = py("import sys; [sys.stdout.write('z' * 65536) for _ in range(384)]", output_cap=1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got.stdout_count == Counted(produced=384 * 65536, kept=1024)
    assert peak < 4 << 20


def test_o40_a_cut_never_splits_a_character_and_what_was_kept_is_the_whole_characters():
    got = py("import sys; sys.stdout.buffer.write('é'.encode() * 10)", output_cap=5)
    assert got.stdout == "éé"  # 4 of the 5 bytes: the fifth began a third character
    assert got.stdout_count == Counted(produced=20, kept=4)


def test_o40_bytes_that_are_not_utf8_are_replaced_and_line_ends_are_universal():
    got = py("import sys; sys.stdout.buffer.write(b'a\\xffb\\r\\nc\\rd')")
    assert got.stdout == "a�b\nc\nd"


def test_o40_a_large_input_does_not_deadlock_a_command_that_writes_while_it_reads():
    """The input goes in from a thread of its own, while the output is read: a command that echoes as it reads would
    block both sides on full pipes otherwise."""
    text = "q" * 2_000_000
    code = "import sys; sys.stdout.write(sys.stdin.read())"
    got = run_bounded([PY, "-c", code], 60, input=text, output_cap=100)
    assert got.stdout == "q" * 100
    assert got.stdout_count == Counted(produced=2_000_000, kept=100)


def test_o39_a_command_past_its_time_is_killed_and_reported_as_timed_out_with_what_it_had_written():
    began = time.monotonic()
    got = py("import sys, time; print('first', flush=True); time.sleep(60)", timeout_s=1.5)
    assert time.monotonic() - began < 20
    assert got.returncode == TIMED_OUT
    assert got.stderr == "timed out after 1.5s"
    assert got.stdout == "first\n"


def test_o39_a_missing_program_is_the_exit_a_shell_gives_it():
    got = run_bounded(["/nonexistent/program"], 5)
    assert got.returncode == 127
    assert "could not be run" in got.stderr


class Undying:
    """A child that does not end after SIGKILL, as a process in uninterruptible sleep does, with its output so far."""

    def __init__(self, held: bytes = b"partial"):
        self.stdin = io.BytesIO()
        self.stdout = io.BufferedReader(io.BytesIO(held))
        self.stderr = io.BufferedReader(io.BytesIO(b""))
        self.returncode = None
        self.killed = 0
        self.waits: list[float | None] = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired("docker", timeout)

    def kill(self):
        self.killed += 1

    def poll(self):
        return None


def test_o39_a_command_that_does_not_die_after_its_kill_is_given_up_on_and_never_waited_for_for_ever(monkeypatch):
    """O39: `subprocess.run` waits for a killed child without a bound, so a process in D state held the caller, and
    through it the whole teardown. The wait after the kill is bounded now; the result is the timeout, with the output
    the command had written."""
    child = Undying()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(hostops, "KILL_WAIT_S", 0.2)
    began = time.monotonic()
    got = run_bounded(["docker", "rm"], 0.3)
    assert time.monotonic() - began < 5
    assert (got.returncode, got.stdout, got.stderr) == (TIMED_OUT, "partial", "timed out after 0.3s")
    assert child.killed == 1
    assert child.waits == [0.3, 0.2]  # its time, then the bounded wait after the kill and no other


def test_o39_the_docker_host_runs_every_command_through_the_bounded_runner(monkeypatch):
    seen = []

    def bounded(argv, timeout_s, *, env=None, input=None, output_cap=None):
        seen.append((list(argv), timeout_s, output_cap))
        return CmdResult(0, "ok")

    monkeypatch.setattr(hostops, "run_bounded", bounded)
    host = hostops.DockerHost()
    assert host.docker("ps", output_cap=7, timeout=9).stdout == "ok"
    assert host.run(["echo", "x"], 3).stdout == "ok"
    assert seen == [(["docker", "ps"], 9, 7), (["echo", "x"], 3, None)]


def test_o40_the_cap_applies_to_a_result_the_same_way_whoever_made_it():
    """`apply_output_cap` is what the in-memory host (and any test double) uses: it cuts a result's text as the real
    runner does, and a result already within the cap is only counted."""
    whole = hostops.apply_output_cap(CmdResult(0, "é" * 10, "err"), 5)
    assert (whole.stdout, whole.stdout_count) == ("éé", Counted(produced=20, kept=4))
    assert (whole.stderr, whole.stderr_count) == ("err", Counted(produced=3, kept=3))
    assert hostops.apply_output_cap(CmdResult(0, "abc"), None) == CmdResult(0, "abc")


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_o40_a_cap_is_a_positive_whole_number_of_bytes(cap):
    with pytest.raises(ValueError, match="output cap"):
        py("print(1)", output_cap=cap)
