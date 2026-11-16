"""O55: the bounded runner takes a command's marker lines out of its output, and says whether its input went in whole.

A cell's completion record (`completion.py`) travels in the cell's own stdout and stderr, as one line each that starts
with a marker only that exec knows. `run_bounded(..., marker=...)` takes every such line out of the stream before the
stream is counted and capped, so the text and the counts are exactly the cell's own bytes, and hands the lines' payloads
back (`CmdResult.stdout_marks`, `stderr_marks`). It also writes the input itself and says when it could not write all of
it, or close it cleanly (`CmdResult.input_error`): a command that stopped reading its program is never taken to have
run it. Real child processes here are the Python interpreter running one-line programs.
"""

import contextlib
import os
import signal
import sys
import threading

import pytest

from expcore_onedata.sandbox import hostops, marks
from expcore_onedata.sandbox.hostops import CmdResult, Counted, run_bounded

PY = sys.executable
MARK = b"\x1eexpcore-exec 0123abcd "


def py(code: str, **options) -> CmdResult:
    return run_bounded([PY, "-c", code], 30, **options)


def test_o55_a_marker_line_is_taken_out_of_the_output_and_its_payload_handed_back():
    """O55: the text and the counts are the command's own bytes; the line's payload, without its marker and newline,
    is in the marks."""
    code = (
        "import os; os.write(1, b'out'); os.write(1, b'\\x1eexpcore-exec 0123abcd {\"a\": 1}\\n'); "
        "os.write(2, b'err\\n\\x1eexpcore-exec 0123abcd {\"a\": 1}\\n')"
    )
    got = py(code, output_cap=1000, marker=MARK)
    assert (got.returncode, got.stdout, got.stderr) == (0, "out", "err\n")
    assert (got.stdout_marks, got.stderr_marks) == (('{"a": 1}',), ('{"a": 1}',))
    assert got.stdout_count == Counted(produced=3, kept=3)
    assert got.stderr_count == Counted(produced=4, kept=4)


def test_o55_without_a_marker_nothing_is_taken_out_and_no_marks_are_said():
    got = py("import os; os.write(1, b'\\x1eexpcore-exec 0123abcd {}\\n')")
    assert got.stdout == "\x1eexpcore-exec 0123abcd {}\n"
    assert (got.stdout_marks, got.stderr_marks) == (None, None)


def test_o55_a_stream_with_no_marker_line_has_no_marks():
    got = py("print('plain')", marker=MARK)
    assert (got.stdout, got.stdout_marks, got.stderr_marks) == ("plain\n", (), ())


def test_o55_another_execs_marker_is_the_commands_own_output():
    """O55: a line with another nonce is not this exec's record: it stays in the text and is no mark."""
    got = py("import os; os.write(1, b'\\x1eexpcore-exec ffffffff {}\\n')", marker=MARK)
    assert got.stdout == "\x1eexpcore-exec ffffffff {}\n"
    assert got.stdout_marks == ()


def test_o55_a_marker_line_after_more_than_the_cap_is_still_found_and_the_cut_counts_only_the_commands_bytes():
    """O55 with O43: the record comes after the cell's output, so a cell that wrote past the cap would push it into the
    dropped part; it is taken out before the cap, so it is found, and `produced` is the cell's bytes alone."""
    code = "import os; os.write(1, b'x' * 5000); os.write(1, b'\\x1eexpcore-exec 0123abcd {\"s\": 0}\\n')"
    got = py(code, output_cap=100, marker=MARK)
    assert got.stdout == "x" * 100
    assert got.stdout_count == Counted(produced=5000, kept=100)
    assert got.stdout_marks == ('{"s": 0}',)


class Trickle:
    """A stream that gives `size` bytes a read, as a pipe may."""

    def __init__(self, data: bytes, size: int) -> None:
        self._data, self._size = data, size

    def read1(self, n: int) -> bytes:
        chunk, self._data = self._data[: min(n, self._size)], self._data[min(n, self._size) :]
        return chunk


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
def test_o55_a_marker_line_split_across_reads_is_found_whole(size):
    """O55: the reader takes what one read gives; a marker line cut anywhere between reads is still one line."""
    data = b"head" + MARK + b'{"k": "v"}\n' + b"tail\x1e"
    pipe = hostops._Pipe(Trickle(data, size), None, MARK)
    pipe.run()
    kept, produced = pipe.snapshot()
    assert (kept, produced) == (b"headtail\x1e", 9)
    assert pipe.marks == [b'{"k": "v"}']


