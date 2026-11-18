"""O56, O17, O34: a cell's bounds on the Apptainer backend, its time, its memory and its disk, and the one cause of a
kill, from the harness's own evidence on a fake node."""

import asyncio
import time

import pytest
from apptainer_support import make_settings, start
from cgroup_v1_support import OWN, v1_host
from fake_node import FakeNode, Unrecorded

from expcore.errors import InfraError
from expcore.plugin import NO_WAITS
from expcore_onedata.sandbox import bounds as BD
from expcore_onedata.sandbox import completion, kinds
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
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


def test_o72_a_cell_that_outlived_its_kill_is_told_what_it_did_and_the_next_call_ends_the_run(node, box):
    """O72, O34: through the code tool, as on Docker: the cell ran and hit its deadline, and a process of it outlived
    the kill, so the run is over. The call is the cell's own, a timeout with the output it had kept and the killed
    exec's check, never a raise in its place (the loop would record a call that ran as skipped); the next call is
    the one refused, as `cell_unkillable`, naming the deadline of the cell."""

    def stuck(cell):
        cell.procs[1].unkillable = True
        _until_dead(cell.procs[0])
        time.sleep(0.2)
        return Unrecorded(137, "partial output\n", "")

    node.cell_handler = stuck
    tool = CodeExecutionProvider(box)
    told = asyncio.run(tool.execute("python", {"code": "read_a_dead_mount()"}, timeout_s=1.2, ctx=NO_WAITS))
    assert (told.is_error, told.error_class) == (True, "timeout")
    assert told.text == "partial output\nthe code did not finish within the time limit and was stopped"
    assert told.exec_check == completion.UNKNOWN_FACTS
    assert box.metadata()["cell_deadlines"][0]["cell"] == 1
    with pytest.raises(InfraError) as refused:
        asyncio.run(tool.execute("python", {"code": "print('next')"}, timeout_s=30, ctx=NO_WAITS))
    assert refused.value.kind == "cell_unkillable"
    assert "cell 1's deadline" in refused.value.detail
    assert "sweep" not in refused.value.detail


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


def test_o72_a_memory_kill_is_the_cell_s_own_and_the_next_cell_runs(node, box):
    """O72: as a container's OOM kill on Docker, the watch's memory kill ends the cell, never the run: it is recorded
    with `scope: cell` (the code tool raises nothing for it), and the agent's next cell runs."""

    def greedy(cell):
        cell.procs[1].rss = 200 << 20
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = greedy
    killed = box.exec("x = bytearray(200 << 20)", 30)
    assert (killed.kill["reason"], killed.kill["scope"]) == ("memory_limit", "cell")
    assert killed.kill["watch_kills"][0]["scope"] == "cell"
    assert kinds.kill_error(killed.kill) is None
    node.cell_handler = lambda cell: CmdResult(0, "recovered\n", "")
    after = box.exec("print('recovered')", 30)
    assert (after.exit_code, after.stdout, after.kill) == (0, "recovered\n", None)


def test_o72_hidden_memory_no_one_cell_can_be_told_to_hold_ends_the_run_as_infrastructure(node, box, tmp_path):
    """O72: hidden memory past the margin while another run's cell shares the cgroup cannot be told this cell's: the
    cell is killed and the run ended with its reason (`scope: run`), infrastructure the code tool raises as
    `cell_not_isolated`; every later exec and the collection are refused, and the run's memory reading is unknown,
    never over."""
    node._new_proc(node.new_namespace(), "python3", environ=("EXPCORE_SANDBOX=other-run-ffff0000",))

    def hiding(cell):
        node._new_proc(cell.pidns, "python3", hidden=True, anon=768 << 20)
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = hiding
    result = box.exec("hide_and_grow()", 30)
    assert (result.kill["reason"], result.kill["scope"], result.kill["category"]) == (
        "memory_limit", "run", "infrastructure"
    )  # fmt: skip
    assert "another run's cell is charged to the same cgroup" in result.kill["basis"]
    assert kinds.kill_error(result.kill).kind == "cell_not_isolated"
    with pytest.raises(InfraError) as refused:
        box.exec("print('next')", 30)
    assert refused.value.kind == "cell_not_isolated"
    with pytest.raises(InfraError):
        box.collect(tmp_path / "out")
    assert box.metadata()["infrastructure_facts"]["memory"]["reading"] == "unknown"


def test_o72_a_run_ended_because_its_cell_could_not_be_isolated_says_so_in_its_infrastructure_facts(
    node, box, tmp_path
):
    """O72: the run-level fact a campaign counts such runs by: `infrastructure_facts.run_ended` names the scope `run`,
    `attributed: false`, and the kill's reason, what and why, and it is there after the collection was refused. A
    run that went on has no such fact."""
    assert "run_ended" not in box.metadata()["infrastructure_facts"]
    node._new_proc(node.new_namespace(), "python3", environ=("EXPCORE_SANDBOX=other-run-ffff0000",))

    def hiding(cell):
        node._new_proc(cell.pidns, "python3", hidden=True, anon=768 << 20)
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = hiding
    box.exec("hide_and_grow()", 30)
    with pytest.raises(InfraError):
        box.collect(tmp_path / "out")
    ended = box.metadata()["infrastructure_facts"]["run_ended"]
    assert set(ended) == {"scope", "attributed", "reason", "what", "why"}
    assert (ended["scope"], ended["attributed"]) == ("run", False)
    assert (ended["reason"], ended["what"]) == ("memory_limit", "memory_hidden")
    assert "another run's cell is charged to the same cgroup" in ended["why"]


