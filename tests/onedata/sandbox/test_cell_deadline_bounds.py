"""O34: the bounds of a cell's hard deadline. Each grace is counted by the clock, so a slow docker daemon cannot
stretch it; the whole escalation stays within one margin past the cap (`deadline.margin_s`); the sandbox's stop runs
once the run is classified, outside the cell's time; the wait that settles a deadline is bounded, and an escalation
that does not decide within it ends the run; every command of the escalation is given the time that is left, never a
fixed default; and only the init's own `sleep`, known by its pid and start time from the container's start, is spared
by the kill, never a process of the agent's that names itself `sleep`."""

import threading

import pytest
from deadline_support import (
    Clock,
    Processes,
    cell_processes,
    escalation,
    killed_pids,
    run_to_the_cap,
    steps,
    stopped_step,
)
from fake_host import FakeHost
from sandbox_support import FAST, around_run, make_spec, start_options

from expcore_onedata.sandbox import deadline, kinds, teardown
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.cells import KILL_S, SETTLE_SLACK_S, _CellProcesses
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

GRACE_S = 5.0


# ------------------------------------------------------------------------------------------- the clock and the margin
def test_o34_a_slow_daemon_cannot_stretch_a_grace_past_its_seconds():
    """O34: each listing of the cell's processes takes 4 s (a docker daemon under load): a grace of 5 s still ends 5 s
    after it began, by the clock, each listing given only what is left of it, and the whole escalation ends within its
    margin. Counted in polls, the 20 polls of each grace took 85 s."""
    clock = Clock()
    procs = Processes({12: "reader"}, wedged=(12,), clock=clock, slow_s=4.0)
    esc, calls = escalation(clock, abort=lambda _: {"ok": True, "connections": 1}, grace_s=GRACE_S)  # frees nothing
    record = deadline.enforce(procs, esc)
    assert steps(record) == ["kill", "survived", "abort", "survived", "stop"]
    kill, first, abort, second = (s["t_s"] for s in record["steps"][:4])
    assert first - kill <= GRACE_S
    assert second - abort <= GRACE_S
    assert record["steps"][-1]["t_s"] <= esc.margin_s
    assert record["outcome"] == deadline.SANDBOX_STOPPED
    assert calls[0][1] <= esc.margin_s - first  # the abort is given what the margin has left, no more


@pytest.mark.parametrize("ok", [False, True], ids=["the abort timed out", "the abort answered late"])
def test_o34_an_escalation_that_reaches_its_margin_stops_waiting_and_is_survived_kill(ok):
    """O34: an abort that takes all the time it is given (a `docker exec` in the sidecar that hangs to its own timeout)
    uses up the margin: the escalation waits no longer, records that the margin ran out, orders the stop all the same,
    and the outcome is `survived_kill`, never past `margin_s`."""
    clock, procs = Clock(), Processes({12: "reader"}, wedged=(12,))

    def slow_abort(timeout_s: float) -> dict:
        clock.now += timeout_s
        return {"ok": ok, "rc": 0 if ok else 124}

    esc, calls = escalation(clock, abort=slow_abort, grace_s=GRACE_S)
    record = deadline.enforce(procs, esc)
    assert record["outcome"] == deadline.SURVIVED
    assert steps(record) == ["kill", "survived", "abort", "margin", "stop"]
    assert record["steps"][-1]["t_s"] <= esc.margin_s == 2 * GRACE_S + deadline.COMMANDS_S
    assert calls[0][1] == pytest.approx(esc.margin_s - GRACE_S)


def test_o34_the_sandboxs_stop_runs_after_the_run_is_classified_and_outside_the_cells_time(world, mounted):
    """O34: when the abort fails, the sandbox's stop runs only once the deadline's outcome is published (the run's
    `ended`), and the cell returns without waiting for its teardown, which may take minutes: the teardown neither holds
    up the classification nor counts in the cell's time. Its result is the record's last step."""
    cell_processes(world, mounted, wedged=True)
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(3, "", "mount: permission denied")
    cells, seen, release = mounted._cells, [], threading.Event()
    stop = cells._stop

    def a_long_teardown():
        seen.append(cells.ended)
        assert release.wait(10)
        return stop()

    cells._stop = a_long_teardown
    result, _ = run_to_the_cap(mounted, world)
    record = result.kill["deadline"]
    assert [s for s in record["steps"] if s["step"] == "stopped"] == []  # the cell is back, the teardown is not done
    release.set()
    assert stopped_step(record)["ok"] is True
    assert seen == [record]  # classified before the teardown began
    assert mounted.stopped is True
    kept = mounted.metadata()["evidence_kept"]  # set aside as infrastructure: its disks are kept, never deleted
    assert kept["relocated"] is True
    assert (world.s.work_root / "_kept_evidence" / mounted.sid).is_dir()


