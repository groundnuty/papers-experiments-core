"""O97: the reader and the attribution agree. A fake `/proc` is read by the Apptainer tracker (`stuckread`) and the
reading goes through the deadline into `attribution.attributer`, so a disagreement between the two on a key or a
syscall number, which would fail every run closed while both sides' own tests pass, is caught here. What the record
keeps is checked against the record's schema."""

from types import SimpleNamespace

import pytest
from deadline_support import Clock, Processes
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, WORK_MOUNT, FakeProcReader, syscall_text

from expcore.records.cell_deadline import problem
from expcore_onedata import fault_owner
from expcore_onedata.sandbox import attribution, deadline
from expcore_onedata.sandbox.apptainer import stuck
from expcore_onedata.sandbox.apptainer.cells import _RunProcesses
from expcore_onedata.sandbox.apptainer.stuckread import Diagnosis

KEY = "task=task-a|model=m1|seed=1"
ROLES = stuck.Roles("/data", device=DATA_DEVICE)
HOLDERS = {"orchard": "providers[1]", "meadow": "providers[2]"}
READ_FD3 = syscall_text(0, 3, 0x7F10, 0x1000)
ACCESS = syscall_text(21, 0x7F20, 4)
OUTSIDE = "-1 0x7ffd1000 0x7f110000\n"


@pytest.fixture(autouse=True)
def empty_registry():
    fault_owner.reset_held()
    yield
    fault_owner.reset_held()


def survivor(view: FakeProcReader, *, call: str = READ_FD3, target: str = "/data/orchard/a.bin", mnt_id=DATA_MOUNT):
    """`view`, with one process in D whose task is in `call`, holding the file `target`."""
    process = view.process(7, start="7").task(7, syscall=call, wchan="request_wait_answer")
    process.fd(3, target, mnt_id=mnt_id)
    return view


def verdict_of(view: FakeProcReader, held=("providers[1]",)) -> dict:
    """The record of a deadline whose one process outlives its kill until the abort, read from `view` and judged by
    the attribution with `held` held down; the abort frees it."""
    procs = Processes({7: "python3"}, wedged=(7,))
    tracker = _RunProcesses(
        SimpleNamespace(node=SimpleNamespace(proc_reader=lambda: view), diagnosis=Diagnosis(lambda: ROLES))
    )
    procs.diagnose = tracker.diagnose  # type: ignore[attr-defined]
    clock = Clock()
    esc = deadline.Escalation(
        grace_s=3.0,
        abort=lambda timeout_s: (procs.release(), {"ok": True})[-1],
        sleep=clock.sleep,
        clock=clock,
        restorable=True,
        attribute=attribution.attributer(lambda: frozenset(held), HOLDERS),
    )
    return deadline.enforce(procs, esc)


def test_o97_a_reader_blocked_in_read_on_a_held_down_spaces_file_is_the_agents():
    """O97: read(2) of a file in the space whose holder is held down: verdict agent, restore pending."""
    record = verdict_of(survivor(FakeProcReader()))
    assert record["attribution"] == {
        "verdict": "agent",
        "why": "blocked_on_held_down_holder",
        "target": "providers[1]",
        "space_holder": "providers[1]",
    }
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert record["restore"] == deadline.RESTORE_PENDING


def test_o97_the_same_read_on_another_space_is_infrastructure():
    """O97: the file is in a space whose holder is up: space_not_held_down, and the run is set aside."""
    record = verdict_of(survivor(FakeProcReader(), target="/data/meadow/a.bin"))
    assert record["attribution"]["verdict"] == "infrastructure"
    assert record["attribution"]["why"] == "space_not_held_down"
    assert "restore" not in record


def test_o97_a_read_on_the_work_directory_is_infrastructure():
    """O97: a wait on the cell's own work directory is not the data mount."""
    record = verdict_of(survivor(FakeProcReader(), target="/work/out.bin", mnt_id=WORK_MOUNT))
    assert record["attribution"]["why"] == "not_data_mount"


def test_o97_an_access_style_call_the_lists_do_not_name_is_infrastructure():
    """O97: access(2) may wait on a path the process does not hold, so what it holds decides nothing: unknown."""
    view = survivor(FakeProcReader(), call=ACCESS)
    record = verdict_of(view)
    assert record["attribution"]["verdict"] == "infrastructure"
    assert record["attribution"]["why"] == "blocking_object_unknown"


def test_o97_a_path_call_is_placed_by_its_argument_and_is_the_agents_on_a_held_down_space():
    """O97: openat(2) of a path in the held-down space, read from the task's memory (G2): agent."""
    view = FakeProcReader()
    view.process(7, start="7").task(7, syscall=syscall_text(257, 0xFFFFFFFFFFFFFF9C, 0x7F20)).mem_at(
        7, 0x7F20, "/data/orchard/dir/b.txt\0"
    )
    assert verdict_of(view)["attribution"]["verdict"] == "agent"


def test_o97_a_path_call_into_another_space_is_infrastructure():
    """O97: openat(2) of a path in a space whose holder is up."""
    view = FakeProcReader()
    view.process(7, start="7").task(7, syscall=syscall_text(257, 0xFFFFFFFFFFFFFF9C, 0x7F20)).mem_at(
        7, 0x7F20, "/data/meadow/b.txt\0"
    )
    assert verdict_of(view)["attribution"]["why"] == "space_not_held_down"


