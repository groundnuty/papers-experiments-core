"""O97: the Docker backend reads what a cell's survivors wait on, through the host's privilege, and the verdict made of
it is the same one an Apptainer run's is.

A reader in a thread other than the main one leaves a zombie leader and a thread in uninterruptible sleep, waiting on a
read of a file on the data mount. These tests run the real escalation (`deadline`), the real reader (`sudoproc`, the
root half `procsnap.py` played on a tree made of the fake host's processes), the shared reading (`stuckread`) and the
real verdict (`attribution`), so what the reader returns is judged by the code that must accept it: the verdict is
`agent` only for a survivor in D, whose blocking object is known, on the data mount and in a space whose holder the
run's own injection holds down, and every other shape is infrastructure with its reason. The abort that frees the
survivor is played by the sidecar's handler, as the other deadline tests do."""

import dataclasses
import threading

import pytest
from deadline_support import cell_processes, run_to_the_cap
from fake_host import FakeHost
from sandbox_support import make_spec, start_options

from expcore.records import cell_deadline
from expcore_onedata.sandbox import attribution, kinds
from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox.apptainer import stuckread
from expcore_onedata.sandbox.calldeadline import cell_deadline_of
from expcore_onedata.sandbox.dockerdiag import DockerDiagnosis
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.sudoproc import SudoProcReader

THREAD = 7010
HOLDERS = {"space-a": "providers[1]"}
ROOT = "1 0 8:1 / / ro - overlay overlay ro\n"
READ_FD_3 = "0 0x3 0x7f00 0x400 0x0 0x0 0x0 0x7ffd 0x7f11\n"  # read(3, ...)


@pytest.fixture(autouse=True)
def x86_64(monkeypatch):
    """The syscall tables are the x86_64 ones, whatever machine the tests run on (`stuck.X86_64`)."""
    monkeypatch.setattr(stuckread.platform, "machine", lambda: "x86_64")


def share_device(host: FakeHost) -> str:
    """The device of the run's FUSE mount in the host's table now, as the mount's own table in a container shows it."""
    return next(line.split()[2] for line in host.mountinfo().splitlines() if "fuse.oneclient" in line)


def start(world, *, held=("providers[1]",), holders=HOLDERS) -> DockerSandbox:
    """A sandbox of the mount data path: its run's injection holds `held` down, and `holders` hold its spaces."""
    spec = dataclasses.replace(
        make_spec(world, "mounted"), space_holders=dict(holders), held_down=lambda: frozenset(held)
    )
    world.host.ns_mountinfo = True
    return DockerSandbox.start(world.host, world.s, spec, **start_options(world))


def wedge(
    world, sbx, *, syscall=READ_FD_3, fd="/data/space-a/file", mount="data", thread_state="D", zombie=True, links=None
):
    """The cell's python is a zombie leader with a thread that waits in a read of descriptor 3 (a file at `fd`, on the
    mount `mount`: the data mount, or `/work`); a thread in `thread_state` (with `zombie` False the process is not a
    zombie, and its one thread is its main one, in `thread_state`). The process's mount table, root, working
    directory and descriptors are the live thread's, as the kernel shows them there. Unkillable until the sidecar's
    abort frees it. Returns the cell's pids."""
    host = world.host
    pids = cell_processes(world, sbx)
    leader = pids["python"]
    thread = THREAD if zombie else leader
    host.add_stat(leader, "python", 6999, state="Z" if zombie else thread_state)
    host.add_threads(
        host.containers[sbx.names.agent],
        leader,
        {leader: "Z", THREAD: thread_state} if zombie else {leader: thread_state},
    )
    host.unkillable.add(leader)
    base = f"/proc/{leader}/task/{thread}"
    host.files[f"{base}/wchan"] = "request_wait_answer"
    if syscall is not None:
        host.files[f"{base}/syscall"] = syscall
    on_data = f"60 25 {share_device(host)} / /data ro - fuse.oneclient s ro\n"
    on_work = "61 25 8:2 / /work rw - ext4 /dev/vdb rw\n"
    host.files[f"{base}/mountinfo"] = ROOT + on_data + on_work
    host.links[f"{base}/root"] = "/"
    host.links[f"{base}/cwd"] = "/work"
    host.links[f"{base}/fd/3"] = fd
    host.files[f"{base}/fdinfo/3"] = f"pos:\t0\nflags:\t0100000\nmnt_id:\t{60 if mount == 'data' else 61}\n"
    for name, target in (links or {}).items():
        host.links[f"{base}/{name}"] = target
    return pids