def test_o55_a_marker_without_its_line_end_is_output_and_no_mark():
    """O55: a stream cut inside the record leaves no record; what was read of it stays in the text."""
    out, held = marks.take(MARK + b'{"k"', MARK, [], final=True)
    assert (out, held) == (MARK + b'{"k"', b"")


def test_o55_a_marker_line_longer_than_a_record_is_output_and_no_mark():
    found: list[bytes] = []
    long_line = MARK + b"x" * (marks.MAX_PAYLOAD_BYTES + 1) + b"\n"
    out, held = marks.take(long_line, MARK, found, final=True)
    assert (out, held, found) == (long_line, b"", [])


def test_o55_an_input_written_whole_and_closed_has_no_input_error():
    got = run_bounded([PY, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], 30, input="abc" * 1000)
    assert (got.returncode, got.stdout, got.input_error) == (0, "abc" * 1000, None)


def test_o55_an_input_the_command_stopped_reading_is_said_with_the_bytes_written():
    """O55: a command that closes its stdin before reading all of it (the docker client that stops forwarding a cell's
    program) leaves the rest unwritten: the runner says so, never that the input went in."""
    code = "import os, time; os.close(0); time.sleep(0.5)"
    got = run_bounded([PY, "-c", code], 30, input="q" * 4_000_000)
    assert got.returncode == 0
    assert got.input_error is not None
    assert "of 4000000 bytes written" in got.input_error
    assert "BrokenPipeError" in got.input_error


def test_o55_no_input_is_no_input_error():
    assert py("print(1)").input_error is None


def test_o55_the_in_memory_cut_takes_marker_lines_out_as_the_runner_does():
    """O55: `apply_output_cap` with a marker is what the in-memory host uses: the lines go, their payloads are said,
    and the cap and the counts are the command's own bytes."""
    result = CmdResult(0, "x" * 50 + MARK.decode() + "{}\n", MARK.decode() + "{}\n")
    got = hostops.apply_output_cap(result, 10, marker=MARK)
    assert (got.stdout, got.stdout_count, got.stdout_marks) == ("x" * 10, Counted(50, 10), ("{}",))
    assert (got.stderr, got.stderr_count, got.stderr_marks) == ("", Counted(0, 0), ("{}",))
    kept = hostops.apply_output_cap(CmdResult(1, "", "", input_error="1 of 2 bytes written (BrokenPipeError)"), 10)
    assert kept.input_error == "1 of 2 bytes written (BrokenPipeError)"


class Held:
    """A stream that gives its first bytes, then blocks until released, as a pipe a lingering worker holds open does."""

    def __init__(self, first: bytes) -> None:
        self._first, self.release, self.blocked = first, threading.Event(), threading.Event()
        self._late = b"late"

    def read1(self, n: int) -> bytes:
        if self._first:
            first, self._first = self._first, b""
            return first
        self.blocked.set()
        self.release.wait(10)
        late, self._late = self._late, b""  # one late chunk, then the end of the stream
        return late


def test_o55_a_reader_given_up_on_keeps_the_bytes_it_held_back_as_output_and_counts_them():
    """O55 with O43: bytes held back as the possible start of a marker line, when the runner gives up on the stream (a
    worker holds the pipe past the time), are the command's output: they are kept and counted, never dropped, and
    nothing read after the abandonment is kept."""
    stream = Held(b"head\x1e")
    pipe = hostops._Pipe(stream, None, MARK)
    pipe.start()
    assert stream.blocked.wait(10)
    pipe.abandon()
    assert pipe.snapshot() == (b"head\x1e", 5)
    stream.release.set()
    pipe.join(10)
    assert not pipe.is_alive()
    assert pipe.snapshot() == (b"head\x1e", 9)  # the late bytes are counted, and none of them kept


def test_o55_a_timed_out_command_keeps_the_bytes_it_held_back_whole(tmp_path):
    """O55: through the runner, a command that wrote a byte that may begin a marker line and left a worker holding the
    pipe past its time keeps that byte in the timed-out result's output."""
    pid_file = tmp_path / "worker.pid"
    code = (
        "import os, subprocess, sys\n"
        "os.write(1, b'head\\x1e')\n"
        "w = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(sys.argv[1], 'w').write(str(w.pid))\n"
    )
    try:
        got = run_bounded([PY, "-c", code, str(pid_file)], 0.5, marker=MARK, output_cap=100)
        assert got.returncode == hostops.TIMED_OUT
        assert got.stdout == "head\x1e"
        assert got.stdout_count == Counted(produced=5, kept=5)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
