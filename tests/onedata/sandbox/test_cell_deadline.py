"""O34: a cell's hard deadline. At its cap every process of the cell is killed; one that outlives the kill (a read
waiting in the kernel on the FUSE mount, which a kill reaches only when it returns) gets a grace period, then the
mount's FUSE connection is aborted from the sidecar, and if that fails or cannot be done the sandbox is stopped. Every
step is recorded with its time, and a cell that outlived its kill makes the attempt infrastructure of a kind no
condition may declare.

A real process in uninterruptible sleep cannot be made in a test: the escalation is driven here by a fake process
tracker, and through the sandbox by the fake host's processes that ignore every signal (`FakeHost.unkillable`)."""

import pytest
from deadline_support import (
    CELL_S,
    Clock,
    Processes,
    cell_processes,
    escalation,
    killed_pids,
    run_to_the_cap,
    steps,
    stopped_step,
)
from sandbox_support import add_sandbox, make_spec, start_options

from expcore.config.loading import ConfigError
from expcore.errors import InfraError, untreatable_kinds
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox import deadline, kinds
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.containers import SIDECAR_ABORT, sidecar_abort_args
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.testing import write_platform


# ---------------------------------------------------------------------------------------------- the escalation alone
def test_o34_a_cell_whose_processes_end_at_the_kill_is_killed_at_its_deadline_and_nothing_more():
    """O34: the cell's processes go at the kill: the outcome is `killed`, recorded with its time, and the mount is
    neither aborted nor the sandbox stopped."""
    clock, procs = Clock(), Processes({11: "python", 12: "worker"})
    esc, calls = escalation(clock)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.KILLED
    assert steps(record) == ["kill", "ended"]
    assert record["steps"][0] == {"step": "kill", "t_s": 0.0, "signalled": 2}
    assert procs.kills == [[11, 12]]
    assert calls == []
    assert record["grace_s"] == 3.0
    assert record["margin_s"] == deadline.margin_s(3.0) == 2 * 3.0 + deadline.COMMANDS_S
    assert "utc" in record


def test_o34_a_process_that_ignores_sigterm_and_sigkill_outlives_the_grace_and_the_mount_is_aborted():
    """O34: a wedged process survives its kill; the sandbox waits out the grace period (not less), records it as
    survived with its state, aborts the mount, and the process then ends: the outcome is `mount_aborted`."""
    clock, procs = Clock(), Processes({11: "python", 12: "reader"}, wedged=(12,))

    def abort(timeout_s: float) -> dict:
        procs.release()
        return {"ok": True, "connections": 1}

    esc, calls = escalation(clock, abort=abort)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.MOUNT_ABORTED
    assert steps(record) == ["kill", "survived", "abort", "ended"]
    survived = record["steps"][1]
    assert survived["t_s"] >= 3.0  # the whole grace period, by the clock
    assert (survived["left"], survived["states"]) == (1, ["D"])
    assert record["steps"][2]["ok"] is True
    assert [name for name, _ in calls] == ["abort"]
    times = [s["t_s"] for s in record["steps"]]
    assert times == sorted(times)


@pytest.mark.parametrize(
    "abort",
    [
        pytest.param(lambda _: {"ok": False, "rc": 1}, id="the abort failed"),
        pytest.param(lambda _: {"ok": None, "why": "no FUSE mount"}, id="no mount to abort"),
        pytest.param(lambda _: {"ok": True, "connections": 1}, id="aborted, and the process still lives"),
    ],
)
def test_o34_when_the_abort_fails_or_cannot_be_done_or_frees_nothing_the_sandbox_is_stopped(abort):
    """O34: the abort is the first resort only: when it fails, when there is no FUSE mount to abort, or when the
    process still lives after it, the sandbox's stop is ordered, its last step, and the outcome is `sandbox_stopped`:
    the stop itself is the caller's, once that outcome is recorded."""
    clock, procs = Clock(), Processes({12: "reader"}, wedged=(12,))
    esc, calls = escalation(clock, abort=abort)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.SANDBOX_STOPPED
    assert record["outcome"] in deadline.STOPS
    assert [name for name, _ in calls] == ["abort"]
    assert steps(record)[-1] == "stop"


def test_o34_processes_that_cannot_be_listed_are_never_taken_for_gone():
    """O34: unknown is never gone: a cell whose processes cannot be listed escalates as one that survived."""
    clock, procs = Clock(), Processes({11: "python"})
    procs.unknown = True
    esc, calls = escalation(clock)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.SANDBOX_STOPPED
    assert record["steps"][0]["signalled"] is None
    assert record["steps"][1]["left"] is None
    assert [name for name, _ in calls] == ["abort"]


