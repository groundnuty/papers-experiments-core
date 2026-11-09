"""O17: how a cell of the agent's code is said to have been killed, from the evidence the host keeps (a fake host)."""

import json
import threading
import time
from pathlib import Path

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.cells import CellTimer
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

DOCKER_REFUSED = "Error response from daemon: container agent is not running"
DATA = Path(__file__).resolve().parents[2] / "data" / "sandbox"
FIXTURE_CID = "ab12" * 16  # the container id the kernel-log fixtures name


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
    result = sbx.exec("while True: pass", 2.5)  # enough that a second is left to start it after preparing it
    assert result.exit_code == 137  # the cell ran, and the harness's kill ended it
    assert result.timed_out is True
    assert (result.kill["reason"], result.kill["category"], result.kill["timer_fired"]) == ("time_limit", "agent", True)
    assert kinds.kill_error(result.kill) is None  # the agent's own outcome


def test_o17_a_cell_left_too_little_time_is_not_started_and_no_timer_fired(world, sbx):
    """O17: a one-second limit with 10 ms of it spent preparing leaves less than the second a cell needs: the cell is
    not started, a status of its own, with no timer fired, no timeout and no kill, and nothing is run; what the run's
    time means is the loop's own rule."""
    cells = sbx._cells
    uptime = cells._obs.uptime

    def slow_uptime():  # the preparation takes 10 ms of the cell's second
        time.sleep(0.01)
        return uptime()

    cells._obs.uptime = slow_uptime
    execs = len([c for c in world.host.calls if c[:2] == ("docker", "exec")])
    result = sbx.exec("print(1)", 1.0)
    assert (result.timed_out, result.kill) == (False, None)
    assert getattr(result, "status", None) == "not_started"
    assert len([c for c in world.host.calls if c[:2] == ("docker", "exec")]) == execs  # nothing was run
    assert cells.cells[-1]["status"] == "not_started"
    assert "timer_fired" not in json.dumps(cells.cells[-1])
    assert "cell_kills" not in sbx.metadata()


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
    timer = CellTimer(60, lambda: kills.append(1))
    timer.start()
    assert timer.end() is False
    timer._fire()  # a firing that raced the cell's end: it finds the cell ended and does nothing
    assert kills == []
    fired = CellTimer(0.0, lambda: kills.append(2))
    fired.start()
    deadline = time.monotonic() + 10
    while not kills and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fired.end() is True
    assert kills == [2]


@pytest.mark.parametrize("code", [137, 143, -9, 124, 1])
def test_o17_an_exit_code_alone_is_the_agents_own_exit(world, sbx, code):
    """O17: a cell that exits 137 by itself (`sys.exit(137)`), with the counters unchanged and the container running,
    was not killed: 137 and 143 are treated alike, as any other code, and none of them is a kill or a time limit."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(code, "", "")
    result = sbx.exec("import sys; sys.exit(137)", 30)
    assert (result.exit_code, result.kill, result.timed_out) == (code, None, False)
    assert "cell_kills" not in sbx.metadata()


@pytest.mark.parametrize("code", [137, 143])
def test_o17_137_and_143_with_the_containers_evidence_are_the_same_kill(world, sbx, code):
    """O17: with the container's evidence (it stopped under the cell) both codes are the same kill, from it."""

    def dies(container, argv, stdin):
        container.running = False
        return CmdResult(code, "", "")

    world.host.exec_handler = dies
    result = sbx.exec("print(1)", 30)
    assert (result.kill["reason"], result.kill["category"]) == ("container_dead", "infrastructure")
    assert result.timed_out is False
    agent_of(world, sbx).running = True  # so that the teardown runs as usual


EVIDENCE = {  # the counters that rise under the cell, the kernel log of its window, and the cause they make
    "own_limit": (("oom", "oom_kill"), "dmesg_own_limit.txt", ("memory_limit", "agent"), (1, 0)),
    "host_killer": (("oom_kill",), "dmesg_host_killer.txt", ("host_oom", "infrastructure"), (0, 1)),
}


@pytest.mark.parametrize("evidence", sorted(EVIDENCE))
def test_o17_identical_kernel_evidence_with_different_exit_codes_classifies_the_same(world, sbx, evidence):
    """O17: whole cells that exit 137, 143, 124, 1 and 0, each with the same rise of the cgroup's counters and the same
    kernel line naming this container, get one and the same cause: the exit code changes nothing."""
    rising, log, cause, lines = EVIDENCE[evidence]
    agent = agent_of(world, sbx)
    events_file = next(path for path in world.host.files if agent.id in path and path.endswith("/memory.events"))
    world.host.dmesg = (DATA / log).read_text(encoding="utf-8").replace(FIXTURE_CID, agent.id)
    counters = dict.fromkeys(("low", "high", "max", "oom", "oom_kill"), 0)

    def exits_with(code):
        def handler(container, argv, stdin):
            for key in rising:
                counters[key] += 1
            world.host.files[events_file] = "".join(f"{key} {value}\n" for key, value in counters.items())
            return CmdResult(code, "", "")

        return handler

    kills = []
    for code in (137, 143, 124, 1, 0):
        world.host.exec_handler = exits_with(code)
        result = sbx.exec("x = bytearray(1 << 40)", 30)
        assert result.exit_code == code
        kills.append(result.kill)
    assert {(kill["reason"], kill["category"]) for kill in kills} == {cause}
    assert len({kill["basis"] for kill in kills}) == 1
    assert {(kill["kernel_memcg_own"], kill["kernel_host"]) for kill in kills} == {lines}  # each saw the kernel's line
    assert all(kill["events_delta"] == kills[0]["events_delta"] for kill in kills)