def test_o97_a_page_fault_is_the_agents_when_everything_the_process_holds_is_in_the_held_down_space():
    """O97: a task blocked outside any syscall is read by its holdings."""
    view = FakeProcReader()
    process = view.process(7, start="7").task(7, syscall=OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).maps("/data/orchard/lib.so")
    assert verdict_of(view)["attribution"]["verdict"] == "agent"


def test_o97_a_page_fault_with_one_holding_in_another_space_is_infrastructure():
    """O97: one object in a space whose holder is up makes the whole infrastructure."""
    view = FakeProcReader()
    process = view.process(7, start="7").task(7, syscall=OUTSIDE)
    process.fd(3, "/data/orchard/a", mnt_id=DATA_MOUNT).fd(4, "/data/meadow/b", mnt_id=DATA_MOUNT)
    assert verdict_of(view)["attribution"]["why"] == "space_not_held_down"


def test_o97_a_survivor_that_is_not_in_d_is_infrastructure():
    """O97: a sleeping task is not blocked in the kernel on the mount."""
    view = FakeProcReader()
    view.process(7, start="7", state="S").task(7, state="S", syscall=READ_FD3).fd(
        3, "/data/orchard/a", mnt_id=DATA_MOUNT
    )
    assert verdict_of(view)["attribution"]["why"] == "not_in_D"


def test_o97_with_no_injection_in_effect_the_same_read_is_infrastructure():
    """O97: nothing held down: no injection in effect."""
    assert verdict_of(survivor(FakeProcReader()), held=())["attribution"]["why"] == "no_injection_in_effect"


def test_o97_a_survivor_the_reader_could_not_read_is_infrastructure():
    """O97: a process `/proc` hides is unknown, never one that waits on the held-down space."""
    view = survivor(FakeProcReader())
    view.processes[7].hidden = True
    assert verdict_of(view)["attribution"]["why"] == "blocking_object_unknown"


def test_o97_the_held_down_set_is_read_when_the_verdict_is_asked_for():
    """O97: a registry that holds the provider by the time of the verdict makes the read the agent's; one released
    before it does not: the closure never takes the set when the sandbox started."""
    holders = attribution.attributer(fault_owner.held_reader(KEY), HOLDERS)
    view = survivor(FakeProcReader())
    reading = {"reading": _read(view)}
    assert holders(reading)["why"] == "no_injection_in_effect"
    fault_owner.hold(KEY, "providers[1]", attempt=None)
    assert holders(reading)["verdict"] == "agent"
    fault_owner.release(KEY, attempt=None)
    assert holders(reading)["why"] == "no_injection_in_effect"


def _read(view: FakeProcReader):
    from expcore_onedata.sandbox.apptainer.stuckread import diagnose

    return diagnose(view, [{"pid": 7, "start": "7", "comm": "python3", "state": "D"}], 5.0, ROLES, machine="x86_64")[
        deadline.READING
    ]


@pytest.mark.parametrize(
    "view",
    [
        survivor(FakeProcReader()),
        survivor(FakeProcReader(), call=ACCESS),
        survivor(FakeProcReader(), target="/work/out.bin", mnt_id=WORK_MOUNT),
    ],
)
def test_o97_what_the_record_keeps_of_the_survivors_is_a_record_the_schema_accepts(view):
    """O97: the recorded deadline (capped survivors, their blocking, the verdict) validates against the record's
    schema, and holds no pid, no start and no reading."""
    record = verdict_of(view)
    assert problem(record) is None
    assert "reading" not in record
    assert "pid" not in repr(record["survivors"])
    assert "start" not in repr(record["survivors"])


def test_o97_a_deadline_with_many_survivors_and_tasks_still_validates():
    """O97: ten processes of six tasks each are cut to what a record may hold and still validate."""
    view = FakeProcReader()
    for pid in range(100, 110):
        process = view.process(pid, start=str(pid))
        for tid in range(pid * 10, pid * 10 + 6):
            process.task(tid, syscall=READ_FD3)
        process.fd(3, f"/data/orchard/{'x' * 200}", mnt_id=DATA_MOUNT)
    procs = Processes(dict.fromkeys(range(100, 110), "python3"), wedged=tuple(range(100, 110)))
    tracker = _RunProcesses(
        SimpleNamespace(node=SimpleNamespace(proc_reader=lambda: view), diagnosis=Diagnosis(lambda: ROLES))
    )
    procs.diagnose = tracker.diagnose  # type: ignore[attr-defined]
    clock = Clock()
    esc = deadline.Escalation(
        grace_s=3.0, abort=lambda t: (procs.release(), {"ok": True})[-1], sleep=clock.sleep, clock=clock,
        restorable=True, attribute=attribution.attributer(lambda: frozenset({"providers[1]"}), HOLDERS),
    )  # fmt: skip
    record = deadline.enforce(procs, esc)
    assert problem(record) is None
    assert len(record["survivors"]) == 8
    assert record["survivors_cut"] == 2