def test_o34_a_process_the_cell_starts_during_the_grace_is_killed_too():
    """O34: a process that appears while the grace runs (a worker forked before its parent died) is killed as well."""
    clock, procs = Clock(), Processes({11: "python"})
    kill = procs.kill

    def kill_and_fork(listed: list[dict], timeout_s: float) -> None:
        kill(listed, timeout_s)
        if len(procs.kills) == 1:  # the first kill: a child forked just before it appears
            procs.procs[21] = {"pid": 21, "comm": "worker", "state": "R", "start": "21"}

    procs.kill = kill_and_fork
    record = deadline.enforce(procs, escalation(clock)[0])
    assert record["outcome"] == deadline.KILLED
    assert [21] in procs.kills


def test_o34_the_kill_survived_reason_is_infrastructure_of_a_kind_no_condition_may_declare():
    """O34: a cell that outlived its kill is infrastructure, of the platform's kind `cell_unkillable`, which is not
    treatable; a clean kill at the deadline stays the agent's own time limit."""
    record = {"outcome": deadline.MOUNT_ABORTED, "steps": []}
    kill = {"reason": "kill_survived", "category": "infrastructure", "basis": "x", "deadline": record}
    error = kinds.kill_error(kill)
    assert error.kind == kinds.CELL_UNKILLABLE == "cell_unkillable"
    assert kinds.CELL_UNKILLABLE in untreatable_kinds()
    assert kinds.deadline_error(kill).kind == kinds.CELL_UNKILLABLE
    assert kinds.deadline_error({"reason": "time_limit", "category": "agent"}) is None
    assert kinds.kill_error({"reason": "time_limit", "category": "agent"}) is None


# ---------------------------------------------------------------------------------------------- through the sandbox
def test_o34_a_normal_cell_killed_at_its_deadline_is_its_own_time_limit_as_before(world, mounted):
    """O34: a cell whose processes end at the kill is the agent's own time limit, as before, with the deadline's record
    saying `killed`; nothing is aborted and the sandbox runs on."""
    cell_processes(world, mounted)
    result, others = run_to_the_cap(mounted, world)
    assert result.timed_out is True
    assert (result.kill["reason"], result.kill["category"]) == ("time_limit", "agent")
    assert kinds.kill_error(result.kill) is None
    record = mounted.metadata()["cell_deadlines"][-1]
    assert (record["cell"], record["outcome"]) == (1, "killed")
    assert others == []  # nothing ran in the sidecar
    assert mounted.stopped is False
    assert mounted.exec("print(1)", 30).exit_code == 0  # the run goes on


def test_o34_the_kill_at_the_deadline_reaches_a_grandchild_in_a_session_of_its_own(world, mounted):
    """O34: the kill at the deadline takes every process of the cell from the container's cgroup, wider than its
    process group: the cell's python, its child and a grandchild that left the group, never the init or its sleep."""
    pids = cell_processes(world, mounted)
    run_to_the_cap(mounted, world)
    agent = world.host.containers[mounted.names.agent]
    killed = killed_pids(world)
    assert {pids["python"], pids["child"], pids["grandchild"]} <= killed
    assert agent.pid not in killed
    assert pids["sleep"] not in killed


def test_o34_a_wedged_cell_has_its_mount_aborted_from_the_sidecar_and_its_attempt_is_infrastructure(world, mounted):
    """O34: a cell process that ignores SIGKILL outlives the grace; the sandbox aborts the mount's FUSE connection from
    the sidecar (the connection the host's mount table gives the run's mount, written as root in the sidecar, nothing
    in the agent's container), the process ends, and the cell is `kill_survived`, infrastructure of the untreatable
    kind `cell_unkillable`. The run is over: the next cell is refused with that kind, and the record keeps each step
    with its time."""
    pids = cell_processes(world, mounted, wedged=True)
    aborts = []

    def sidecar(container, argv, stdin):
        aborts.append((container.name, argv))
        world.host.unkillable.discard(pids["python"])  # the read fails with an errno: the pending SIGKILL takes
        world.host.files.pop(f"/proc/{pids['python']}/stat", None)
        return CmdResult(0)

    world.host.exec_handler = sidecar
    result, others = run_to_the_cap(mounted, world)
    assert others == aborts  # the abort is the only exec besides the cell, and it ran in the sidecar
    connection = next(  # the device of the run's FUSE mount in the host's table: 0:<connection>
        line.split()[2].split(":")[1] for line in world.host.mountinfo().splitlines() if "fuse.oneclient" in line
    )
    assert aborts == [(mounted.names.sidecar, sidecar_abort_args(mounted.names, connection)[4:])]
    assert sidecar_abort_args(mounted.names, connection)[:4] == ["exec", "-u", "0", mounted.names.sidecar]
    assert (result.kill["reason"], result.kill["category"]) == ("kill_survived", "infrastructure")
    error = kinds.kill_error(result.kill)
    assert error.kind == "cell_unkillable"
    assert "cell_unkillable" in untreatable_kinds()
    record = result.kill["deadline"]
    assert record["outcome"] == "mount_aborted"
    assert [s["step"] for s in record["steps"]] == ["kill", "survived", "abort", "ended"]
    assert record["steps"][1]["states"] == ["D"]
    meta = mounted.metadata()
    assert meta["cell_deadlines"][-1]["outcome"] == "mount_aborted"
    assert meta["mount"]["aborted"] is True
    with pytest.raises(InfraError) as refused:  # the mount is gone: no later cell reads an empty mount point
        mounted.exec("print(1)", 30)
    assert refused.value.kind == "cell_unkillable"


