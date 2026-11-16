"""O55: a cell's exec is known to have run its program whole and ended, or it is not a result.

`python -` given no program runs an empty one: exit 0, nothing on stdout or stderr. And `docker exec` reports exit 0
for an exec whose exit it never recorded (its client reads the exit code from the exec's inspection, where an exit not
recorded is a null it takes as 0). Either reads as a cell that ran and printed nothing. A cell now runs under a
bootstrap (`completion.BOOTSTRAP`) that reads the program whole, checks its size and SHA-256 against what the harness
sent, runs it in the interpreter as before (`python -`), and, after that interpreter has exited, writes one completion
record on each stream: this exec's nonce, whether the source arrived whole, and the exit status. `Completion.check`
reads a result against it.

Here the bootstrap runs for real, in the host's Python, through the bounded runner, as `docker exec` would run it in the
agent's container; the checks run over results made by hand, which stand for what `docker exec` can return.
"""

import json
import sys

import pytest

from expcore_onedata.sandbox import completion
from expcore_onedata.sandbox.completion import Completion
from expcore_onedata.sandbox.hostops import CmdResult, encode_input, run_bounded

PY = sys.executable


def run(code: str, *, delivered: str | None = None, cap: int | None = 1 << 20) -> tuple[Completion, CmdResult]:
    """Run `code` under the bootstrap, with `delivered` as what reaches its stdin (the whole program by default)."""
    done = Completion.for_source(code)
    stdin = code if delivered is None else delivered
    return done, run_bounded(done.argv(PY), 60, input=stdin, output_cap=cap, marker=done.marker)


def payload(done: Completion, rc: int) -> str:
    """The payload of the completion record the bootstrap writes after a program whole that exited `rc`."""
    return json.dumps({"source_bytes": done.size, "source_whole": True, "exit_status": rc}, sort_keys=True)


# ----------------------------------------------------------------------------- the bootstrap, run for real
def test_o55_a_cells_output_is_its_own_byte_for_byte_and_its_exec_is_verified():
    code = "import sys\nprint('out')\nsys.stdout.write('no newline')\nprint('err', file=sys.stderr)\n"
    done, got = run(code)
    assert (got.returncode, got.stdout, got.stderr) == (0, "out\nno newline", "err\n")
    check = done.check(got)
    assert check.cause is None
    assert check.facts == {"source_whole": True, "exit_status": 0, "nonce_matched": True}


def test_o55_a_genuine_exit_0_that_prints_nothing_stays_a_success():
    """O55: the program ran, whole, and printed nothing: no different from before but for being verified."""
    done, got = run("x = 1\n")
    assert (got.returncode, got.stdout, got.stderr) == (0, "", "")
    assert done.check(got).cause is None


def test_o55_os_exit_0_after_the_program_started_is_a_verified_success():
    """O55: the record is written by the bootstrap after the interpreter has exited, so a program that leaves by
    `os._exit(0)`, with no clean-up of its own, is still recorded."""
    done, got = run("import os\nos._exit(0)\n")
    assert (got.returncode, got.stdout, got.stderr) == (0, "", "")
    check = done.check(got)
    assert check.cause is None
    assert check.facts["exit_status"] == 0


def test_o55_an_exit_status_is_the_programs_own_and_recorded():
    done, got = run("import sys\nsys.exit(3)\n")
    assert got.returncode == 3
    check = done.check(got)
    assert (check.cause, check.facts["exit_status"]) == (None, 3)


