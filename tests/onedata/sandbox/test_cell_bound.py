"""O39: a cell the sandbox's own bound ended says so, with `record=False` as with a cell of the agent's (a fake host).

A harness step (`record=False`) starts no timer: its only limit is the container's `timeout -s KILL`, `CAP_MARGIN_S`
after `timeout_s`, which ends the cell with exit 137, the code a cell that kills itself exits with too. What tells them
apart is the time: that kill comes only once the cell has run the whole of its limit.
"""

import threading
import time

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import bounds
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

TIMEOUT_S = 1.2  # a cell needs a second left once it is prepared to start; a harness step prepares nothing


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    sandbox.stop()


def limit_of(argv: list[str]) -> float:
    """The seconds the container's `timeout -s KILL` was given, as the sandbox put them in the command."""
    return float(argv[argv.index("KILL") + 1])


def runs_to_the_backstop(code: int):
    """A cell that runs the whole of its limit and is then ended by the container's own `timeout`, with `code`."""

    def handler(container, argv, stdin):
        time.sleep(limit_of(argv))
        return CmdResult(code, "", "")

    return handler


@pytest.mark.parametrize("code", [137, 124])
def test_o39_a_harness_step_the_containers_timeout_killed_is_timed_out_and_names_that_bound(world, sbx, code):
    """O39: `record=False` starts no timer, so the container's backstop is the only bound; a cell it killed at its limit
    is `timed_out`, with the bound named, and the harness step's record says the same: the exit code (137, or 124 from
    a `timeout` that reports its own kill so) is no longer all a caller has to go on."""
    world.host.exec_handler = runs_to_the_backstop(code)
    result = sbx.exec("while True: pass", TIMEOUT_S, record=False)
    assert (result.exit_code, result.timed_out, result.bound) == (code, True, bounds.CONTAINER_TIMEOUT)
    assert result.duration_s >= TIMEOUT_S
    step = sbx.metadata()["harness_steps"][-1]
    assert (step["exit_code"], step["timed_out"], step["bound"]) == (code, True, bounds.CONTAINER_TIMEOUT)
    assert sbx.metadata()["cells"] == 0  # still no cell of the agent's


@pytest.mark.parametrize("code", [137, 124])
def test_o39_a_harness_step_that_exits_137_by_itself_is_a_crash_not_a_timeout(world, sbx, code):
    """O39: a cell that kills itself (`os.kill(os.getpid(), 9)` exits 137 under `timeout`) or exits 124 on its own ends
    long before its limit: it is its own outcome, not timed out, and no bound is named."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(code, "", "")
    result = sbx.exec("import os; os.kill(os.getpid(), 9)", TIMEOUT_S, record=False)
    assert (result.exit_code, result.timed_out, result.bound) == (code, False, None)
    step = sbx.metadata()["harness_steps"][-1]
    assert (step["timed_out"], step["bound"]) == (False, None)


def test_o39_a_harness_step_that_ran_the_whole_limit_and_ended_by_itself_is_not_timed_out(world, sbx):
    """O39: reaching the limit alone is no kill: a step that ran its whole limit and exited 0 was not ended by one."""

    def finishes_late(container, argv, stdin):
        time.sleep(limit_of(argv))
        return CmdResult(0, "done\n", "")

    world.host.exec_handler = finishes_late
    result = sbx.exec("print('done')", TIMEOUT_S, record=False)
    assert (result.exit_code, result.timed_out, result.bound) == (0, False, None)


def test_o39_a_cell_the_harnesss_timer_ended_names_the_cell_timer_and_is_unchanged(world, sbx):
    """O39: with `record=True` the harness's own timer is the bound (O17, O34): `timed_out` and the kill record are what
    they were, and the bound the result names is that timer's."""
    cells, killing = sbx._cells, threading.Event()
    kill = cells._kill_at_the_cap

    def the_harness_kills(*cell):
        killing.set()
        return kill(*cell)

    def runs_until_killed(container, argv, stdin):
        assert killing.wait(10), "the harness's timer did not fire"
        return CmdResult(137, "", "")

    cells._kill_at_the_cap = the_harness_kills
    world.host.exec_handler = runs_until_killed
    result = sbx.exec("while True: pass", 2.5)
    assert (result.exit_code, result.timed_out, result.bound) == (137, True, bounds.CELL_TIMER)
    assert result.kill["reason"] == "time_limit"


def test_o39_a_cell_that_exits_137_by_itself_names_no_bound(world, sbx):
    """O39: with `record=True` too, an exit 137 the harness's timer did not cause is the agent's own exit."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(137, "", "")
    result = sbx.exec("import os; os.kill(os.getpid(), 9)", 30)
    assert (result.exit_code, result.timed_out, result.bound) == (137, False, None)


@pytest.mark.parametrize(
    ("fired", "gave_up", "code", "ran_s", "bound"),
    [
        (True, False, 137, 1.2, bounds.CELL_TIMER),
        (False, True, -1, 31.0, bounds.HOST_CALL),
        (False, False, 137, 2.0, bounds.CONTAINER_TIMEOUT),
        (False, False, 124, 2.5, bounds.CONTAINER_TIMEOUT),
        (False, False, 137, 1.99, None),  # the limit not yet reached: the cell ended itself
        (False, False, 1, 9.0, None),  # an ordinary failure, at any time
        (False, False, 0, 9.0, None),  # a cell that finished late
    ],
)
def test_o39_the_bound_that_ended_a_cell_is_named_from_what_fired_and_what_the_exit_and_the_time_show(
    fired, gave_up, code, ran_s, bound
):
    """O39: the timer's firing and the host giving up are bounds of their own; the container's `timeout` is one only for
    a kill's exit code after the whole of its limit (2 s here)."""
    assert bounds.named(fired=fired, gave_up=gave_up, exit_code=code, ran_s=ran_s, limit_s=2.0) == bound