def killed(world, pids):
    """The cell's other processes ended with the kill, as the harness's SIGKILL takes them; the wedged one stays."""
    for name in ("child", "grandchild"):
        world.host.files.pop(f"/proc/{pids[name]}/stat", None)
        world.host.alive.discard(pids[name])


def abort_frees(world, pids, aborts):
    """The sidecar's abort ends the survivor's wait: the process goes, as the pending SIGKILL takes."""

    def sidecar(container, argv, stdin):
        aborts.append((container.name, argv))
        world.host.unkillable.discard(pids["python"])
        world.host.files.pop(f"/proc/{pids['python']}/stat", None)
        return CmdResult(0)

    world.host.exec_handler = sidecar


def at_the_cap(world, sbx, **wedged):
    """Run a cell to its cap with the wedge in place; the result and the sandbox's record of the deadline."""
    pids = wedge(world, sbx, **wedged)
    aborts: list = []
    abort_frees(world, pids, aborts)
    result, _ = run_to_the_cap(sbx, world)
    assert aborts, "the abort was never made"
    return result, result.deadline


@pytest.fixture
def sandbox(world):
    sbx = start(world)
    yield sbx
    world.host.unkillable.clear()
    sbx.stop()


def test_o97_a_survivor_blocked_on_a_file_of_the_held_down_holder_is_the_agents_and_the_restore_is_pending(
    world, sandbox
):
    """O97: the zombie leader's thread waits in a read of fd 3, a file in space-a on the data mount, whose holder
    providers[1] is the one the run's injection holds down: the verdict is `agent`, the abort frees the cell, the
    deadline is `mount_aborted` with its restore pending, the kill stays the agent's time limit, and the run is not
    ended."""
    result, record = at_the_cap(world, sandbox)
    assert record["outcome"] == DL.MOUNT_ABORTED
    assert record["restore"] == DL.RESTORE_PENDING
    assert record["attribution"] == {
        "verdict": "agent",
        "why": "blocked_on_held_down_holder",
        "target": "providers[1]",
        "space_holder": "providers[1]",
    }
    assert (result.kill["reason"], result.kill["category"]) == ("time_limit", "agent")
    assert result.restore_pending is True
    assert sandbox._cells.ended is None


def test_o97_the_cell_the_survivor_was_the_agents_is_restored_and_the_next_cell_runs(world, sandbox):
    """O97: the whole path: the wedge is attributed to the agent, the abort frees the cell, the restore brings the mount
    back, the deadline becomes `mount_restored`, nothing ended the run, and the next cell runs."""
    result, record = at_the_cap(world, sandbox)
    assert result.restore_pending is True
    answer = sandbox.restore_mount()
    assert (answer["ok"], answer["generation"]) == (True, 2)
    assert record["outcome"] == DL.MOUNT_RESTORED
    assert record["restore"] == DL.RESTORE_DONE
    assert sandbox._cells.ended is None
    after = sandbox.exec("print(1)", 30)
    assert after.exit_code == 0
    assert not after.timed_out


def test_o97_the_record_carries_what_the_reader_found_and_nothing_a_record_may_not_hold(world, sandbox):
    """O97: the record keeps the survivor by its name, its state and its thread's call and object (the data mount, the
    space, the path below it), and the connection's state; no pid, no start, no path above the mount; and the call
    carries it whole: it passes the record's own check (`cell_deadline.problem`) as it is, with no fallback to the
    minimal form that drops the survivors."""
    _, record = at_the_cap(world, sandbox)
    [survivor] = record["survivors"]
    assert (survivor["comm"], survivor["state"]) == (
        "python",
        "Z",
    )  # the leader is a zombie; its thread is the one in D
    [task] = survivor["tasks"]
    assert task["state"] == "D"
    assert task["wchan"] == "request_wait_answer"
    assert task["syscall"] == {"nr": 0, "fd": 3}
    assert task["blocking"] == {
        "via": "fd",
        "cut": False,
        "objects": [{"mount": "data", "space": "space-a", "path": "file"}],
    }
    assert not {"pid", "start"} & set(survivor)
    assert record["connection"] == {"fusectl": "writable"}
    carried = cell_deadline_of(record)
    assert carried["survivors"] == record["survivors"]
    assert carried["attribution"]["verdict"] == "agent"
    assert cell_deadline.problem(carried) is None