# ---------------------------------------------------------------------------------------------- the init's own sleep
def test_o34_a_worker_named_sleep_under_the_init_is_killed_at_the_deadline(world, mounted):
    """O34: a process can write its own /proc/self/comm, and an orphan is reparented to the container's init: a worker
    of the cell named `sleep` under the init is killed at the cap like any other. Only the init's own `sleep`, the one
    of that pid and start time when the container started, is spared, and the init itself."""
    pids = cell_processes(world, mounted)
    agent = world.host.containers[mounted.names.agent]
    world.host.add_stat(7009, "sleep", agent.pid, start=90)  # an orphan that renamed itself, started after the init's
    world.host.add_cgroup(agent, [agent.pid, *pids.values(), 7009])
    run_to_the_cap(mounted, world)
    killed = killed_pids(world)
    assert 7009 in killed
    assert {pids["python"], pids["child"], pids["grandchild"]} <= killed
    assert pids["sleep"] not in killed
    assert agent.pid not in killed


def test_o34_a_pid_of_the_inits_sleep_taken_by_another_process_is_not_spared(world, mounted):
    """O34: the init's `sleep` is known by its pid and its start time together: a process that has the pid but not the
    start time (the pid was reused) is a process of the cell, and is killed."""
    agent = world.host.containers[mounted.names.agent]
    world.host.add_stat(agent.init_child, "sleep", agent.pid, start=95)  # the same pid, started later
    world.host.add_cgroup(agent, [agent.pid, agent.init_child])
    run_to_the_cap(mounted, world)
    assert agent.init_child in killed_pids(world)


def test_o34_a_start_whose_inits_sleep_cannot_be_identified_fails(world):
    """O34: the agent's container is refused when its init's `sleep` cannot be identified at its start: a sandbox that
    cannot tell it from a process of the agent's could not bound a cell."""
    world.host.init_starts_sleep = False
    with pytest.raises(SandboxError, match="sleep"):
        DockerSandbox.start(world.host, world.s, make_spec(world, "mounted"), **start_options(world))


def test_o34_without_a_record_of_the_start_the_teardown_spares_only_the_inits_first_sleep():
    """O34: the orphan reaper's teardown has no record of the container's start: it spares the init's `sleep` that
    started first, which no process of the agent's can predate, and signals a later one of that name."""
    host = FakeHost()
    host.add_image("agent:1", "sha256:" + "a" * 64)
    host.docker("run", "-d", "--name", "sbx-run1", "agent:1", "sleep", "infinity")
    agent = host.containers["sbx-run1"]
    host.add_stat(21, "sleep", agent.pid, start=3)
    host.add_stat(24, "sleep", agent.pid, start=40)
    host.add_stat(22, "python", 6999, start=30)
    host.add_cgroup(agent, [agent.pid, 24, 21, 22])
    info = teardown._inspect(host, "sbx-run1", teardown.WAITS)
    assert sorted(p["pid"] for p in teardown._readers(host, info)) == [22, 24]
    known = {**info, "sleep": {"pid": 24, "start": "40"}}  # with the identity recorded at the start, that one alone
    assert sorted(p["pid"] for p in teardown._readers(host, known)) == [21, 22]


# --------------------------------------------------------------------------------------- the bound of the settle
class Timer:
    """The timer of the cell running, as `settle` and `wait_decided` see it: the seconds each was allowed to wait for
    the deadline's outcome are kept, and `fire_now` answers as the test says (False: the cell's kill was clean; True:
    it fired and its outcome is decided; None: it was not decided in time)."""

    def __init__(self, answer: bool | None) -> None:
        self.answer, self.fired, self.waited = answer, [], []

    def fire_now(self, timeout_s: float) -> bool | None:
        self.fired.append(timeout_s)
        return self.answer

    def wait_decided(self, timeout_s: float) -> bool | None:
        self.waited.append(timeout_s)
        return self.answer


