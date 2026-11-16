"""O56, C19: what a run of the Apptainer backend leaves is recorded before it exists, reaped once its driver is gone,
and proved gone by the lease's probe; a start that fails leaves nothing, the token file first."""

import json

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import runs
from expcore_onedata.sandbox.apptainer.node import Spawned


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def _abandoned(node, settings):
    """A run whose driver was killed outright: its record, directory, helpers and a cell's process are all left."""
    box = start(node, settings, make_spec(lease_key="slot-a"))
    cell = node._new_proc(node.new_namespace(), "python3", environ=(f"EXPCORE_SANDBOX={box.sid}",))
    node.procs[node.owner.pid].alive = False  # the driver is gone
    return box, cell


def test_a_run_writes_its_record_naming_its_lease_its_owner_and_its_helpers(node, settings):
    """O56: the record is written before anything exists and names every helper as it starts; no secret."""
    box = start(node, settings, make_spec(lease_key="slot-a"))
    try:
        document = json.loads(runs.record_path(settings.work_root, box.sid).read_text())
        assert document["lease"] == "slot-a"
        assert document["owner"] == {"pid": node.owner.pid, "start": node.owner.start}
        assert sorted(h["role"] for h in document["helpers"]) == ["collector", "gate"]
        assert document["token_files"] == ["gate-secret/gate-token"]
    finally:
        box.stop()


def test_the_next_start_reaps_a_dead_driver_s_run_whole(node, settings):
    """O56, C19: its helpers and its cell's process are killed, its directory and record removed."""
    old, cell = _abandoned(node, settings)
    node.owner = Spawned(1001, "5151")
    node.procs[1001] = type(node.procs[999])(1001, "5151", "pid:[4026531836]", "driver", role="driver")
    fresh = start(node, settings)
    try:
        assert fresh.metadata()["reaped_orphans"] == {"runs": 1, "processes": 3, "left": 0}
        assert not cell.alive
        assert not old.dir.exists()
        assert not runs.record_path(settings.work_root, old.sid).exists()
    finally:
        fresh.stop()


def test_a_run_whose_driver_lives_is_never_reaped(node, settings):
    """O56: another live driver's run on the same work root is left alone."""
    first = start(node, settings)
    second = start(node, settings)
    try:
        assert second.metadata()["reaped_orphans"]["runs"] == 0
        assert first.dir.exists()
    finally:
        second.stop()
        first.stop()


def test_the_probe_holds_a_key_while_its_record_its_directory_or_a_tagged_process_is_left(node, settings):
    """O56, C19: the key is gone only when nothing of its runs is on the node."""
    probe = runs.ApptainerProbe(node, settings.work_root)
    box, cell = _abandoned(node, settings)
    assert probe.resource_gone("slot-a", {}) is False
    runs.drop(settings.work_root, box.sid)
    assert probe.resource_gone("slot-a", {}) is False  # its directory is still there
    runs.remove_tree(box.dir)
    assert probe.resource_gone("slot-a", {}) is False  # its cell's process carries its tag
    cell.alive = False
    for proc in node.procs.values():
        proc.alive = proc.role == "driver"
    assert probe.resource_gone("slot-a", {}) is True
    assert probe.resource_gone("slot-b", {}) is True


def test_the_probe_says_nothing_is_gone_when_the_node_s_processes_cannot_be_listed(node, settings):
    """O56: unknown is never gone."""
    node.proc_unreadable = True
    assert runs.ApptainerProbe(node, settings.work_root).resource_gone("slot-a", {}) is False


def test_a_gate_that_never_says_it_is_ready_fails_the_start_and_leaves_nothing(node, settings):
    """O56, O05: the token file is removed, the helpers stopped, the directory and the record gone."""
    node.gate_never_ready = True
    with pytest.raises(SandboxError, match="gate did not start"):
        start(node, settings)
    assert not [p for p in node.procs.values() if p.alive and p.role != "driver"]
    assert not [p for p in settings.work_root.iterdir() if p.name != runs.RUNS]
    assert not list((settings.work_root / runs.RUNS).iterdir())


def test_a_gate_whose_port_was_taken_is_started_again_on_another(node, settings):
    """O56: a free port another process bound first: the gate is tried again, with a new token file each time."""
    node.gate_busy = 1
    box = start(node, settings)
    try:
        assert len(node.gates) == 2
        assert node.gates[0].config["listen_port"] != node.gates[1].config["listen_port"]
        assert not (box.dir / "gate-secret" / "gate-token").exists()
    finally:
        box.stop()


def test_remove_tree_removes_read_only_directories_and_never_follows_a_link(tmp_path):
    """O56: a run's read-only copies are removed; a link out of the tree is removed, never its target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    tree = tmp_path / "run"
    (tree / "inputs" / "0").mkdir(parents=True)
    (tree / "inputs" / "0" / "f").write_text("x")
    (tree / "link").symlink_to(outside)
    (tree / "inputs" / "0").chmod(0o555)
    (tree / "inputs").chmod(0o555)
    runs.remove_tree(tree)
    assert not tree.exists()
    assert (outside / "keep.txt").read_text() == "keep"
