"""O15: FUSE teardown is ordered: readers, then unmount, then sidecar, then agent; each step verified."""

import pytest
from fake_host import FakeHost

from expcore_onedata.sandbox import netpool, teardown
from expcore_onedata.sandbox.hostops import CmdResult

WAITS = teardown.Waits(
    reader_term_s=0.05,
    reader_kill_s=0.05,
    after_abort_s=0.02,
    unmount_s=0.05,
    sidecar_stop_s=1,
    command_s=1.0,
    probe_s=1.0,
    poll_s=0.01,
)
AGENT, SIDECAR = "sbx-run1", "sbx-run1" + teardown.SIDECAR_SUFFIX


@pytest.fixture
def world(tmp_path):
    """An agent with an init, its `sleep` and two readers; a sidecar; a FUSE mount at the sandbox's mount point."""
    host = FakeHost()
    run = tmp_path.resolve() / "run1"
    mountpoint = run / "mount"
    mountpoint.mkdir(parents=True)
    host.add_image("agent:1", "sha256:" + "a" * 64)
    host.docker("run", "-d", "--name", AGENT, "agent:1", "sleep", "infinity")
    host.docker("run", "-d", "--name", SIDECAR, "--mount", f"type=bind,src={mountpoint},dst=/mnt/oneclient", "agent:1")
    agent = host.containers[AGENT]
    host.add_stat(agent.pid, "tini", 1)
    host.add_stat(21, "sleep", agent.pid)
    host.add_stat(22, "python", agent.pid)
    host.add_stat(23, "python", 22)
    host.add_cgroup(agent, [agent.pid, 21, 22, 23])
    host.mounts[str(mountpoint)] = "fuse.oneclient"
    host.fuse_mountpoint = mountpoint
    return host


def run_teardown(host, **kwargs):
    return teardown.ordered_fuse_teardown(host, AGENT, SIDECAR, host.fuse_mountpoint, waits=WAITS, **kwargs)


def effects(host):
    """The order of the effects that matter: signals, unmounts, stops and removals."""
    out = []
    for call in host.calls:
        if call[0] == "run" and call[1][0] == "kill":
            out.append(("readers", call[1][2]))
        elif call[0] == "run" and call[1][0] in ("fusermount", "umount"):
            out.append(("unmount", call[1][-1]))
        elif call[0] == "docker" and call[1] == "stop":
            out.append(("stop", call[2][-1]))
        elif call[0] == "removed":
            out.append(("remove", call[1]))
    return out


def test_o15_the_teardown_order_is_readers_then_unmount_then_sidecar_then_agent(world):
    """O15: the host froze when the agent was killed with the mount live; the order is the fix."""
    result = run_teardown(world)
    steps = effects(world)
    kinds = [kind for kind, _ in steps]
    assert result.fuse_safe
    assert kinds == ["readers", "unmount", "stop", "remove", "remove"]
    assert steps[3] == ("remove", SIDECAR)
    assert steps[4] == ("remove", AGENT)
    assert steps[2] == ("stop", SIDECAR)


def test_o15_the_readers_are_signalled_without_the_init_and_its_sleep(world):
    """O15: the container's init and its `sleep` stay: they keep the mount namespace alive until the last step."""
    run_teardown(world)
    signalled = {pid for call in world.calls if call[0] == "run" and call[1][0] == "kill" for pid in call[1][3:]}
    assert signalled == {"22", "23"}


def test_o15_every_step_is_verified_by_reading_the_state_again(world):
    """O15: the unmount is checked in the mount table, the stop by inspecting the container, the removal by listing."""
    result = run_teardown(world)
    unmount = next(r for r in result.log if r["step"] == "unmount")
    assert unmount["verified"] is True
    assert str(world.fuse_mountpoint) not in world.mounts
    sidecar = [r for r in result.log if r["step"] == "sidecar"]
    assert sidecar[0]["sidecar_running_after"] is False
    assert world.containers == {}


def stick(host, *, abort_works):
    """The mount will not unmount; its fusectl entry exists; the abort works or is denied."""
    host.stuck_mounts.add(str(host.fuse_mountpoint))
    for n in range(100, 104):
        host.files[f"/sys/fs/fuse/connections/{n}"] = ""
    if not abort_works:
        host.run_handlers["sh"] = lambda argv: CmdResult(1, "", "denied")


def test_o15_a_mount_that_cannot_be_released_leaves_both_containers_running(world):
    """O15: no container is killed while its FUSE mount is live; the result says the containers must not be removed."""
    stick(world, abort_works=False)
    result = run_teardown(world)
    assert result.fuse_safe is False
    assert set(world.containers) == {AGENT, SIDECAR}
    assert [kind for kind, _ in effects(world) if kind in ("stop", "remove")] == []
    assert any("left running" in error for error in result.errors)


def test_o15_a_mount_that_cannot_be_unmounted_but_is_aborted_lets_the_containers_go_in_order(world):
    """O15: the last resort ends the FUSE connection; only then are the sidecar and the agent removed, in order."""
    stick(world, abort_works=True)
    result = run_teardown(world)
    assert result.fuse_safe is True
    assert next(r for r in result.log if r["step"] == "abort")["ok"] is True
    removals = [name for kind, name in effects(world) if kind == "remove"]
    assert removals == [SIDECAR, AGENT]


def test_o15_a_failed_removal_is_recorded_and_the_next_step_still_runs(world):
    """O15: every step runs whatever an earlier one raised; the failure is in the result."""
    world.fail_remove.add(SIDECAR)
    result = run_teardown(world)
    assert any("sidecar could not be removed" in error for error in result.errors)
    assert AGENT not in world.containers


def test_o15_the_log_is_written_step_by_step_so_a_freeze_leaves_its_last_step(world, tmp_path):
    """O15: one JSON line per step, flushed, in a file that survives a freeze."""
    log = tmp_path / "log" / "run1.jsonl"
    run_teardown(world, log_path=log)
    steps = [line.split('"step": "')[1].split('"')[0] for line in log.read_text().splitlines()]
    assert steps[0] == "begin"
    assert steps[-1] == "end"
    assert steps.index("unmount") < steps.index("sidecar") < steps.index("agent")


def test_o15_the_orphan_reaper_runs_the_same_order_before_removing_anything(world):
    """O15: a cleanup that removes containers by name, not knowing the order, goes through the ordered teardown."""
    held = teardown.fuse_first(world, [AGENT, SIDECAR], waits=WAITS)
    assert held == set()
    kinds = [kind for kind, _ in effects(world)]
    assert kinds.index("readers") < kinds.index("unmount") < kinds.index("stop")
    assert world.containers == {}


def test_o15_a_sidecar_whose_mount_point_cannot_be_found_is_held_not_removed(world):
    world.containers[SIDECAR].options["--mount"] = []
    held = teardown.fuse_first(world, [AGENT, SIDECAR], waits=WAITS)
    assert held == {AGENT, SIDECAR}
    assert set(world.containers) == {AGENT, SIDECAR}


def test_reaping_an_orphan_with_a_live_mount_goes_through_the_ordered_teardown(world):
    boot = netpool.owner_id(world).split("/")[0]
    for name in (AGENT, SIDECAR):
        world.containers[name].labels = {
            "org.test.sandbox": "1",
            "org.test.owner": f"{boot}/4026531836/4242/9",
            "org.test.dir": str(world.fuse_mountpoint.parent),
        }
    out = netpool.reap_orphans(world, netpool.Labels("org.test"))
    assert sorted(out["held"]) == []
    assert world.containers == {}
