"""O70: the watch keeps the run's pids, cpus and the gate's memory on a fake node, and the record says which readings
were shown: a reading is never within, pinned or set when it was not."""

import signal
import threading
import time

import pytest
from fake_node import OWN_NS, FakeNode

from expcore_onedata.sandbox.apptainer import limits as L
from expcore_onedata.sandbox.apptainer.node import Spawned
from expcore_onedata.sandbox.apptainer.watch import Space, Watch

TAG = "EXPCORE_SANDBOX=run-1-abcd1234"
STEP = [0, 1, 2, 3]
NOT_HIDDEN = {"hidden_processes": 0, "hidden_memory_cells": 0}


def _check(gate=None, **more):
    values = {"cpus": 2.0, "pids": 64, "nofile": 64, "gate_memory_bytes": 8 << 20, "has_gate": False}
    check = L.LimitsCheck(**{**values, **more})
    check.begin(STEP, gate)
    return check


def _watch(node, tmp_path, check, limit=1 << 30):
    (tmp_path / "work").mkdir(exist_ok=True)
    spaces = (Space("work", tmp_path / "work", 1 << 20),)
    return Watch(node, run_tag=TAG, own_pidns=OWN_NS, memory_bytes=limit, spaces=spaces, memory_poll_s=0.02,
                 disk_poll_s=0.0, limits=check)  # fmt: skip


@pytest.fixture
def node() -> FakeNode:
    node = FakeNode()
    node.wait_for_watch = False
    return node


def test_o70_a_run_over_its_pids_limit_has_its_processes_killed_as_a_pids_limit(node, tmp_path):
    """O70: the watch sums the tasks of the run's processes; over `pids` it kills them all as `pids_limit`, scope
    cell, and the record reads `over` with the kill counted and the largest count seen."""
    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), threads=40) for _ in range(2)]
    check = _check()
    watch = _watch(node, tmp_path, check)
    watch.begin_cell(2, "n")
    watch.poll()
    assert not any(p.alive for p in procs)
    kill = watch.end_cell()["kills"][0]
    assert (kill["reason"], kill["what"], kill["amount"], kill["limit"], kill["scope"]) == (
        "pids_limit", "pids", 80, 64, "cell"
    )  # fmt: skip
    pids = watch.limits_record()["pids"]
    assert (pids["declared"], pids["reading"], pids["max_seen"], pids["kills"]) == (64, "over", 80, 1)


def test_o70_a_poll_that_kills_for_pids_leaves_the_memory_reading_unknown_never_within(node, tmp_path):
    """O70: the kill for `pids` returns before the hidden-memory check (the cgroup's assessment and the scan of the
    cell's children), so that poll did not read the cell's memory: with a hidden child a visible parent names, the
    run's memory reading was `within` with no hidden process. It is `unknown` with `hidden_unchecked`; the pids
    reading, which the kill reached, stays `over`."""
    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), threads=40) for _ in range(2)]
    node._new_proc(ns, "python3", hidden=True, anon=4 << 20, parent=procs[0].pid)
    watch = _watch(node, tmp_path, _check())
    watch.begin_cell(2, "n")
    watch.poll()
    found = watch.end_cell()
    assert [k["reason"] for k in found["kills"]] == ["pids_limit"]
    assert (found["memory"]["reading"], found["memory"]["unknown"]) == ("unknown", ["hidden_unchecked"])
    assert watch.visibility()["memory"] == "unknown"
    assert watch.limits_record()["pids"]["reading"] == "over"


def test_o70_a_cell_that_ends_while_the_watch_logs_its_pids_kill_keeps_the_unknown_reading(node, tmp_path, monkeypatch):
    """O70: the memory reading is marked in the step that records the kill, against the cell it killed: a cell that
    ends and another that begins while the watch logs the kill neither loses the mark nor lends it to the next."""
    from expcore_onedata.sandbox.apptainer import watch as W

    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), threads=40) for _ in range(2)]
    node._new_proc(ns, "python3", hidden=True, anon=4 << 20, parent=procs[0].pid)
    watch = _watch(node, tmp_path, _check())
    watch.begin_cell(1, "n")
    first: list = []

    def next_cell(*args, **kwargs):  # the exec thread: the cell ended, another began, while the watch logs
        first.append(watch.end_cell())
        watch.begin_cell(2, "m")

    monkeypatch.setattr(W.log, "info", next_cell)
    watch.poll()
    assert first[0]["memory"]["unknown"] == ["hidden_unchecked"]
    assert watch.end_cell()["memory"]["unknown"] == []


