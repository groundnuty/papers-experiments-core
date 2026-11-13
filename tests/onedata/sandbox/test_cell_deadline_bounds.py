"""O30: the bounds of a cell's hard deadline. Only the init's own `sleep`, known by its pid and start time from the
container's start, is spared by the kill; a process of the agent's that names itself `sleep` is not."""

import pytest
from deadline_support import cell_processes, killed_pids, run_to_the_cap
from fake_host import FakeHost
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.sandbox import DockerSandbox


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
