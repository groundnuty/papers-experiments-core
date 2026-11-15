"""O55: a cell whose exec did not complete raises, naming why; it never reads as a cell that ran and printed nothing.

A live Day-0 read cell, a program that prints a JSON line on every branch, came back as exit 0 with empty stdout and
stderr within 1.31 s. Two things below the harness give exactly that: the program lost on its way to `python -`'s stdin
(which then runs an empty program), and a `docker exec` that reports exit 0 for an exit it never recorded. Both reach
every cell, the agent's included, through `DockerSandbox.exec` and `CellRunner.run`. Each cell now runs under the
completion bootstrap (`completion.py`), and the fake host plays it (`fake_host.FakeHost._run_cell`): the handler's
result is the program's own, and the completion record is added to it unless the test says the record never came.
"""

import asyncio
import threading

import pytest
from fake_host import Unrecorded
from sandbox_support import make_spec, start_options

from expcore.errors import InfraError
from expcore.plugin import NO_WAITS
from expcore_onedata.sandbox import completion
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.completion import ExecIncomplete
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

VERIFIED = {"source_whole": True, "exit_status": 0, "nonce_matched": True}
NOT_RECORDED = "the exec's exit was not recorded: docker exec reported exit 0 with no completion record"


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    sandbox.stop()


def answers(result: CmdResult):
    return lambda container, argv, stdin: result


@pytest.fixture(params=[True, False], ids=["agent-cell", "harness-step"])
def record(request):
    return request.param


# ----------------------------------------------------------------------------- (a) the program never ran whole
def test_o55_a_program_the_containers_stdin_never_got_raises_and_is_never_run(world, sbx, record):
    """O55 (a): nothing of the program reached the container. Before, `python -` ran an empty program: exit 0, nothing
    printed. Now the bootstrap runs nothing and records 0 bytes, and the exec raises naming it."""
    ran = []
    world.host.exec_handler = lambda container, argv, stdin: ran.append(stdin) or CmdResult(0, '{"ok": 1}\n')
    world.host.exec_delivers = lambda data: b""
    with pytest.raises(ExecIncomplete) as raised:
        sbx.exec("print('{\"ok\": 1}')", 30, record=record)
    assert raised.value.cause.startswith("the cell's source did not arrive whole in the container (0 of ")
    assert raised.value.cause.endswith("bytes): its program did not run")
    assert ran == []
    assert raised.value.result.exec_check == {"source_whole": False, "exit_status": None, "nonce_matched": True}


def test_o55_a_program_that_arrived_in_part_raises_and_is_never_run(world, sbx, record):
    world.host.exec_handler = answers(CmdResult(0))
    world.host.exec_delivers = lambda data: data[:5]
    code = "print('a whole program')"
    with pytest.raises(ExecIncomplete, match=rf"\(5 of {len(code)} bytes\): its program did not run"):
        sbx.exec(code, 30, record=record)


def test_o55_a_program_docker_exec_was_not_given_whole_raises_naming_the_short_write(world, sbx, record):
    """O55: the docker client stopped reading the program, and nothing came back: the write is what is named."""
    world.host.exec_handler = answers(Unrecorded(0))
    world.host.exec_input_error = "4 of 24 bytes written (BrokenPipeError)"
    with pytest.raises(ExecIncomplete) as raised:
        sbx.exec("print('a whole program')", 30, record=record)
    assert raised.value.cause == (
        "the cell's source was not written whole to docker exec (4 of 24 bytes written (BrokenPipeError)): its program "
        "did not run"
    )


# ----------------------------------------------------------------------------- (b) the exit never recorded
def test_o55_an_attach_stream_that_ended_before_the_record_raises_the_exit_not_recorded(world, sbx, record):
    """O55 (b): docker reported exit 0 for an exec whose record never came (its stream ended first, or the daemon never
    saw the exit): never a success with empty output."""
    world.host.exec_handler = answers(Unrecorded(0, "", ""))
    with pytest.raises(ExecIncomplete, match=NOT_RECORDED) as raised:
        sbx.exec("print(1)", 30, record=record)
    assert isinstance(raised.value, InfraError)
    assert raised.value.kind == "sandbox"
    assert isinstance(raised.value, SandboxError)  # a harness that catches the sandbox's failures catches it
    assert raised.value.result.exec_check == completion.UNKNOWN_FACTS


def test_o55_partial_output_with_no_record_is_the_exit_not_recorded_too(world, sbx, record):
    world.host.exec_handler = answers(Unrecorded(0, '{"progress": 1048576}\n', ""))
    with pytest.raises(ExecIncomplete, match=NOT_RECORDED):
        sbx.exec("print(1)", 30, record=record)


def test_o55_a_stream_cut_before_its_record_is_output_lost(world, sbx, record):
    """O55: the record came on stderr and not on stdout: what the program printed did not all arrive."""

    def stdout_cut(container, argv, stdin):
        nonce = argv[argv.index(completion.BOOTSTRAP) + 1]
        line = completion.record_line(nonce, {"source_bytes": len(stdin), "source_whole": True, "exit_status": 0})
        return Unrecorded(0, '{"res', line)

    world.host.exec_handler = stdout_cut
    with pytest.raises(
        ExecIncomplete, match="the cell's stdout ended before its completion record: its output was lost"
    ):
        sbx.exec("print(1)", 30, record=record)