def test_o70_a_poll_that_does_not_kill_for_pids_still_reads_the_cell_s_memory(node, tmp_path):
    """O70: the control of the case above: the same cell under its `pids` limit has its hidden child found, so its
    memory reading is `unknown` for `hidden_process` and not for a poll that did not look."""
    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), threads=1) for _ in range(2)]
    node._new_proc(ns, "python3", hidden=True, anon=4 << 20, parent=procs[0].pid)
    watch = _watch(node, tmp_path, _check())
    watch.begin_cell(2, "n")
    watch.poll()
    found = watch.end_cell()
    assert found["kills"] == []
    assert (found["memory"]["reading"], found["memory"]["unknown"]) == ("unknown", ["hidden_process"])


def test_o70_the_tasks_at_the_limit_are_within_it(node, tmp_path):
    """O70: a count equal to `pids` is allowed (Docker refuses the fork past it); the reading is within with the
    count it showed."""
    ns = node.new_namespace()
    procs = [node._new_proc(ns, "python3", environ=(TAG,), threads=32) for _ in range(2)]
    watch = _watch(node, tmp_path, _check())
    watch.begin_cell(1, "n")
    watch.poll()
    assert all(p.alive for p in procs)
    pids = watch.limits_record()["pids"]
    assert (pids["reading"], pids["unknown"], pids["max_seen"], pids["kills"]) == ("within", [], 64, 0)


def test_o70_a_process_whose_task_count_cannot_be_read_leaves_pids_unknown_never_within(node, tmp_path):
    """O70: a status with no `Threads:` line is unknown, not 0 tasks."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,), threads=None)
    watch = _watch(node, tmp_path, _check())
    watch.begin_cell(1, "n")
    watch.poll()
    pids = watch.limits_record()["pids"]
    assert (pids["reading"], pids["max_seen"]) == ("unknown", None)
    assert "proc_unreadable" in pids["unknown"]


def test_o70_a_pids_reading_with_no_process_ever_counted_is_unknown(node, tmp_path):
    """O70: no poll with a process of the run in it shows nothing: unknown, with the count null."""
    watch = _watch(node, tmp_path, _check())
    watch.poll()
    pids = watch.limits_record()["pids"]
    assert (pids["reading"], pids["max_seen"]) == ("unknown", None)
    assert pids["unknown"] == ["proc_unreadable"]


def test_o70_a_poll_that_cannot_list_proc_during_a_cell_leaves_pids_and_cpus_unknown(node, tmp_path):
    """O70: a poll nothing was read at is no sample: the readings that held until then are unknown for good."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,), threads=3, cores=frozenset({0, 1}))
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    watch = _watch(node, tmp_path, check)
    watch.begin_cell(1, "n")
    watch.poll()
    assert (watch.limits_record()["pids"]["reading"], watch.limits_record()["cpus"]["reading"]) == ("within", "pinned")
    node.proc_unreadable = True
    watch.poll()
    found = watch.limits_record()
    assert found["pids"]["reading"] == found["cpus"]["reading"] == "unknown"
    assert "proc_unreadable" in found["pids"]["unknown"]
    assert "proc_unreadable" in found["cpus"]["unknown"]


def test_o70_a_pids_kill_that_signalled_no_process_is_never_over():
    """O70: a kill that reached nothing bounded nothing: the reading is unknown with `kill_reached_none`."""
    check = _check()
    check.max_tasks = 99
    kill = {"reason": "pids_limit", "signalled": 0}
    pids = check.record(kills=[kill], gate_kills=[], memory=NOT_HIDDEN)["pids"]
    assert (pids["reading"], pids["kills"], pids["unknown"]) == ("unknown", 0, ["kill_reached_none"])


@pytest.mark.parametrize("memory", [{"hidden_processes": 1}, {"hidden_memory_cells": 1}])
def test_o70_a_run_that_hid_a_process_from_proc_reads_pids_and_cpus_unknown(memory):
    """O70: /proc hides a non-dumpable process's tasks and cores, so a run whose memory reading holds a hidden
    process (or a cell killed for hidden memory) was not shown within its pids or pinned to its cores."""
    check = _check()
    check.max_tasks = 3
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    found = check.record(kills=[], gate_kills=[], memory={**NOT_HIDDEN, **memory})
    assert (found["pids"]["reading"], found["pids"]["unknown"]) == ("unknown", ["hidden_process"])
    assert (found["cpus"]["reading"], found["cpus"]["unknown"]) == ("unknown", ["hidden_process"])