@pytest.mark.parametrize(
    ("held", "holders", "wedged", "why"),
    [
        ((), HOLDERS, {}, attribution.NO_INJECTION),
        (("providers[1]",), {"space-b": "providers[2]"}, {}, attribution.SPACE_NOT_HELD_DOWN),
        (("providers[2]",), HOLDERS, {}, attribution.SPACE_NOT_HELD_DOWN),
        (("providers[1]",), HOLDERS, {"fd": "/work/out", "mount": "work"}, attribution.NOT_DATA_MOUNT),
        (("providers[1]",), HOLDERS, {"syscall": None}, attribution.UNKNOWN),
        (("providers[1]",), HOLDERS, {"syscall": "running\n"}, attribution.UNKNOWN),
        (("providers[1]",), HOLDERS, {"thread_state": "S", "zombie": False}, attribution.NOT_IN_D),
        (("providers[1]",), HOLDERS, {"syscall": "39 0x0 0x0 0x0 0x0 0x0 0x0 0x7ffd 0x7f11\n"}, attribution.UNKNOWN),
    ],
    ids=[
        "no injection in effect",
        "the space has another holder",
        "another provider is held down",
        "the file is on /work",
        "syscall unreadable",
        "task not in a syscall",
        "process not in D",
        "a call that is not resolved",
    ],
)
def test_o97_every_other_shape_is_infrastructure_with_its_reason_and_the_run_ends_as_it_did(
    world, held, holders, wedged, why
):
    """O97: the design's negatives, each through the whole path: no injection in effect, a space whose holder is not
    the one held down, a file on `/work`, a `syscall` that cannot be read or shows no call, a thread that is not in D,
    and a call whose object is not named, which holdings never resolve: the abort frees the cell as before, the
    verdict is `infrastructure` with the reason, there is no restore, the kill is `kill_survived`, and the run is
    over as `cell_unkillable`, as it was."""
    sbx = start(world, held=held, holders=holders)
    try:
        result, record = at_the_cap(world, sbx, **wedged)
        assert record["attribution"]["verdict"] == "infrastructure"
        assert record["attribution"]["why"] == why
        assert "restore" not in record
        assert record["outcome"] == DL.MOUNT_ABORTED
        assert (result.kill["reason"], result.kill["category"]) == ("kill_survived", "infrastructure")
        assert kinds.kill_error(result.kill).kind == "cell_unkillable"
        assert sbx._cells.ended is not None
        assert result.restore_pending is False
    finally:
        world.host.unkillable.clear()
        sbx.stop()


def test_o97_a_diagnosis_that_cannot_be_read_is_never_the_agents(world, monkeypatch):
    """O97: the root half's answer is lost (sudo refused it): no survivor is read, the verdict is infrastructure, and
    the run ends as it did before this existed. Unknown is never the agent's."""
    sbx = start(world)
    try:
        monkeypatch.setattr(FakeHost, "_snapshot_helper", lambda self, request: CmdResult(1, "", "sudo: refused"))
        _, record = at_the_cap(world, sbx)
        assert record["attribution"] == {
            "verdict": "infrastructure",
            "why": attribution.UNKNOWN,
            "target": "providers[1]",
            "space_holder": None,
        }
        assert "restore" not in record
    finally:
        world.host.unkillable.clear()
        sbx.stop()


def test_o97_the_diagnosis_gives_the_pid_and_start_the_deadline_listed_so_the_reading_covers_every_survivor(
    world, sandbox
):
    """O97: `reading` has an entry for each process listed, with its `pid` and `start` as listed (the deadline checks
    this, and anything less is no verdict); and the same output, handed to the attribution directly, is the agent's."""
    killed(world, wedge(world, sandbox))
    left = sandbox._cells.agent_processes()
    assert [p["pid"] for p in left] == [7002]
    found = sandbox._cells.diagnosis(left, 5.0)
    assert {(e["pid"], e["start"]) for e in found[DL.READING]} == {(p["pid"], p["start"]) for p in left}
    assert DL._Run.covers(left, found)
    verdict = attribution.attribute(found, {"providers[1]"}, HOLDERS)
    assert (verdict["verdict"], verdict["why"]) == ("agent", attribution.BLOCKED_ON_HOLDER)