def test_o34_a_deadline_that_is_not_decided_within_its_bound_ends_the_run_as_survived_kill(world, mounted, monkeypatch):
    """O34: the run's body is over while a cell's escalation never decides (a docker daemon that answers none of the
    commands the margin bounds): `settle` waits for its outcome at most the cell's margin and `SETTLE_SLACK_S`, never
    for ever. An outcome it never learns is no clean kill, so the run is ended as `survived_kill`, which is
    `cell_unkillable`. Without that, the attempt would be scored the agent's own `time_cap`."""
    cells, timer = mounted._cells, Timer(None)
    monkeypatch.setattr(cells, "_running", timer)
    ended = cells.settle()
    bound = deadline.margin_s(world.s.kill_grace_s) + SETTLE_SLACK_S
    assert timer.fired == [bound] == [60.0]  # the margin of the default grace, and the slack: a bound, never None
    assert ended["outcome"] == deadline.SURVIVED
    assert deadline.SURVIVED in deadline.ESCALATED
    assert deadline.SURVIVED in deadline.STOPS
    assert cells.ended is ended
    assert cells.settle() is ended  # a later settle finds the same end
    error = kinds.run_ended_error(ended)
    assert error.kind == kinds.CELL_UNKILLABLE
    assert deadline.SURVIVED in error.detail


def test_o34_an_undecided_settle_keeps_the_end_a_deadline_has_already_published(world, mounted, monkeypatch):
    """O34: a deadline that published its own end (an abort that freed the cell) and was then not shown decided in time
    is the run's end as it published it, never replaced by `survived_kill`."""
    cells, timer = mounted._cells, Timer(None)
    published = {"outcome": deadline.MOUNT_ABORTED, "steps": []}
    cells.ended = published
    monkeypatch.setattr(cells, "_running", timer)
    assert cells.settle() is published


@pytest.mark.parametrize("answer", [False, True], ids=["killed cleanly", "decided, and published nothing"])
def test_o34_a_settled_deadline_that_is_decided_ends_nothing_by_itself(world, mounted, monkeypatch, answer):
    """O34: a deadline that is decided and whose record the timer's thread did not publish as the run's end (it killed
    the cell cleanly) leaves the run running; so does a settle with no cell running, and it asks for nothing."""
    cells, timer = mounted._cells, Timer(answer)
    monkeypatch.setattr(cells, "_running", timer)
    assert cells.settle() is None
    assert cells.ended is None
    monkeypatch.setattr(cells, "_running", None)
    assert cells.settle() is None
    assert len(timer.fired) == 1


def test_o34_a_stop_waits_for_a_deadline_under_way_no_longer_than_the_settle_does(world, mounted, monkeypatch):
    """O34: a sandbox's stop first waits for a deadline under way, so its teardown never races an escalation, bounded
    by the same margin and slack as the settle, and it never fires one."""
    cells, timer = mounted._cells, Timer(False)
    monkeypatch.setattr(cells, "_running", timer)
    cells.wait_decided()
    assert timer.waited == [deadline.margin_s(world.s.kill_grace_s) + SETTLE_SLACK_S]
    assert timer.fired == []


# ----------------------------------------------------------------------------- every command gets the time left
def recording_docker(host, clock: Clock | None = None, takes_s: float = 0.0) -> list[tuple[tuple, float | None]]:
    """What each docker command of the host was given, its arguments and its timeout: the fake host ignores timeouts.
    With a `clock`, every `inspect` takes `takes_s` of it, as a slow docker daemon's does."""
    given, original = [], host.docker

    def docker(*args, **kwargs):
        given.append((args, kwargs.get("timeout")))
        if clock is not None and args[0] == "inspect":
            clock.now += takes_s
        return original(*args, **kwargs)

    host.docker = docker
    return given


def recording_runs(host) -> list[tuple[list[str], float]]:
    """What each command run on the host was given: its argv and its timeout."""
    given = []

    def wrapper(original, argv, timeout_s, **kwargs):
        given.append((argv, timeout_s))
        return original(argv, timeout_s, **kwargs)

    around_run(host, wrapper)
    return given


def inspects_of(given, container: str) -> list[float | None]:
    return [timeout for args, timeout in given if args[0] == "inspect" and args[1] == container]


def kills_of(given) -> list[float]:
    return [timeout for argv, timeout in given if argv[:3] == ["kill", "-s", "KILL"]]