def test_o34_an_abort_that_fails_stops_the_sandbox_and_the_attempt_is_infrastructure(world, mounted):
    """O34: when the sidecar's abort fails, the sandbox is stopped (its ordered teardown), the outcome is
    `sandbox_stopped`, and a later cell or collection is refused as `cell_unkillable`, not as a sandbox that is
    merely not running."""
    cell_processes(world, mounted, wedged=True)
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(3, "", "mount: permission denied")
    result, others = run_to_the_cap(mounted, world)
    assert [name for name, _ in others] == [mounted.names.sidecar]
    record = result.kill["deadline"]
    assert record["outcome"] == "sandbox_stopped"
    assert stopped_step(record)["ok"] is True  # the stop ran once the outcome was recorded: its own last step
    assert [s["step"] for s in record["steps"]] == ["kill", "survived", "abort", "stop", "stopped"]
    assert record["steps"][2]["ok"] is False
    assert mounted.stopped is True
    assert kinds.kill_error(result.kill).kind == "cell_unkillable"
    with pytest.raises(InfraError) as refused:
        mounted.exec("print(1)", 30)
    assert refused.value.kind == "cell_unkillable"
    with pytest.raises(InfraError) as uncollected:
        mounted.collect(world.tmp_path / "out")
    assert uncollected.value.kind == "cell_unkillable"


def test_o34_without_a_mount_a_cell_that_outlives_its_kill_stops_the_sandbox(world):
    """O34: with no FUSE mount (no data path) there is nothing to abort: the sandbox is stopped."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    try:
        cell_processes(world, sandbox, wedged=True)
        result, others = run_to_the_cap(sandbox, world)
        record = result.kill["deadline"]
        assert record["outcome"] == "sandbox_stopped"
        assert record["steps"][2]["ok"] is None  # no abort was possible
        assert stopped_step(record)["ok"] is True
        assert others == []
    finally:
        world.host.unkillable.clear()
        sandbox.stop()


def test_o34_the_code_tool_ends_the_attempt_as_cell_unkillable(world, mounted):
    """O34: the code tool raises the cell's kind for a cell that outlived its kill, never a timeout told to the
    model."""
    import asyncio

    pids = cell_processes(world, mounted, wedged=True)

    def sidecar(container, argv, stdin):
        world.host.unkillable.discard(pids["python"])
        world.host.files.pop(f"/proc/{pids['python']}/stat", None)
        return CmdResult(0)

    world.host.exec_handler = sidecar
    tool = CodeExecutionProvider(mounted)
    result, _ = run_to_the_cap(mounted, world)  # the cell itself, as the tool would have run it
    mounted.exec = lambda code, timeout_s: result  # the tool's next call returns that cell's result
    with pytest.raises(InfraError) as ended:
        asyncio.run(tool.execute("python", {"code": "x"}, timeout_s=CELL_S, ctx=None))
    assert ended.value.kind == "cell_unkillable"


def test_o34_the_grace_period_is_a_setting_and_is_recorded(world, mounted):
    """O34: `[sandbox] kill_grace_s` sets the grace (5 s unless declared) and the run's metadata records it."""
    assert world.s.kill_grace_s == 5.0
    assert mounted.metadata()["cell_deadline"] == {"grace_s": 5.0, "margin_s": 30.0}
    assert "fusermount" not in SIDECAR_ABORT  # a lazy unmount leaves the agent's copy and its waiting reads
    assert "/abort" in SIDECAR_ABORT


@pytest.mark.parametrize(("value", "loads"), [(2, 2.0), (12.5, 12.5), (0.5, None), (61, None), ("5", None)])
def test_o34_the_grace_period_is_read_from_the_file_within_its_bounds(tmp_path, value, loads):
    """O34: a declared grace of 1 to 60 seconds is read; one outside that, or not a number, refuses the load naming
    the key."""

    def edit(data):
        add_sandbox(data)
        data["sandbox"]["kill_grace_s"] = value

    path = write_platform(tmp_path, edit)
    if loads is None:
        with pytest.raises(ConfigError, match="kill_grace_s"):
            load_platform(path, require_secrets=False)
    else:
        assert load_platform(path, require_secrets=False).sandbox.kill_grace_s == loads


def test_o34_the_sidecars_abort_takes_a_connection_number_and_nothing_else(world):
    """O34: the abort's argument is the connection's number from the mount table, never a path or a name."""
    names = DockerSandbox(world.host, world.s, make_spec(world, "mounted")).names
    assert sidecar_abort_args(names, "61")[-1] == "61"
    for bad in ("", "6/1", "../abort", "0:61"):
        with pytest.raises(ValueError, match="by its number"):
            sidecar_abort_args(names, bad)