def test_o70_a_process_on_cores_outside_the_effective_set_is_recorded_as_widened_and_never_killed(node, tmp_path):
    """O70: affinity is a set the agent's own code can widen within the step's cores: the watch reads the cores of every
    task of every process of the run and records one outside the effective set, never killing it."""
    ns = node.new_namespace()
    inside = node._new_proc(ns, "python3", environ=(TAG,), cores=frozenset({0, 1}))
    wide = node._new_proc(ns, "python3", environ=(TAG,), cores=frozenset({0, 1, 2}))
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    watch = _watch(node, tmp_path, check)
    watch.begin_cell(1, "n")
    watch.poll()
    watch.poll()
    assert inside.alive
    assert wide.alive
    cpus = watch.limits_record()["cpus"]
    assert (cpus["reading"], cpus["widened"], cpus["widened_processes"]) == ("widened", True, 1)


def test_o70_processes_within_the_effective_set_read_pinned_with_the_sets_shown(node, tmp_path):
    """O70: every task inside the canary's set, the step's cores read, no process hidden: pinned, with the effective
    set, its count (the first `ceil(cpus)` cores) and the step's set."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,), cores=frozenset({0, 1}))
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    watch = _watch(node, tmp_path, check)
    watch.begin_cell(1, "n")
    watch.poll()
    cpus = watch.limits_record()["cpus"]
    assert cpus == {"declared": 2.0, "reading": "pinned", "unknown": [], "effective_set": [0, 1], "effective_count": 2,
                    "step_set": STEP, "widened": False, "widened_processes": 0}  # fmt: skip


def test_o70_a_task_whose_cores_cannot_be_read_is_unknown_never_pinned(node, tmp_path):
    """O70: a `Cpus_allowed_list` that cannot be read shows nothing of that task."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,))
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    node.affinity_unreadable = True
    watch = _watch(node, tmp_path, check)
    watch.begin_cell(1, "n")
    watch.poll()
    cpus = watch.limits_record()["cpus"]
    assert (cpus["reading"], cpus["unknown"]) == ("unknown", ["affinity_unreadable"])


def test_o70_before_the_canary_a_process_is_judged_against_the_pin(node, tmp_path):
    """O70: the canary's own cell runs before its answer is known, so its processes are judged against the pin the
    cell was started with; a process wider than it is recorded."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,), cores=frozenset({0, 1, 2, 3}))
    watch = _watch(node, tmp_path, _check())  # the pin is cores 0 and 1
    watch.begin_cell(1, "n", calibration=True)
    watch.poll()
    assert watch.limits_record()["cpus"]["widened_processes"] == 1


def test_o70_a_canary_set_wider_than_the_pin_reads_widened_whatever_the_watch_saw():
    """O70: a starter that gave the cell more cores than `taskset` asked for leaves the effective set wider than the
    pin: widened, with the set the cell really had."""
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1, 2, 3})))
    cpus = check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["cpus"]
    assert (cpus["reading"], cpus["widened"], cpus["effective_count"]) == ("widened", True, 4)


def test_o70_a_step_set_nobody_read_leaves_cpus_unknown_with_that_reason():
    """O70: no step set, no pin, so nothing to judge a process against: unknown, step_set null."""
    check = L.LimitsCheck(cpus=2.0, pids=64, nofile=64, gate_memory_bytes=1, has_gate=False)
    check.begin(None, None)
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    cpus = check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["cpus"]
    assert (cpus["reading"], cpus["step_set"], cpus["unknown"]) == ("unknown", None, ["step_set_unreadable"])


def test_o70_the_nofile_reading_is_set_only_when_the_canary_showed_the_declared_hard_limit():
    """O70: `set` needs the cell's own reading of its hard limit to be the declared one."""
    check = _check()
    assert check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["nofile"] == {
        "declared": 64, "reading": "unknown", "unknown": ["probe_unreadable"], "hard_limit": None
    }  # fmt: skip
    check.note_probe(L.Probe(64, 64, None))
    assert check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["nofile"] == {
        "declared": 64, "reading": "set", "unknown": [], "hard_limit": 64
    }  # fmt: skip
    check.note_probe(L.Probe(1024, 1024, None))
    other = check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["nofile"]
    assert (other["reading"], other["hard_limit"]) == ("unknown", 1024)


