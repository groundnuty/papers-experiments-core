"""O56: what `/proc` hides from the run's watch is checked from outside: the cgroup's anonymous memory against the
visible processes', and a child of a cell process that `/proc` does not show (the HPC probes of 16 Nov 2026: a
non-dumpable process held 768 MiB while the watch saw 12.7 MB, and the step's cgroup counted a hidden 512 MiB)."""

import pytest
from fake_node import CGROUP, OWN_NS, FakeNode

from expcore.visibility.instrument import readable
from expcore_onedata.sandbox.apptainer.hidden import HIDDEN_CONFIRM
from expcore_onedata.sandbox.apptainer.watch import Space, Watch

TAG = "EXPCORE_SANDBOX=run-1-abcd1234"
MIB = 1 << 20
MARGIN = 100 * MIB


def _watch(node, tmp_path, *, limit=4096 * MIB, margin=MARGIN):
    (tmp_path / "work").mkdir(exist_ok=True)
    spaces = (Space("work", tmp_path / "work", 1 << 30),)
    return Watch(node, run_tag=TAG, own_pidns=OWN_NS, memory_bytes=limit, spaces=spaces, memory_poll_s=0.02,
                 disk_poll_s=3600.0, hidden_margin_bytes=margin)  # fmt: skip


@pytest.fixture
def node() -> FakeNode:
    node = FakeNode()
    node.wait_for_watch = False
    node._new_proc(OWN_NS, "python3", role="helper", rss=300 * MIB)  # a helper: visible, its memory explained
    return node


def _cell(node, *, rss=10 * MIB):
    """A cell's visible processes: `timeout` (tagged) and the agent's interpreter under it."""
    ns = node.new_namespace()
    top = node._new_proc(ns, "timeout", environ=(TAG,), rss=MIB)
    agent = node._new_proc(ns, "python3", rss=rss)
    return ns, top, agent


def _polls(watch, n=HIDDEN_CONFIRM):
    for _ in range(n):
        watch.poll()


def test_o56_memory_a_hidden_process_holds_kills_the_cell_as_over_its_limit(node, tmp_path):
    """O56: a non-dumpable process is not in /proc, but the cgroup charges its memory: grown past the margin for two
    polls, it is taken as past the limit, and the run's processes are killed as `memory_limit` (`memory_hidden`); the
    hidden process ends with its namespace."""
    watch = _watch(node, tmp_path)
    watch.poll()  # no cell yet: the baseline
    watch.begin_cell(1, "n")
    ns, top, agent = _cell(node)
    hidden = node._new_proc(ns, "python3", hidden=True, anon=768 * MIB)  # forked twice: no visible parent names it
    watch.poll()
    assert top.alive, "one poll over the margin is not enough"
    watch.poll()
    assert not top.alive
    assert not agent.alive
    assert not hidden.alive
    found = watch.end_cell()
    kill = found["kills"][0]
    assert (kill["reason"], kill["what"], kill["cell"]) == ("memory_limit", "memory_hidden", 1)
    assert kill["amount"] >= 768 * MIB
    assert kill["limit"] == MARGIN
    assert kill["figures"]["cgroup_anon_bytes"] - kill["figures"]["visible_anon_bytes"] == 768 * MIB
    assert found["memory"]["reading"] == "over"
    reading = watch.reading()
    assert reading["reading"] == "over"
    assert reading["hidden_memory_cells"] == 1
    assert reading["peak"]["unexplained_bytes"] == 768 * MIB


def test_o56_a_normal_run_whose_cgroup_matches_its_visible_processes_is_within(node, tmp_path):
    """O56: the helpers' and the cell's memory is all visible, so nothing is unexplained: no kill, and the reading is
    `within`, with its figures."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _ns, top, _agent = _cell(node, rss=900 * MIB)
    _polls(watch, 5)
    assert top.alive
    found = watch.end_cell()
    assert found["kills"] == []
    assert found["memory"] == {"reading": "within", "unknown": [], "hidden_processes": 0, "unexplained_peak_bytes": 0}
    reading = watch.reading()
    assert (reading["reading"], reading["unknown"], reading["margin_bytes"]) == ("within", [], MARGIN)
    assert watch.summary()["memory"] == reading


def test_o56_the_node_s_own_hidden_share_under_the_margin_is_no_kill(node, tmp_path):
    """O56: Apptainer's setuid starter and the cell's init are hidden too: what they add, under the margin, is no
    agent's."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _ns, top, _agent = _cell(node)
    node.cgroup_slack = 60 * MIB
    _polls(watch, 5)
    assert top.alive
    assert watch.end_cell()["memory"]["reading"] == "within"