@pytest.mark.parametrize(
    ("left_s", "given_s"),
    [(0.3, 0.3), (50.0, FAST.command_s)],
    ids=["less than a command may take", "more: the command's own limit"],
)
def test_o34_the_abort_in_the_sidecar_is_given_the_time_the_margin_has_left(world, mounted, left_s, given_s):
    """O34: the sidecar's `docker exec` that aborts the mount is run with the seconds the deadline's margin has left it,
    never a fixed default: a hung one would otherwise outlast the margin it is bounded by."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0)
    docker = recording_docker(world.host)
    note = mounted._abort_mount(left_s)
    assert note["ok"] is True
    aborts = [timeout for args, timeout in docker if args[0] == "exec" and mounted.names.sidecar in args]
    assert aborts == [pytest.approx(given_s)]


def test_o34_an_abort_given_no_time_runs_nothing(world, mounted):
    """O34: an abort that has no time left runs no command in the sidecar and says the margin ran out."""
    docker = recording_docker(world.host)
    note = mounted._abort_mount(0.0)
    assert note["ok"] is False
    assert not [args for args, _ in docker if args[0] == "exec"]


def test_o34_the_listing_of_the_cells_processes_is_given_the_time_the_deadline_has_left(world, mounted):
    """O34: the `docker inspect` of the agent's container, which lists the cell's processes, is run with the seconds the
    deadline has left it when that is less than a command's own limit, and with that limit otherwise."""
    cell_processes(world, mounted)
    docker = recording_docker(world.host)
    tracker = _CellProcesses(mounted._cells)
    assert {p["pid"] for p in tracker.processes(0.5)} == {7002, 7003, 7004}
    tracker.processes(500.0)
    mounted._cells.agent_processes()  # the quiesce's: no deadline, the command's own limit
    assert inspects_of(docker, mounted.names.agent) == [0.5, teardown.WAITS.command_s, teardown.WAITS.command_s]


def test_o34_the_kill_of_the_cells_processes_is_given_the_time_the_deadline_has_left(world, mounted):
    """O34: the privileged `kill` of the cell's processes is run with the seconds the deadline has left it when that is
    less than `KILL_S`, and with `KILL_S` otherwise."""
    cell_processes(world, mounted)
    runs = recording_runs(world.host)
    tracker = _CellProcesses(mounted._cells)
    tracker.kill([{"pid": 7002}], 3.0)
    tracker.kill([{"pid": 7003}], 500.0)
    assert kills_of(runs) == [3.0, KILL_S]


def test_o34_each_command_of_the_escalation_is_given_what_its_margin_has_left_through_the_real_tracker(world, mounted):
    """O34: with a docker daemon whose every `inspect` takes 10 s, the real tracker over the host's processes is given
    the margin's commands allowance for its first listing (20 s of the 22 s margin of a grace of 1 s), the 12 s that
    are then left for the kill, and the same 12 s for the listing after it."""
    cell_processes(world, mounted)
    clock = Clock()
    docker, runs = recording_docker(world.host, clock, 10.0), recording_runs(world.host)
    esc, _ = escalation(clock, grace_s=1.0)
    record = deadline.enforce(_CellProcesses(mounted._cells), esc)
    assert record["outcome"] == deadline.KILLED
    assert esc.margin_s == 22.0
    assert inspects_of(docker, mounted.names.agent) == [20.0, 12.0]
    assert kills_of(runs) == [12.0]


# ----------------------------------------------------------------------------- the teardown spares the init's sleep
def test_o34_the_sandboxs_teardown_spares_the_inits_sleep_identified_at_the_start(world, mounted):
    """O34: the sandbox hands its teardown the identity of the init's `sleep` that it recorded at the container's start.
    An orphan named `sleep` that started before the init's own, which no real process of the agent's can, is signalled
    and the init's sleep is spared; the fallback without that record, the init's child of that name that started
    first, would spare the orphan and signal the init's sleep."""
    agent = world.host.containers[mounted.names.agent]
    assert mounted._init_sleep == {"pid": agent.init_child, "start": "2"}
    world.host.add_stat(7010, "sleep", agent.pid, start=1)  # older than the init's `sleep`, which started at tick 2
    world.host.add_cgroup(agent, [agent.pid, agent.init_child, 7010])
    mounted.stop()
    terminated = {
        int(pid) for argv in world.host.sudo_commands if argv[:3] == ["kill", "-s", "TERM"] for pid in argv[3:]
    }
    assert 7010 in terminated
    assert agent.init_child not in terminated


# --------------------------------------------------------------------------------- the metadata and the deadline's lock
def test_o34_the_metadata_is_read_whole_under_the_lock_a_deadline_writes_it_under(world, mounted):
    """O34: a cell's deadline writes the run's metadata from its timer's thread under the sandbox's metadata lock, so
    `metadata()` reads it under that lock: while another thread holds it, the read waits, and it ends once it is
    released. Read unlocked, it could meet a record half written."""
    holding, release, reads = threading.Event(), threading.Event(), []

    def hold() -> None:
        with mounted._meta_lock:
            holding.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    reader = threading.Thread(target=lambda: reads.append(mounted.metadata()), daemon=True)
    try:
        holder.start()
        assert holding.wait(10), "the lock was not taken"
        reader.start()
        reader.join(0.3)
        waited = reader.is_alive()
    finally:
        release.set()
        holder.join(10)
        reader.join(10)
    assert waited, "metadata() did not wait for the lock a deadline writes the metadata under"
    assert not reader.is_alive()
    assert reads