def test_o72_a_cell_s_own_memory_kill_leaves_no_run_ended_fact(node, box):
    """O72: a memory kill that is the cell's own, with the run going on, is no `run_ended`: only a run the watch ended
    because it could not tell whose memory it was is counted as one."""

    def greedy(cell):
        cell.procs[1].rss = 200 << 20
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = greedy
    box.exec("x = bytearray(200 << 20)", 30)
    assert "run_ended" not in box.metadata()["infrastructure_facts"]


def test_o56_a_cell_whose_hidden_process_holds_memory_is_killed_as_over_its_limit(node, box):
    """O56: the HPC probe's E3: a cell process that makes itself non-dumpable and grows to 768 MiB is not in /proc,
    so the visible sum stays small; the cgroup charges it, and the run's record never reads it within its limit: the
    cell is killed as `memory_limit` (`memory_hidden`), its memory reading `over`, the run flagged as hiding."""

    def hiding(cell):
        node._new_proc(cell.pidns, "python3", hidden=True, anon=768 << 20, parent=cell.procs[1].pid)
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = hiding
    result = box.exec("import ctypes; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); x = bytearray(768 << 20)", 30)
    assert result.kill["reason"] == "memory_limit"
    assert result.kill["category"] == "agent"
    assert result.kill["watch_kills"][0]["what"] == "memory_hidden"
    assert "than any process /proc shows explains" in result.kill["basis"]
    assert box.metadata()["infrastructure_facts"]["memory"]["reading"] == "over"
    seen = box.process_visibility()
    assert (seen["memory"], seen["hidden_memory"], seen["hidden_process"]) == ("over", 1, 1)


def test_o56_a_hidden_process_no_visible_parent_names_is_still_killed_by_the_cgroup_s_check(node, box):
    """O56: a process forked twice under the cell's hidden init escapes the children signal; the cgroup's check does
    not depend on it."""

    def double_fork(cell):
        node._new_proc(cell.pidns, "python3", hidden=True, anon=768 << 20)
        _until_dead(cell.procs[1])
        return CmdResult(137, "", "")

    node.cell_handler = double_fork
    result = box.exec("x = 1", 30)
    assert result.kill["watch_kills"][0]["what"] == "memory_hidden"
    assert box.process_visibility()["hidden_process"] == 0


def test_o56_a_normal_cell_with_the_node_s_hidden_share_under_the_margin_is_untouched(node, box):
    """O56: Apptainer's own hidden starter adds memory during every cell; under the margin, it is no kill and the
    reading stays within."""

    def ordinary(cell):
        node.cgroup_slack = 40 << 20
        cell.procs[1].rss = 30 << 20
        time.sleep(0.2)
        node.cgroup_slack = 0
        return CmdResult(0, "done\n", "")

    node.cell_handler = ordinary
    result = box.exec("print('done')", 30)
    assert result.kill is None
    assert result.exit_code == 0
    assert box.metadata()["infrastructure_facts"]["memory"]["reading"] == "within"


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


def test_o56_an_oom_kill_a_cgroup_v1_step_counted_is_infrastructure_as_on_v2(node, box, tmp_path, monkeypatch):
    """O56: on a v1 host the node reads the step's memory cgroup from the memory controller's tree, where the kernel
    counts a kill in the cgroup of the cell's own process: a cell during which that count rises is `host_oom`, as on
    v2; one with no rise has no kill."""
    sys_cgroup = v1_host(tmp_path / "v1", monkeypatch)
    node.cgroup_memory = LocalNode().cgroup_memory  # the node's own reading, over the v1 tree
    control = sys_cgroup / "memory" / OWN.lstrip("/") / "memory.oom_control"

    assert box.exec("print('x')", 30).kill is None

    def oom(cell):
        control.write_text(control.read_text().replace("oom_kill 0", "oom_kill 1"))
        return CmdResult(137, "", "")

    node.cell_handler = oom
    result = box.exec("x = bytearray(1 << 40)", 30)
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


def test_o72_every_process_a_cell_left_counts_not_only_the_first_sixteen_recorded(node, box):
    """O72: whether the run goes on is judged on every process left after the kill, never only the 16 the record
    keeps: sixteen in D and a seventeenth still running end the run as `survived_kill`."""

    def stuck(cell):
        for n in range(17):
            proc = node._new_proc(cell.pidns, "reader", state="D" if n < 16 else "S", unkillable=True)
            node.cell_left.append(proc)
        return CmdResult(0, "")

    node.cell_handler = stuck
    box.exec("start_readers()", 30)
    assert box._cells.ended["outcome"] == DL.SURVIVED
    swept = [q for q in box.metadata()["quiesced"] if q.get("cell") == 1]
    assert (len(swept[0]["left"]), swept[0]["left_running"]) == (16, 1)


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