def test_o56_a_growth_past_the_margin_for_one_poll_alone_is_no_kill(node, tmp_path):
    """O56: the cgroup and /proc are read at different moments: a process that allocates or ends between them is
    seen once, and only a growth that lasts `HIDDEN_CONFIRM` polls counts."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _ns, top, _agent = _cell(node)
    for slack in (300 * MIB, 0, 300 * MIB, 0):
        node.cgroup_slack = slack
        watch.poll()
    assert top.alive
    assert watch.end_cell()["kills"] == []


def test_o56_the_margin_is_a_setting(node, tmp_path):
    """O56: the same hidden 200 MiB is under a margin of 256 MiB and past one of 128 MiB."""
    for margin, killed in ((256 * MIB, False), (128 * MIB, True)):
        watch = _watch(node, tmp_path, margin=margin)
        watch.poll()
        watch.begin_cell(1, "n")
        ns, top, _agent = _cell(node)
        node._new_proc(ns, "python3", hidden=True, anon=200 * MIB)
        _polls(watch, 3)
        assert top.alive is not killed
        watch.end_cell()
        for proc in node.procs.values():
            if proc.pidns == ns:
                proc.alive = False


def test_o56_memory_unexplained_before_the_cell_is_its_baseline(node, tmp_path):
    """O56: memory nothing visible explains while no cell runs is no cell's (the kernel's batching, say): a cell is
    judged by what it adds."""
    node.cgroup_slack = 300 * MIB
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _ns, top, _agent = _cell(node)
    _polls(watch, 5)
    assert top.alive
    assert watch.end_cell()["memory"]["reading"] == "within"


def test_o56_hidden_memory_with_another_run_s_cell_in_the_cgroup_is_unknown_never_a_kill(node, tmp_path):
    """O56: with another run's cell charged to the same cgroup, the memory cannot be told whose it is: the reading is
    `unknown` (`unattributed`), and nothing is killed."""
    watch = _watch(node, tmp_path)
    watch.poll()
    other = node.new_namespace()
    node._new_proc(other, "python3", environ=("EXPCORE_SANDBOX=run-2-ffff0000",))
    watch.begin_cell(1, "n")
    ns, top, _agent = _cell(node)
    node._new_proc(ns, "python3", hidden=True, anon=768 * MIB)
    _polls(watch, 4)
    assert top.alive
    found = watch.end_cell()
    assert found["kills"] == []
    assert found["memory"]["reading"] == "unknown"
    assert found["memory"]["unknown"] == ["unattributed"]
    assert watch.reading()["reading"] == "unknown"


def test_o56_a_start_cell_past_the_margin_makes_the_reading_unknown_and_never_kills(node, tmp_path):
    """O56: the start's own cells calibrate the check: one whose hidden share passed the margin means this node's
    share cannot be told from an agent's, so a later growth is never a kill, and the reading is unknown."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "canary", calibration=True)
    _ns, canary, canary_agent = _cell(node)
    node.cgroup_slack = 150 * MIB
    _polls(watch, 3)
    assert canary.alive, "a calibrating cell is the harness's own code: never killed"
    watch.end_cell()
    canary.alive = canary_agent.alive = False
    node.cgroup_slack = 0
    watch.poll()
    watch.begin_cell(2, "n")
    ns, top, _agent = _cell(node)
    node._new_proc(ns, "python3", hidden=True, anon=768 * MIB)
    _polls(watch, 3)
    assert top.alive
    found = watch.end_cell()
    assert found["memory"]["unknown"] == ["margin_exceeded_at_start"]
    reading = watch.reading()
    assert reading["reading"] == "unknown"
    assert reading["start_unexplained_bytes"] == 150 * MIB