def test_o70_a_gate_over_its_memory_limit_is_killed_through_its_pidfd_and_recorded(node, tmp_path):
    """O70: the watch reads the gate's RssAnon and RssShmem (the gate is a process of the driver's own namespace, named
    by its pid and start time) and over `gate_memory_bytes` signals SIGKILL to that process alone."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=32 << 20)
    check = _check(has_gate=True, gate=Spawned(gate.pid, gate.start))
    watch = _watch(node, tmp_path, check)
    watch.poll()
    assert not gate.alive
    assert (gate.pid, signal.SIGKILL) in node.signals
    kill = watch.gate_kill()
    assert (kill["amount"], kill["limit"], kill["signalled"]) == (32 << 20, 8 << 20, True)
    assert watch.limits_record()["gate_memory"] == {
        "declared": 8 << 20, "reading": "over", "unknown": [], "peak_bytes": 32 << 20
    }  # fmt: skip


def test_o70_a_gate_within_its_limit_reads_within_with_its_peak(node, tmp_path):
    """O70: a gate the watch read at every poll, under the limit: within, with the largest it was."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=4 << 20)
    watch = _watch(node, tmp_path, _check(has_gate=True, gate=Spawned(gate.pid, gate.start)))
    watch.poll()
    gate.rss = 6 << 20
    watch.poll()
    gate.rss = 5 << 20
    watch.poll()
    assert watch.limits_record()["gate_memory"] == {
        "declared": 8 << 20, "reading": "within", "unknown": [], "peak_bytes": 6 << 20
    }  # fmt: skip
    assert gate.alive


def test_o70_a_gate_the_listing_does_not_show_is_unknown_never_within(node, tmp_path):
    """O70: a gate nothing could read the memory of (alive, not in the listing) is unknown with `gate_unreadable`."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=None)
    watch = _watch(node, tmp_path, _check(has_gate=True, gate=Spawned(gate.pid, gate.start)))
    watch.poll()
    found = watch.limits_record()["gate_memory"]
    assert (found["reading"], found["unknown"], found["peak_bytes"]) == ("unknown", ["gate_unreadable"], None)


def test_o70_a_gate_that_ended_is_no_unreadable_reading(node, tmp_path):
    """O70: a gate gone (the collection stopped it) stops being watched: the readings it gave stand."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=4 << 20)
    watch = _watch(node, tmp_path, _check(has_gate=True, gate=Spawned(gate.pid, gate.start)))
    watch.poll()
    node.signal(gate.pid, gate.start, signal.SIGKILL)
    watch.poll()
    watch.poll()
    found = watch.limits_record()["gate_memory"]
    assert (found["reading"], found["unknown"], found["peak_bytes"]) == ("within", [], 4 << 20)


def test_o70_a_gate_that_ended_but_is_not_reaped_yet_is_no_unreadable_reading(node, tmp_path):
    """O70: a zombie gate has a row and no memory to read; the node says it is gone, so the watch stops."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=4 << 20)
    watch = _watch(node, tmp_path, _check(has_gate=True, gate=Spawned(gate.pid, gate.start)))
    watch.poll()
    gate.rss, gate.state = None, "Z"
    node.alive = lambda pid, start: False
    watch.poll()
    found = watch.limits_record()["gate_memory"]
    assert (found["reading"], found["unknown"], found["peak_bytes"]) == ("within", [], 4 << 20)


def test_o70_a_gate_kill_that_reached_no_process_is_never_over(node, tmp_path):
    """O70: the watch's signal that found nothing to signal bounded nothing: unknown with `kill_reached_none`."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=32 << 20)
    check = _check(has_gate=True, gate=Spawned(gate.pid, gate.start))
    watch = _watch(node, tmp_path, check)
    node.signal = lambda pid, start, sig: None  # the node cannot tell whether it signalled
    watch.poll()
    found = watch.limits_record()["gate_memory"]
    assert (found["reading"], found["unknown"]) == ("unknown", ["kill_reached_none"])
    assert watch.gate_kill() is None


def test_o70_a_run_with_no_gate_has_no_gate_memory_to_bound():
    """O70: no REST data path, no gate: `not_applicable`, never within."""
    found = _check().record(kills=[], gate_kills=[], memory=NOT_HIDDEN)["gate_memory"]
    assert found == {"declared": 8 << 20, "reading": "not_applicable", "unknown": [], "peak_bytes": None}


def test_o70_every_key_of_the_record_is_present_before_anything_was_read():
    """O70: the record's shape is fixed: every key, a value not read null, and an `unknown` reading with a reason."""
    check = L.LimitsCheck(cpus=2.0, pids=64, nofile=64, gate_memory_bytes=8 << 20, has_gate=True)
    found = check.record(kills=[], gate_kills=[], memory=NOT_HIDDEN)
    assert found["version"] == 1
    assert set(found["cpus"]) == {"declared", "reading", "unknown", "effective_set", "effective_count", "step_set",
                                  "widened", "widened_processes"}  # fmt: skip
    assert set(found["pids"]) == {"declared", "reading", "unknown", "max_seen", "kills"}
    assert set(found["nofile"]) == {"declared", "reading", "unknown", "hard_limit"}
    assert set(found["gate_memory"]) == {"declared", "reading", "unknown", "peak_bytes"}
    for name in ("cpus", "pids", "nofile", "gate_memory"):
        assert found[name]["reading"] == "unknown"
        assert found[name]["unknown"], name
    assert found["cpus"]["effective_set"] is None
    assert found["cpus"]["effective_count"] is None


