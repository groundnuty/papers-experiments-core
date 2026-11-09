"""O17: how a cell of the agent's code is said to have been killed, from the evidence the host keeps (a fake host)."""

import threading
import time

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.cells import CellTimer
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

DOCKER_REFUSED = "Error response from daemon: container agent is not running"


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    sandbox.stop()


def agent_of(world, sandbox):
    return world.host.containers[sandbox.names.agent]


def test_o17_a_cell_the_harnesss_timer_ended_is_a_time_limit(world, sbx):
    """O17: the harness's own timer fires at the cell's limit, records that it fired and kills the cell; that record,
    and nothing else, makes the cell's cause a time limit."""

    cells, killing = sbx._cells, threading.Event()
    kill = cells._kill_at_the_cap

    def the_harness_kills():  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        killing.set()
        kill()

    def runs_until_killed(container, argv, stdin):
        assert killing.wait(10), "the harness's timer did not fire"
        return CmdResult(137, "", "")

    cells._kill_at_the_cap = the_harness_kills
    world.host.exec_handler = runs_until_killed
    result = sbx.exec("while True: pass", 1.2)  # the least that starts a cell: a second is left after preparing it
    assert result.exit_code == 137  # the cell ran, and the harness's kill ended it
    assert result.timed_out is True
    assert (result.kill["reason"], result.kill["category"], result.kill["timer_fired"]) == ("time_limit", "agent", True)
    assert kinds.kill_error(result.kill) is None  # the agent's own outcome


@pytest.mark.parametrize(("stopped", "kind"), [(True, "sandbox"), (False, "unattributed_kill")])
def test_o17_a_docker_failure_without_the_timers_flag_is_never_a_time_limit(world, sbx, stopped, kind):
    """O17: docker exec failing while the harness's timer has not fired is no time limit: infrastructure when the
    container is gone, a kill nothing attributes when it still runs."""

    def docker_fails(container, argv, stdin):
        if stopped:
            container.running = False
        return CmdResult(126, "", DOCKER_REFUSED)

    world.host.exec_handler = docker_fails
    result = sbx.exec("print(1)", 30)
    assert result.timed_out is False
    assert result.kill["timer_fired"] is False
    assert result.kill["reason"] != "time_limit"
    assert kinds.kill_error(result.kill).kind == kind
    agent_of(world, sbx).running = True  # so that the teardown runs as usual


def test_o17_a_timer_that_the_cell_outran_never_fires_and_never_kills():
    """O17: the cell's end and the timer's firing are decided under one lock: a cell that ended first has no fired
    timer, and the timer does nothing afterwards."""
    kills = []
    timer = CellTimer(0.05, lambda: kills.append(1))
    timer.start()
    assert timer.end() is False
    time.sleep(0.1)
    assert kills == []
    fired = CellTimer(0.0, lambda: kills.append(2))
    fired.start()
    time.sleep(0.05)
    assert fired.end() is True
    assert kills == [2]
