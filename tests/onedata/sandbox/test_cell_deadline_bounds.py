"""O30: the bounds of a cell's hard deadline. Each grace is counted by the clock, so a slow docker daemon cannot
stretch it; the whole escalation stays within one margin past the cap (`deadline.margin_s`); the sandbox's stop runs
once the run is classified, outside the cell's time; and only the init's own `sleep`, known by its pid and start time
from the container's start, is spared by the kill, never a process of the agent's that names itself `sleep`."""

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
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import deadline, teardown
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

GRACE_S = 5.0


# ------------------------------------------------------------------------------------------- the clock and the margin
def test_o30_a_slow_daemon_cannot_stretch_a_grace_past_its_seconds():
    """O30: each listing of the cell's processes takes 4 s (a docker daemon under load): a grace of 5 s still ends 5 s
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
def test_o30_an_escalation_that_reaches_its_margin_stops_waiting_and_is_survived_kill(ok):
    """O30: an abort that takes all the time it is given (a `docker exec` in the sidecar that hangs to its own timeout)
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


def test_o30_the_sandboxs_stop_runs_after_the_run_is_classified_and_outside_the_cells_time(world, mounted):
    """O30: when the abort fails, the sandbox's stop runs only once the deadline's outcome is published (the run's
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
def test_o30_a_worker_named_sleep_under_the_init_is_killed_at_the_deadline(world, mounted):
    """O30: a process can write its own /proc/self/comm, and an orphan is reparented to the container's init: a worker
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


def test_o30_a_pid_of_the_inits_sleep_taken_by_another_process_is_not_spared(world, mounted):
    """O30: the init's `sleep` is known by its pid and its start time together: a process that has the pid but not the
    start time (the pid was reused) is a process of the cell, and is killed."""
    agent = world.host.containers[mounted.names.agent]
    world.host.add_stat(agent.init_child, "sleep", agent.pid, start=95)  # the same pid, started later
    world.host.add_cgroup(agent, [agent.pid, agent.init_child])
    run_to_the_cap(mounted, world)
    assert agent.init_child in killed_pids(world)


def test_o30_a_start_whose_inits_sleep_cannot_be_identified_fails(world):
    """O30: the agent's container is refused when its init's `sleep` cannot be identified at its start: a sandbox that
    cannot tell it from a process of the agent's could not bound a cell."""
    world.host.init_starts_sleep = False
    with pytest.raises(SandboxError, match="sleep"):
        DockerSandbox.start(world.host, world.s, make_spec(world, "mounted"), **start_options(world))


def test_o30_without_a_record_of_the_start_the_teardown_spares_only_the_inits_first_sleep():
    """O30: the orphan reaper's teardown has no record of the container's start: it spares the init's `sleep` that
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