def test_o70_a_poll_that_kills_for_memory_still_records_the_tasks_and_cores_it_saw(node, tmp_path):
    """O70: a poll that ends in a kill read the run's tasks and cores first, so the record never keeps the canary's
    `within` and `pinned` for a cell that was over its pids and outside its cores in the poll that killed it."""
    ns = node.new_namespace()
    node._new_proc(ns, "python3", environ=(TAG,), rss=200 << 20, threads=40, cores=frozenset({0, 1, 2}))
    check = _check()
    check.note_probe(L.Probe(64, 64, frozenset({0, 1})))
    watch = _watch(node, tmp_path, check, limit=100 << 20)
    watch.begin_cell(1, "n")
    watch.poll()
    assert watch.end_cell()["kills"][0]["reason"] == "memory_limit"
    found = watch.limits_record()
    assert found["pids"]["max_seen"] == 40
    assert (found["cpus"]["reading"], found["cpus"]["widened_processes"]) == ("widened", 1)


def test_o70_a_poll_over_both_memory_and_pids_is_over_for_both(node, tmp_path):
    """O70: one SIGKILL ends the cell for both reasons: the memory kill first (the cell's cause), the pids kill beside
    it with the processes the same signal reached, and each reading is `over`."""
    ns = node.new_namespace()
    for _ in range(2):
        node._new_proc(ns, "python3", environ=(TAG,), rss=60 << 20, threads=40)
    watch = _watch(node, tmp_path, _check(), limit=100 << 20)
    watch.begin_cell(1, "n")
    watch.poll()
    kills = watch.end_cell()["kills"]
    assert [k["reason"] for k in kills] == ["memory_limit", "pids_limit"]
    assert kills[1]["signalled"] == kills[0]["signalled"] == 2
    pids = watch.limits_record()["pids"]
    assert (pids["reading"], pids["kills"], pids["max_seen"]) == ("over", 1, 80)
    assert watch.reading()["reading"] == "over"


def test_o70_the_gate_s_kill_is_published_before_a_collection_can_read_the_gate_dead(node, tmp_path):
    """O70: the collection reads the watch's gate kill after seeing the gate dead; the signal and the record are one
    step to it, so it never finds the gate dead and no kill (which would leave the death `undetermined`)."""
    gate = node._new_proc(OWN_NS, "python3", role="helper", rss=32 << 20)
    watch = _watch(node, tmp_path, _check(has_gate=True, gate=Spawned(gate.pid, gate.start)))
    asked: list = []
    signal_now = node.signal

    def racing(pid, start, sig):
        sent = signal_now(pid, start, sig)
        reader = threading.Thread(target=lambda: asked.append(watch.gate_kill()))
        reader.start()
        racing.reader = reader
        time.sleep(0.2)  # the window between the signal and the record
        return sent

    node.signal = racing
    watch.poll()
    racing.reader.join(5)
    assert asked[0] is not None
    assert asked[0]["signalled"] is True


def test_o70_the_two_kills_of_a_poll_over_memory_and_pids_are_of_the_cell_that_was_killed(node, tmp_path, monkeypatch):
    """O70: both records are made in one step against the cell the signal ended: a cell that ends and another that
    begins while the watch logs the kill is never given the pids kill of the first."""
    from expcore_onedata.sandbox.apptainer import watch as W

    ns = node.new_namespace()
    for _ in range(2):
        node._new_proc(ns, "python3", environ=(TAG,), rss=60 << 20, threads=40)
    watch = _watch(node, tmp_path, _check(), limit=100 << 20)
    watch.begin_cell(1, "n")

    def next_cell(*args, **kwargs):  # the exec thread: the cell ended, another began, while the watch logs
        watch.end_cell()
        watch.begin_cell(2, "m")

    monkeypatch.setattr(W.log, "info", next_cell)
    watch.poll()
    assert [(k["reason"], k["cell"]) for k in watch.kills] == [("memory_limit", 1), ("pids_limit", 1)]
    assert watch.end_cell()["kills"] == []