def test_o97_a_survivor_of_the_cell_that_was_not_read_makes_the_verdict_infrastructure(world, sandbox):
    """O97: a process the cell has beside the wedged one that the reading did not reach (here: gone from `/proc` when it
    was read) leaves the reading short of a survivor, so there is no verdict: the whole is unknown, not the agent's."""
    pids = wedge(world, sandbox)
    world.host.files.pop(f"/proc/{pids['child']}/stat")  # the one beside it ended with the kill
    left = sandbox._cells.agent_processes()
    assert sorted(p["pid"] for p in left) == [pids["python"], pids["grandchild"]]
    world.host.files.pop(f"/proc/{pids['grandchild']}/stat")  # and this one between the listing and the reading
    found = sandbox._cells.diagnosis(left, 5.0)
    verdict = attribution.attribute(found, {"providers[1]"}, HOLDERS)
    assert (verdict["verdict"], verdict["why"]) == ("infrastructure", attribution.UNKNOWN)


def test_o97_the_data_mount_is_the_run_s_connection_and_no_other_fuse_mount_at_the_path(world, sandbox):
    """O97: the device the reading takes as the data mount is the run's own (`Roles.device`), read when the deadline
    fires: a process whose mount table shows another device at `/data` is on something else: not the data mount."""
    pids = wedge(world, sandbox)
    killed(world, pids)
    host = world.host
    base = f"/proc/{pids['python']}/task/{THREAD}"
    host.files[f"{base}/mountinfo"] = ROOT + "60 25 0:999 / /data ro - fuse.oneclient s ro\n"
    left = sandbox._cells.agent_processes()
    found = sandbox._cells.diagnosis(left, 5.0)
    assert attribution.attribute(found, {"providers[1]"}, HOLDERS)["why"] == attribution.NOT_DATA_MOUNT


def test_o97_a_run_with_no_mount_has_no_diagnosis(world):
    """O97: a REST run has no mount to read a block on: no `diagnosis`, so no survivor is ever the agent's there."""
    spec = make_spec(world, "via-gate")
    sbx = DockerSandbox.start(world.host, world.s, spec, **start_options(world))
    try:
        assert sbx._cells.diagnosis is None
        assert sbx._cells._escalation.restorable is False
    finally:
        sbx.stop()


def test_o97_a_read_that_never_returns_is_given_up_on_and_the_abort_keeps_its_time(world, monkeypatch):
    """O97: the root half's read of a survivor waits (the memory lock a process in D holds): the diagnosis is bounded by
    its own seconds and run in a thread that is given up on, so the abort still frees the cell, the verdict is
    infrastructure, the record says the diagnosis timed out, and the run ends as it did, never hangs."""
    sbx = start(world)
    release = threading.Event()
    try:
        sbx._cells._escalation = dataclasses.replace(sbx._cells._escalation, diagnose_s=0.3)
        monkeypatch.setattr(
            SudoProcReader, "snapshot", classmethod(lambda cls, *args, **kwargs: release.wait(30) and None)
        )
        result, record = at_the_cap(world, sbx)
        assert any(s["step"] == "diagnosis_timed_out" for s in record["steps"])
        assert record["attribution"]["verdict"] == "infrastructure"
        assert "restore" not in record
        assert (result.kill["reason"], result.kill["category"]) == ("kill_survived", "infrastructure")
    finally:
        release.set()
        world.host.unkillable.clear()
        sbx.stop()


def test_o97_the_docker_diagnosis_roles_are_the_cells_fixed_targets(world, sandbox):
    """O97: the data mount where the agent sees it, on the run's device; `/work`, `/tmp`, `/dev/shm`."""
    roles = DockerDiagnosis(
        world.host,
        waits=sandbox.waits,
        mountpoint=lambda: str(sandbox.dir / "mount"),
        mount_path="/data",
        inputs=("/inputs/a",),
    ).roles()
    assert (roles.data, roles.work, roles.tmp, roles.shm, roles.inputs) == (
        "/data",
        "/work",
        "/tmp",
        "/dev/shm",
        ("/inputs/a",),
    )
    assert roles.device == share_device(world.host)
