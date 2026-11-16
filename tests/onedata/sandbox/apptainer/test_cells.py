"""O56, O17, O34: a cell's bounds on the Apptainer backend, its time, its memory and its disk, and the one cause of a
kill, from the harness's own evidence on a fake node."""

import time

import pytest
from apptainer_support import make_settings, start
from fake_node import FakeNode

from expcore.errors import InfraError
from expcore_onedata.sandbox import bounds as BD
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox.hostops import CmdResult


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def box(node, tmp_path):
    sandbox = start(node, make_settings(tmp_path))
    yield sandbox
    for proc in node.procs.values():
        proc.unkillable = False
    sandbox.stop(force=True)


def _until_dead(proc, seconds=10.0) -> None:
    until = time.monotonic() + seconds
    while proc.alive and time.monotonic() < until:
        time.sleep(0.01)


def test_a_cell_at_its_cap_is_killed_by_the_harness_s_timer_and_is_a_time_limit(node, box):
    """O56, O17: the timer fires at the cap and kills the cell's processes: `time_limit`, the agent's own, with the
    bound named; the deadline's record says every process ended."""

    def slow(cell):
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = slow
    result = box.exec("import time; time.sleep(60)", 1.2)
    assert result.timed_out
    assert result.bound == BD.CELL_TIMER
    assert result.kill["reason"] == "time_limit"
    assert result.kill["category"] == "agent"
    assert box.metadata()["cell_deadlines"][0]["outcome"] == DL.KILLED


def test_a_process_that_outlives_its_kill_ends_the_run_as_kill_survived(node, box):
    """O56, O34: a process no signal ends past the grace orders the sandbox's stop; the cell is `kill_survived` and
    every later exec is refused as `cell_unkillable`."""

    def stuck(cell):
        cell.procs[1].unkillable = True
        _until_dead(cell.procs[0])
        time.sleep(0.2)
        return CmdResult(137, "", "")

    node.cell_handler = stuck
    result = box.exec("read_a_dead_mount()", 1.2)
    assert result.kill["reason"] == "kill_survived"
    assert result.timed_out
    with pytest.raises(InfraError) as refused:
        box.exec("print('next')", 30)
    assert refused.value.kind == "cell_unkillable"


def test_a_cell_over_its_memory_limit_is_killed_by_the_watch_as_a_memory_limit(node, box):
    """O56: the watch sums the cell's resident memory and kills its processes over `memory_bytes`: `memory_limit`,
    the agent's own outcome, never a time limit."""

    def greedy(cell):
        cell.procs[1].rss = 200 << 20
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = greedy
    result = box.exec("x = bytearray(200 << 20)", 30)
    assert result.kill["reason"] == "memory_limit"
    assert result.kill["category"] == "agent"
    assert not result.timed_out
    assert result.kill["watch_kills"][0]["what"] == "memory"


def test_a_cell_that_fills_work_past_its_size_is_killed_by_the_watch_as_a_disk_limit(node, box):
    """O56: /work is a directory on the node's tmpfs; past `work_disk_bytes` the watch kills the run's processes."""

    def filler(cell):
        (cell.source("/work") / "big.bin").write_bytes(b"\0" * (5 << 20))
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = filler
    result = box.exec("open('big.bin','wb').write(bytes(5 << 20))", 30)
    assert result.kill["reason"] == "disk_limit"
    assert result.kill["watch_kills"][0]["what"] == "work"


def test_an_oom_kill_the_step_s_cgroup_counted_is_infrastructure(node, box):
    """O56: the step's memory cgroup is the harness's and the gate's too, so an OOM kill it counted during the cell
    is `host_oom`, never the agent's own limit."""

    def oom(cell):
        node.cgroup["events"]["oom_kill"] += 1
        return CmdResult(137, "", "")

    node.cell_handler = oom
    result = box.exec("print('x')", 30)
    assert result.kill["reason"] == "host_oom"
    assert result.kill["category"] == "infrastructure"


def test_an_exit_137_with_no_evidence_of_a_kill_is_the_cell_s_own_exit(node, box):
    """O56, O17: an exit code alone never makes a kill."""
    node.cell_handler = lambda cell: CmdResult(137, "", "")
    result = box.exec("import os; os.kill(os.getpid(), 9)", 30)
    assert result.exit_code == 137
    assert result.kill is None
    assert not result.timed_out


def test_a_worker_a_cell_left_running_is_killed_at_the_cell_s_end(node, box):
    """O56: whatever of a cell is left when its exec returns is killed and shown gone, and counted on its record."""

    def detach(cell):
        node.cell_left.append(cell.procs[1])
        return CmdResult(0, "")

    node.cell_handler = detach
    box.exec("start_a_daemon()", 30)
    assert not node.cell_left[0].alive
    assert box._cells.cells[-1]["left_after_exec"] == 1


def test_an_oom_of_the_step_s_cgroup_comes_before_the_watch_s_kill(node, box):
    """O56, O17: infrastructure first: a cell the watch killed while the shared cgroup counted an OOM kill is
    `host_oom`, never the agent's `memory_limit`."""

    def both(cell):
        node.cgroup["events"]["oom_kill"] += 1
        cell.procs[1].rss = 200 << 20
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = both
    result = box.exec("x = bytearray(200 << 20)", 30)
    assert result.kill["reason"] == "host_oom"
    assert result.kill["watch_kills"]
