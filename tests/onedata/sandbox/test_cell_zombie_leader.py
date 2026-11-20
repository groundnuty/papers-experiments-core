"""O97: a cell's process whose leader is a zombie while one of its threads still waits in the kernel is a survivor.

A reader in a thread other than the main one leaves a zombie leader (the SIGKILL ended the main thread) and a thread
in uninterruptible sleep, waiting on its FUSE request. Docker's listing took a zombie for gone, so the deadline found
nothing left and recorded the cell as cleanly killed while the thread still waited. The cell's tracker lists such a
process, as D, when a thread of it is in uninterruptible sleep, the one state that can outlive a SIGKILL (a thread
that is only exiting is in R or S for an instant, and is not a survivor); the teardown's own listing is unchanged, and
unknown is never gone.
"""

import pytest
from deadline_support import cell_processes
from fake_host import FakeHost

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.cells import _CellProcesses

LEADER, THREAD = 7002, 7010


def agent_of(world, mounted):
    return teardown._inspect(world.host, mounted.names.agent, teardown.WAITS)


def zombie_leader(world, mounted, threads):
    """The cell's python is a zombie leader whose threads are `{tid: state}`."""
    pids = cell_processes(world, mounted)
    world.host.add_stat(pids["python"], "python", 6999, state="Z")
    world.host.add_threads(world.host.containers[mounted.names.agent], pids["python"], threads)
    return pids


def test_o97_a_zombie_leader_with_a_thread_in_d_is_listed_in_the_threads_state(world, mounted):
    """O97: the leader is Z and its second thread is in D: the process is listed, as D, and says its leader is a
    zombie; the process's pid and start are the leader's, which is what a signal and a reading identify it by."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: "D"})
    listed = {p["pid"]: p for p in teardown._readers(world.host, agent_of(world, mounted), zombie_threads=True)}
    assert listed[pids["python"]]["state"] == "D"
    assert listed[pids["python"]]["zombie_leader"] is True
    assert listed[pids["python"]]["start"] == "1"
    assert pids["child"] in listed  # the rest of the cell is listed as it was


@pytest.mark.parametrize("state", ["R", "S", "X", "Z"])
def test_o97_a_zombie_leader_whose_thread_is_only_exiting_is_not_listed(world, mounted, state):
    """O97: every thread of a killed process is in R or S for an instant while it exits, with a zombie leader: only a
    thread in uninterruptible sleep (D) can outlive the SIGKILL, so only that is a survivor. Counting an exiting one
    would end a run whose cell was killed cleanly, at the sweep after every cell."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: state})
    listed = {p["pid"] for p in teardown._readers(world.host, agent_of(world, mounted), zombie_threads=True)}
    assert pids["python"] not in listed


def test_o97_a_zombie_leader_whose_threads_are_all_gone_is_not_listed(world, mounted):
    """O97: the control: a zombie leader with nothing left waiting is gone, as before."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: "Z"})
    listed = {p["pid"] for p in teardown._readers(world.host, agent_of(world, mounted), zombie_threads=True)}
    assert pids["python"] not in listed
    assert {pids["child"], pids["grandchild"]} <= listed


def test_o97_the_teardown_listing_still_takes_a_zombie_for_gone(world, mounted):
    """O97: only the cell's tracker asks for the threads; the teardown's `_readers` is as it was."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: "D"})
    listed = {p["pid"] for p in teardown._readers(world.host, agent_of(world, mounted))}
    assert pids["python"] not in listed


def test_o97_a_zombie_leader_whose_threads_cannot_be_read_makes_the_listing_unknown(world, mounted):
    """O97: unknown is never gone: no thread list, or a thread whose state is refused, is an unknown listing, so the
    deadline does not take the cell for ended."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: "D"})
    agent = agent_of(world, mounted)
    world.host.denied.add(f"/proc/{pids['python']}/task/{THREAD}/stat")
    assert teardown._readers(world.host, agent, zombie_threads=True) is None
    world.host.denied.clear()
    world.host.files.pop(f"/sys/fs/cgroup/system.slice/docker-{agent['id']}.scope/cgroup.threads")
    assert teardown._readers(world.host, agent, zombie_threads=True) is None


def test_o97_the_cells_tracker_lists_the_zombie_leader(world, mounted):
    """O97: through the runner: the tracker's listing, the deadline's, holds the process, in D."""
    pids = zombie_leader(world, mounted, {LEADER: "Z", THREAD: "D"})
    tracker = _CellProcesses(mounted._cells)
    listed = {p["pid"]: p for p in tracker.processes(5.0)}
    assert listed[pids["python"]]["state"] == "D"


def test_o97_a_thread_of_another_process_in_the_cgroup_is_not_this_zombie_leaders(world, mounted):
    """O97: a thread id the cgroup lists that is not a thread of the zombie leader (its `task/<tid>/stat` is not under
    the leader) does not keep it alive."""
    pids = zombie_leader(world, mounted, {LEADER: "Z"})
    world.host.files[
        f"/sys/fs/cgroup/system.slice/docker-{world.host.containers[mounted.names.agent].id}.scope/cgroup.threads"
    ] += "7099\n"
    listed = {p["pid"] for p in teardown._readers(world.host, agent_of(world, mounted), zombie_threads=True)}
    assert pids["python"] not in listed


def test_o97_a_container_with_no_zombie_is_listed_with_no_thread_read(world, mounted, monkeypatch):
    """O97: the thread lists are read only for a zombie leader: a listing with none reads none."""
    cell_processes(world, mounted)
    reads, real = [], FakeHost.read_text
    monkeypatch.setattr(FakeHost, "read_text", lambda self, path: (reads.append(str(path)), real(self, path))[1])
    teardown._readers(world.host, agent_of(world, mounted), zombie_threads=True)
    assert not [r for r in reads if "cgroup.threads" in r or "/task/" in r]
