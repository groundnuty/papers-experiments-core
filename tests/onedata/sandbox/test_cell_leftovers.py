"""O72: a Docker cell's processes end with it, as an Apptainer cell's do: the code tool tells the agent that state does
not carry between calls, so whatever a cell leaves running is killed and shown gone once its `docker exec` returns."""

import time

import pytest
from deadline_support import cell_processes, killed_pids
from sandbox_support import make_spec, start_options

from expcore.errors import InfraError
from expcore_onedata.sandbox import deadline, teardown
from expcore_onedata.sandbox.codetool import DEFAULT_DESCRIPTION
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox


@pytest.fixture
def sbx(world):
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    yield sandbox
    world.host.unkillable.clear()
    sandbox.stop(force=True)


def test_o72_the_code_tool_promises_that_state_does_not_carry_between_calls():
    """O72: the promise both backends keep, in the words the model is given by default."""
    assert "State does not carry between calls" in DEFAULT_DESCRIPTION


def test_o72_a_worker_a_docker_cell_detached_is_killed_when_its_exec_returns(world, sbx):
    """O72: a worker in a session of its own, outside `timeout`'s group, is killed and shown gone at the cell's end,
    with the rest of what the cell left; the container's init and its `sleep` are spared, and the count is on the
    cell's record and in the quiesce records."""
    pids = cell_processes(world, sbx)
    result = sbx.exec("start_a_worker()", 30)
    assert result.kill is None
    assert {pids["python"], pids["child"], pids["grandchild"]} <= killed_pids(world)
    assert pids["sleep"] not in killed_pids(world)
    assert sbx._cells.cells[-1]["left_after_exec"] == 3
    swept = [q for q in sbx.metadata()["quiesced"] if q.get("cell") == 1]
    assert [(q["reason"], q["killed"]) for q in swept] == [("end of cell", 3)]


def test_o72_a_docker_cell_that_leaves_nothing_kills_nothing(world, sbx):
    """O72: a cell whose processes all ended with it is swept to no effect, and nothing is added to the record."""
    sbx.exec("print(1)", 30)
    assert sbx._cells.cells[-1]["left_after_exec"] == 0
    assert not [q for q in sbx.metadata().get("quiesced", []) if q.get("cell")]


def test_o72_a_leftover_that_no_kill_ends_ends_the_run_as_survived_kill(world, sbx):
    """O72, O34: a process the cell left that is not shown gone after its kill would be running in the next cell, so
    the run is ended as `survived_kill`, and every later cell is refused as `cell_unkillable`."""
    pids = cell_processes(world, sbx)
    world.host.unkillable.add(pids["grandchild"])
    sbx.exec("start_a_worker()", 30)
    assert sbx._cells.cells[-1]["left_after_exec"] is None
    assert sbx._cells.ended["outcome"] == deadline.SURVIVED
    with pytest.raises(InfraError) as refused:
        sbx.exec("print('next')", 30)
    assert refused.value.kind == "cell_unkillable"


def test_o72_the_sweep_s_wait_is_the_harness_s_never_the_cell_s_time(world, sbx):
    """O72: the sweep runs after the cell's duration is taken, so a leftover it waits for (up to 10 s, here one no kill
    ends) never lengthens the cell's recorded time, and a harness step's exit is never read as its backstop's."""
    pids = cell_processes(world, sbx)
    world.host.unkillable.add(pids["grandchild"])
    began = time.monotonic()
    result = sbx.exec("start_a_worker()", 30)
    assert time.monotonic() - began >= 5  # the sweep waited for the survivor
    assert result.duration_s < 5
    assert sbx._cells.cells[-1]["duration_s"] < 5