def test_o56_a_child_proc_does_not_show_is_a_hidden_process_and_the_reading_unknown(node, tmp_path):
    """O56: probe F's signal: a visible cell process's children list a pid /proc does not show. It is recorded, never
    a kill, and the run's memory reading is unknown even when the hidden memory is under the margin."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    ns, top, agent = _cell(node)
    node._new_proc(ns, "python3", hidden=True, anon=4 * MIB, parent=agent.pid)
    _polls(watch, 3)
    assert top.alive
    found = watch.end_cell()
    assert found["memory"]["reading"] == "unknown"
    assert found["memory"]["unknown"] == ["hidden_process"]
    assert found["memory"]["hidden_processes"] == 1
    assert watch.visibility()["hidden_process"] == 1


def test_o56_children_that_cannot_be_read_make_the_reading_unknown(node, tmp_path):
    node.children_unreadable = True
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _cell(node)
    watch.poll()
    assert watch.end_cell()["memory"]["unknown"] == ["children_unreadable"]


def test_o56_without_a_cgroup_v2_the_reading_is_unknown(node, tmp_path):
    """O56: on a v1 or hybrid host there is no cgroup to compare with: the reading is unknown, never within."""
    node.cgroup_path = None
    watch = _watch(node, tmp_path)
    assert watch.reading()["unknown"] == ["cgroup_unreadable"]
    watch.begin_cell(1, "n")
    _cell(node)
    watch.poll()
    assert watch.end_cell()["memory"]["reading"] == "unknown"


def test_o56_a_cgroup_reading_that_fails_during_a_cell_makes_it_unknown(node, tmp_path):
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _cell(node)
    node.cgroup_unreadable = True
    watch.poll()
    found = watch.end_cell()
    assert found["memory"]["unknown"] == ["cgroup_unreadable"]
    assert watch.reading()["cgroup_unreadable_polls"] == 1


def test_o56_a_proc_listing_that_fails_during_a_cell_makes_the_reading_unknown(node, tmp_path):
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    _cell(node)
    node.proc_unreadable = True
    watch.poll()
    assert watch.end_cell()["memory"]["unknown"] == ["proc_unreadable"]


def test_o56_a_kill_for_hidden_memory_that_reaches_no_process_leaves_the_reading_unknown(node, tmp_path):
    """O56: hidden memory past the margin with no visible process of the cell left to kill: nothing was bounded, so
    the reading is unknown, never within."""
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    ns, top, agent = _cell(node)
    watch.poll()  # the cell's namespace is found
    top.alive = agent.alive = False
    node._new_proc(ns, "python3", hidden=True, anon=768 * MIB)
    _polls(watch, 3)
    found = watch.end_cell()
    assert found["kills"] == []
    assert found["memory"]["unknown"] == ["kill_reached_none"]


def test_o56_memory_outside_the_driver_s_cgroup_is_neither_counted_nor_explained(node, tmp_path):
    """O56: a process of the uid in another cgroup (another job step) is charged elsewhere: its memory is not taken
    to explain this cgroup's."""
    node._new_proc(OWN_NS, "python3", rss=2048 * MIB, cgroup=CGROUP.replace("step_0", "step_1"))
    watch = _watch(node, tmp_path)
    watch.poll()
    watch.begin_cell(1, "n")
    ns, top, _agent = _cell(node)
    node._new_proc(ns, "python3", hidden=True, anon=768 * MIB)
    _polls(watch, 2)
    assert not top.alive


def test_o56_every_reading_is_one_the_core_s_instrument_reads(node, tmp_path):
    """O56: the watch's `visibility()` is the `process_visibility` instrument's input, within, over and unknown."""
    watch = _watch(node, tmp_path)
    watch.poll()
    assert readable(watch.visibility())["memory"] == "within"
    watch.begin_cell(1, "n")
    ns, _top, agent = _cell(node)
    node._new_proc(ns, "python3", hidden=True, anon=768 * MIB, parent=agent.pid)
    _polls(watch, 2)
    watch.end_cell()
    seen = readable(watch.visibility())
    assert (seen["memory"], seen["hidden_memory"], seen["hidden_process"]) == ("over", 1, 1)
    node.cgroup_path = None
    assert readable(_watch(node, tmp_path).visibility())["memory"] == "unknown"