def test_o55_an_exit_docker_reports_other_than_the_recorded_one_raises(world, sbx, record):
    def lost_exit(container, argv, stdin):
        nonce = argv[argv.index(completion.BOOTSTRAP) + 1]
        line = completion.record_line(nonce, {"source_bytes": len(stdin), "source_whole": True, "exit_status": 1})
        return Unrecorded(0, "out\n" + line, "Traceback\n" + line)

    world.host.exec_handler = lost_exit
    with pytest.raises(ExecIncomplete, match="docker exec reported exit 0 and the container recorded exit 1"):
        sbx.exec("1/0", 30, record=record)


# ----------------------------------------------------------------------------- what stays a result
def test_o55_a_genuine_exit_0_that_prints_nothing_stays_a_success_and_is_verified(world, sbx, record):
    world.host.exec_handler = answers(CmdResult(0, "", ""))
    result = sbx.exec("x = 1", 30, record=record)
    assert (result.exit_code, result.stdout, result.stderr, result.timed_out) == (0, "", "", False)
    assert result.exec_check == VERIFIED


def test_o55_a_cells_output_is_its_own_byte_for_byte(world, sbx, record):
    """O55: the record's lines are taken out of the streams: what the agent sees and the record keeps is the program's
    own output, nothing added."""
    world.host.exec_handler = answers(CmdResult(2, "out\nno newline", "err\n"))
    result = sbx.exec("print(1)", 30, record=record)
    assert (result.exit_code, result.stdout, result.stderr) == (2, "out\nno newline", "err\n")
    assert result.exec_check == {**VERIFIED, "exit_status": 2}


def test_o55_the_cells_record_and_the_steps_record_keep_the_check(world, sbx):
    world.host.exec_handler = answers(CmdResult(0, "1\n"))
    sbx.exec("print(1)", 30, record=False)
    sbx.exec("print(1)", 30)
    assert sbx.metadata()["harness_steps"][-1]["exec_check"] == VERIFIED
    assert sbx._cells.cells[-1]["exec_check"] == VERIFIED
    assert "exec_incomplete" not in sbx.metadata()


def test_o55_an_incomplete_cell_is_on_the_runs_record_with_its_cause(world, sbx):
    world.host.exec_handler = answers(Unrecorded(0))
    with pytest.raises(ExecIncomplete):
        sbx.exec("print(1)", 30)
    (entry,) = sbx._cells.cells
    assert entry["incomplete"].startswith(NOT_RECORDED)
    assert entry["exec_check"] == completion.UNKNOWN_FACTS
    assert sbx.metadata()["exec_incomplete"] == [{"cell": 1, "cause": entry["incomplete"]}]


def test_o55_an_incomplete_harness_step_is_recorded_with_its_cause_before_it_raises(world, sbx):
    world.host.exec_handler = answers(Unrecorded(0))
    with pytest.raises(ExecIncomplete):
        sbx.exec("print(1)", 30, record=False)
    (step,) = sbx.metadata()["harness_steps"]
    assert step["incomplete"].startswith(NOT_RECORDED)
    assert step["exec_check"] == completion.UNKNOWN_FACTS
    assert sbx.metadata()["cells"] == 0


def test_o55_a_cell_the_harnesss_timer_ended_is_its_time_limit_with_or_without_a_record(world, sbx):
    """O55 with O17: the harness's own timer killed the cell, the bootstrap with it, so no record came; the timer's
    record says how it ended, and the cell is the agent's time limit as before, never an incomplete exec."""
    cells, killing = sbx._cells, threading.Event()
    kill = cells._kill_at_the_cap

    def the_harness_kills(*cell):
        killing.set()
        return kill(*cell)

    def runs_until_killed(container, argv, stdin):
        assert killing.wait(10), "the harness's timer did not fire"
        return Unrecorded(137, "", "")

    cells._kill_at_the_cap = the_harness_kills
    world.host.exec_handler = runs_until_killed
    result = sbx.exec("while True: pass", 2.5)
    assert (result.exit_code, result.timed_out, result.kill["reason"]) == (137, True, "time_limit")
    assert result.exec_check["nonce_matched"] is False


def test_o55_a_host_call_given_up_on_is_a_timeout_and_no_incomplete_exec(world, sbx, record):
    world.host.exec_handler = answers(Unrecorded(124, "", "timed out after 31.5s"))
    result = sbx.exec("print(1)", 1.5, record=record)
    assert (result.timed_out, result.bound) == (True, "host_call")


def test_o55_a_cell_in_a_container_that_stopped_is_the_kill_it_was(world, sbx):
    """O55 with O17: the container stopped under the cell, so no record came; the container's own state says so, and the
    cell's kill is infrastructure as before (the code tool raises its kind), not an incomplete exec."""

    def dies(container, argv, stdin):
        container.running = False
        return Unrecorded(0, "", "")

    world.host.exec_handler = dies
    result = sbx.exec("print(1)", 30)
    assert result.kill is not None
    assert result.kill["category"] != "agent"


# ----------------------------------------------------------------------------- the code tool
def test_o55_the_code_tool_raises_the_incomplete_exec_as_sandbox_infrastructure(world, sbx):
    """O55: the agent's call never returns an empty success: its attempt ends as infrastructure of kind `sandbox`,
    retried within the attempt limit, with the cause."""
    world.host.exec_handler = answers(Unrecorded(0))
    tool = CodeExecutionProvider(sbx)
    with pytest.raises(InfraError, match=NOT_RECORDED) as raised:
        asyncio.run(tool.execute("python", {"code": "print(1)"}, timeout_s=30, ctx=NO_WAITS))
    assert raised.value.kind == "sandbox"