def test_o55_a_program_killed_by_a_signal_ends_the_exec_the_same_way_and_says_so():
    """O55: the bootstrap ends itself by the signal that ended the program, so `timeout` and `docker exec` see what
    they saw before (137 for SIGKILL); the record says 137, as the shell convention does."""
    done, got = run("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert got.returncode == -9  # here the bootstrap is the runner's child: docker would report 137
    check = done.check(got)
    assert (check.cause, check.facts["exit_status"]) == (None, 137)


def test_o55_a_traceback_is_the_programs_own_with_its_line_numbers():
    done, got = run("x = 1\n1 / 0\n")
    assert got.returncode == 1
    assert 'File "<stdin>", line 2' in got.stderr
    assert got.stderr.rstrip().endswith("ZeroDivisionError: division by zero")
    assert done.check(got).cause is None


def test_o55_a_program_that_writes_past_the_cap_is_still_verified():
    done, got = run("import sys\nsys.stdout.write('x' * 5000)\n", cap=100)
    assert got.stdout == "x" * 100
    assert done.check(got).cause is None


def test_o55_a_program_that_never_arrived_is_said_and_never_run():
    """O55 (a): the stdin of the exec delivered nothing. Before, `python -` ran an empty program and exited 0 with
    nothing printed; the bootstrap runs nothing and says what arrived."""
    done, got = run("print('ran')\n", delivered="")
    assert "ran" not in got.stdout
    check = done.check(got)
    assert (
        check.cause
        == "the cell's source did not arrive whole in the container (0 of 13 bytes): its program did not run"
    )
    assert check.facts == {"source_whole": False, "exit_status": None, "nonce_matched": True}


def test_o55_a_program_that_arrived_in_part_is_said_and_never_run():
    code = "print('first')\nprint('second')\n"
    done, got = run(code, delivered=code[:10])
    assert "first" not in got.stdout
    assert done.check(got).cause == (
        "the cell's source did not arrive whole in the container (10 of 31 bytes): its program did not run"
    )


def test_o55_a_program_of_the_same_size_but_other_bytes_is_not_whole():
    code = "print('aaaa')\n"
    done, got = run(code, delivered="print('bbbb')\n")
    assert "bbbb" not in got.stdout
    assert done.check(got).facts["source_whole"] is False


def test_o55_the_bootstrap_ignores_a_module_the_agent_left_in_the_working_directory(tmp_path, monkeypatch):
    """O55: the bootstrap runs isolated (`-I`), so a `subprocess.py` or `hashlib.py` the agent wrote into its working
    directory cannot take its place; the program itself runs as before, with the directory on its path."""
    (tmp_path / "hashlib.py").write_text("raise SystemExit('hijacked')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    done, got = run("import sys\nprint(sys.path[0] == '')\n")
    assert (got.returncode, got.stdout) == (0, "True\n")
    assert done.check(got).cause is None


# ----------------------------------------------------------------------------- the check, over what docker can return
def test_o55_an_exit_0_with_no_record_is_an_exit_not_recorded():
    """O55 (b): the attach stream ended before the completion record came, and docker reported 0 for an exit it never
    saw: never a success."""
    done = Completion.for_source("print(1)\n")
    check = done.check(CmdResult(0, "", "", stdout_marks=(), stderr_marks=()))
    assert check.cause == (
        "the exec's exit was not recorded: docker exec reported exit 0 with no completion record from the container, "
        "so its program may not have run, or its output ended before the program did"
    )
    assert check.facts == {"source_whole": None, "exit_status": None, "nonce_matched": False}


def test_o55_output_with_no_record_is_an_exit_not_recorded_too():
    done = Completion.for_source("print(1)\n")
    assert done.check(CmdResult(0, "1\n", "", stdout_marks=(), stderr_marks=())).cause.startswith(
        "the exec's exit was not recorded"
    )


def test_o55_a_stream_that_ended_before_its_record_is_output_lost():
    done = Completion.for_source("print(1)\n")
    check = done.check(CmdResult(0, "", "", stdout_marks=(), stderr_marks=(payload(done, 0),)))
    assert check.cause == "the cell's stdout ended before its completion record: its output was lost"
    assert check.facts["nonce_matched"] is False


def test_o55_a_status_docker_reports_other_than_the_recorded_one_is_an_exit_not_recorded():
    done = Completion.for_source("print(1)\n")
    line = payload(done, 1)
    check = done.check(CmdResult(0, "", "", stdout_marks=(line,), stderr_marks=(line,)))
    assert check.cause == (
        "the exec's exit was not recorded: docker exec reported exit 0 and the container recorded exit 1"
    )
    assert check.facts["exit_status"] == 1


def test_o55_an_input_not_written_whole_is_said_when_no_record_came():
    done = Completion.for_source("print(1)\n")
    got = CmdResult(0, "", "", stdout_marks=(), stderr_marks=(), input_error="0 of 9 bytes written (BrokenPipeError)")
    assert done.check(got).cause == (
        "the cell's source was not written whole to docker exec (0 of 9 bytes written (BrokenPipeError)): its program "
        "did not run"
    )


@pytest.mark.parametrize("payload", ["not json", "[]", '{"source_whole": true}', '{"source_whole": "yes"}'])
def test_o55_a_record_that_does_not_read_is_no_record(payload):
    done = Completion.for_source("print(1)\n")
    check = done.check(CmdResult(0, "", "", stdout_marks=(payload,), stderr_marks=(payload,)))
    assert check.cause == "the completion record of the exec could not be read: its exit is not known"


def test_o55_two_records_on_a_stream_are_no_record():
    done = Completion.for_source("print(1)\n")
    line = payload(done, 0)
    check = done.check(CmdResult(0, "", "", stdout_marks=(line, line), stderr_marks=(line,)))
    assert check.cause == "the completion record of the exec could not be read: its exit is not known"


def test_o55_the_record_line_is_the_marker_then_the_payload_then_a_newline():
    done = Completion.for_source("print(1)\n")
    line = completion.record_line(done.nonce, {"exit_status": 0, "source_whole": True})
    assert line.encode() == done.marker + b'{"exit_status": 0, "source_whole": true}\n'


def test_o55_each_exec_has_its_own_nonce_and_a_marker_that_names_it():
    first, second = Completion.for_source("x"), Completion.for_source("x")
    assert first.nonce != second.nonce
    assert len(first.nonce) == 32
    assert first.marker == b"\x1eexpcore-exec " + first.nonce.encode() + b" "
    assert (first.size, first.sha256) == (1, __import__("hashlib").sha256(encode_input("x")).hexdigest())


def test_o55_the_argv_runs_the_bootstrap_isolated_with_the_interpreter_named_for_the_program():
    done = Completion.for_source("print(1)\n")
    argv = done.argv("python3")
    assert argv[:4] == ["python3", "-I", "-c", completion.BOOTSTRAP]
    assert argv[4:] == [done.nonce, str(done.size), done.sha256, "python3"]
