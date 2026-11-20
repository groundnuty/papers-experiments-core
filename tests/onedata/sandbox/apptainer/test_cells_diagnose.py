"""The Apptainer deadline's tracker reads its survivors from the node's `/proc` at the `survived` step, before the
mount is aborted, and hands what it read to the escalation's attribution (O97)."""

import dataclasses
from types import SimpleNamespace

from deadline_support import Clock, Processes, escalation
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, FakeProcReader, syscall_text

from expcore_onedata.sandbox import deadline
from expcore_onedata.sandbox.apptainer import stuck
from expcore_onedata.sandbox.apptainer.cells import _RunProcesses
from expcore_onedata.sandbox.apptainer.stuckread import Diagnosis

ROLES = stuck.Roles("/data", device=DATA_DEVICE)
VERDICT = {"verdict": "infrastructure", "why": "space_not_held_down", "target": None, "space_holder": None}


def wedged_reader(pid: int = 7):
    """A reader in D, blocked in read(2) of a data file, on a fake `/proc`."""
    view = FakeProcReader()
    process = view.process(pid, start=str(pid))
    process.task(pid, syscall=syscall_text(0, 3, 0x7F10, 0x1000), wchan="request_wait_answer")
    process.fd(3, "/data/orchard/a.bin", mnt_id=DATA_MOUNT)
    return view


def tracker_over(view, procs: Processes, *, diagnosis, node=None):
    """The real Apptainer tracker's `diagnose`, on top of the fake processes' listing and kills."""
    node = node if node is not None else SimpleNamespace(proc_reader=lambda: view)
    tracker = _RunProcesses(SimpleNamespace(node=node, diagnosis=diagnosis))
    procs.diagnose = tracker.diagnose  # type: ignore[attr-defined]
    return procs


def test_o97_the_survivors_are_diagnosed_before_the_mount_is_aborted():
    """O97: the diagnosis is read at the `survived` step, so the abort that ends their wait has not happened yet."""
    clock, order = Clock(), []
    procs = tracker_over(wedged_reader(), Processes({7: "python3"}, wedged=(7,)), diagnosis=Diagnosis(lambda: ROLES))
    esc, _ = escalation(clock, abort=lambda t: (order.append("abort"), procs.release(), {"ok": True})[-1])
    read = procs.diagnose
    procs.diagnose = lambda left, t: (order.append("diagnose"), read(left, t))[-1]
    deadline.enforce(procs, esc)
    assert order == ["diagnose", "abort"]


def test_o97_what_was_read_reaches_the_attribution_and_the_record():
    """O97: `attribute` is given the uncapped `blocks`, and the record keeps the survivors the tracker read."""
    clock, seen = Clock(), []
    procs = tracker_over(wedged_reader(), Processes({7: "python3"}, wedged=(7,)), diagnosis=Diagnosis(lambda: ROLES))
    esc, _ = escalation(clock, abort=lambda t: (procs.release(), {"ok": True})[-1])
    esc = dataclasses.replace(esc, attribute=lambda found: (seen.append(found), VERDICT)[-1])
    record = deadline.enforce(procs, esc)
    assert seen[0]["blocks"] == [
        {"state": "D", "tasks": [{"state": "D", "objects": [{"mount": "data", "space": "orchard"}]}]}
    ]
    assert record["survivors"][0]["waits_on"][0]["space"] == "orchard"
    assert record["survivors"][0]["tasks"][0]["wchan"] == "request_wait_answer"
    assert record["attribution"] == VERDICT


def test_o97_a_run_given_no_diagnosis_is_not_read_so_nothing_is_attributed_to_the_agent():
    """O97: with no targets given, the tracker reads nothing: `attribute` sees no diagnostics."""
    clock, seen = Clock(), []
    procs = tracker_over(wedged_reader(), Processes({7: "python3"}, wedged=(7,)), diagnosis=None)
    esc, _ = escalation(clock, abort=lambda t: (procs.release(), {"ok": True})[-1])
    esc = dataclasses.replace(esc, attribute=lambda found: (seen.append(found), VERDICT)[-1])
    record = deadline.enforce(procs, esc)
    assert seen == [None]
    assert "survivors" not in record


def test_o97_a_node_with_no_proc_reader_is_not_diagnosed():
    """O97: a node that has no `proc_reader` (a fake, an old node) gives no diagnosis, and the deadline goes on."""
    clock, seen = Clock(), []
    procs = Processes({7: "python3"}, wedged=(7,))
    tracker_over(wedged_reader(), procs, diagnosis=Diagnosis(lambda: ROLES), node=SimpleNamespace())
    esc, _ = escalation(clock, abort=lambda t: (procs.release(), {"ok": True})[-1])
    esc = dataclasses.replace(esc, attribute=lambda found: (seen.append(found), VERDICT)[-1])
    record = deadline.enforce(procs, esc)
    assert seen == [None]
    assert record["outcome"] == deadline.MOUNT_ABORTED


def test_o97_the_run_asked_for_its_targets_when_the_deadline_fires():
    """O97: the cell's targets (the run's connection, a new device after a restore) are read when asked, not before."""
    clock, asked = Clock(), []
    diagnosis = Diagnosis(lambda: (asked.append("roles"), ROLES)[-1])
    procs = tracker_over(wedged_reader(), Processes({7: "python3"}, wedged=(7,)), diagnosis=diagnosis)
    assert asked == []
    esc, _ = escalation(clock, abort=lambda t: (procs.release(), {"ok": True})[-1])
    deadline.enforce(procs, esc)
    assert asked == ["roles"]


def test_o97_the_local_node_gives_its_own_proc_to_read():
    """O97: `LocalNode.proc_reader()` is a reader of the node's own `/proc`."""
    from expcore_onedata.sandbox.apptainer.node import LocalNode
    from expcore_onedata.sandbox.apptainer.stuckread import LocalProcReader

    assert isinstance(LocalNode().proc_reader(), LocalProcReader)