def test_o72_a_leftover_in_d_whose_kill_failed_ends_the_run(world, sbx):
    """O72: a process in D takes a pending SIGKILL on its next return, but only one the kill reached: when the kill
    command failed, a leftover in D is not known to be signalled, so the run ends as `survived_kill`."""
    agent = world.host.containers[sbx.names.agent]
    world.host.add_stat(7010, "reader", agent.pid, state="D")
    world.host.add_cgroup(agent, [agent.pid, agent.init_child, 7010])
    world.host.run_handlers["kill"] = lambda argv: CmdResult(1, "", "sudo: a password is required")
    sbx.exec("start_a_reader()", 30)
    del world.host.run_handlers["kill"]
    assert sbx._cells.ended["outcome"] == deadline.SURVIVED


@pytest.mark.parametrize("exit_code", [0, 3])
def test_o72_a_cell_that_ran_keeps_its_own_result_when_the_sweep_cannot_end_a_leftover(
    world, sbx, monkeypatch, exit_code
):
    """O72: the cell's exec returned, with its exit status, its output and its completion record; then the sweep could
    not end a worker it left. The cell's result is what the cell did and stays exactly that: no kill, no timeout, the
    real status and streams, the exec's check. The run's ending is separate and names the sweep: `run_ended` in the
    metadata (the sweep, the cell it followed, how it ended and why), the same words in every later exec's error."""
    wait_gone = teardown._wait_gone  # the sweep's wait for a survivor (10 s), shortened: this one never goes
    monkeypatch.setattr(teardown, "_wait_gone", lambda host, procs, seconds, poll: wait_gone(host, procs, 0.3, 0.05))
    pids = cell_processes(world, sbx)
    world.host.unkillable.add(pids["grandchild"])
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(exit_code, "worker started\n", "a warning\n")
    result = sbx.exec("start_a_worker()", 30)
    assert (result.exit_code, result.stdout, result.stderr) == (exit_code, "worker started\n", "a warning\n")
    assert (result.timed_out, result.kill, result.status) == (False, None, "ran")
    assert result.exec_check == {"source_whole": True, "exit_status": exit_code, "nonce_matched": True}
    cell = sbx._cells.cells[-1]
    assert (cell["cell"], cell["status"], cell["exit_code"], cell["timed_out"]) == (1, "ran", exit_code, False)
    assert cell["exec_check"] == result.exec_check
    assert "kill" not in cell
    assert cell["left_after_exec"] is None  # unknown: the sweep could not show them gone
    ended = sbx.metadata()["run_ended"]
    assert (ended["by"], ended["cell"], ended["outcome"]) == ("end_of_cell_sweep", 1, deadline.SURVIVED)
    assert "outlived its exec and its kill" in ended["why"]
    (swept,) = [q for q in sbx.metadata()["quiesced"] if q.get("cell") == 1]
    assert (swept["reason"], swept["left_running"]) == ("end of cell", 1)
    with pytest.raises(InfraError) as refused:
        sbx.exec("print('next')", 30)
    assert refused.value.kind == "cell_unkillable"
    assert "end-of-cell sweep" in refused.value.detail
    assert "cell 1" in refused.value.detail


def test_o72_every_leftover_counts_not_only_the_first_sixteen_recorded(world, sbx):
    """O72: the quiesce's record keeps 16 leftovers, but whether the run goes on is judged on all of them: sixteen in
    D and a seventeenth still running end the run."""
    agent = world.host.containers[sbx.names.agent]
    pids = list(range(7100, 7117))
    for pid in pids[:16]:
        world.host.add_stat(pid, "reader", agent.pid, state="D")
    world.host.add_stat(pids[16], "worker", agent.pid)
    world.host.add_cgroup(agent, [agent.pid, agent.init_child, *pids])
    world.host.unkillable.update(pids)
    sbx.exec("start_readers()", 30)
    assert sbx._cells.ended["outcome"] == deadline.SURVIVED
    swept = [q for q in sbx.metadata()["quiesced"] if q.get("cell") == 1]
    assert (len(swept[0]["left"]), swept[0]["left_running"]) == (16, 1)
