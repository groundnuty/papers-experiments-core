"""O11: a sandbox records when its mount disconnected and its REST gate stopped.

An outage run's admission is let go only a drain after the later of the two (`expcore_onedata.fault_drain`), so the
sandbox says when each ended: the gate when its stop was confirmed (the collection stops it, or the teardown removes
it), the mount when the teardown has stopped and removed the oneclient sidecar that holds it. An end the sandbox could
not show is not recorded, and the drain then treats it as unknown.
"""

import time

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import teardown
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.sandbox import DockerSandbox


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def test_o11_a_rest_sandbox_expects_a_gate_and_records_its_stop_when_the_collection_stops_it(world, tmp_path):
    """O11: the gate's stop is confirmed in the collection, long before the sandbox's teardown: its end is that moment,
    so a restore that follows the collection overlaps the drain."""
    sbx = start(world, "via-gate")
    assert sbx.session_kinds() == ("gate",)
    assert sbx.session_ends() == {}
    before = time.monotonic()
    sbx.collect(tmp_path / "evidence")
    stopped = sbx.session_ends()["gate"]
    after = time.monotonic()
    assert before <= stopped <= after
    sbx.stop()
    assert sbx.session_ends()["gate"] == stopped  # the teardown that removes it later does not move it
    assert sbx.metadata()["session_ends"]["gate"]


def test_o11_a_rest_sandbox_never_collected_records_the_gates_end_when_the_teardown_removes_it(world):
    """O11: a stop with no collection first: the gate is gone once the teardown has removed its container."""
    sbx = start(world, "via-gate")
    before = time.monotonic()
    sbx.stop()
    assert before <= sbx.session_ends()["gate"] <= time.monotonic()


def test_o11_a_mount_sandbox_expects_a_mount_and_records_its_end_when_its_sidecar_is_gone(world):
    """O11: the mount is disconnected when the ordered teardown has stopped and removed the oneclient sidecar."""
    sbx = start(world, "mounted")
    assert sbx.session_kinds() == ("mount",)
    assert sbx.session_ends() == {}
    before = time.monotonic()
    sbx.stop()
    assert before <= sbx.session_ends()["mount"] <= time.monotonic()
    assert "gate" not in sbx.session_ends()
    assert sbx.metadata()["session_ends"]["mount"]


def test_o11_a_sandbox_with_no_data_path_has_no_session_to_end(world):
    """O11: no mount and no gate: nothing is expected and nothing is recorded."""
    sbx = start(world, "isolated")
    assert sbx.session_kinds() == ()
    sbx.stop()
    assert sbx.session_ends() == {}


def test_o11_a_mount_the_teardown_could_not_release_has_no_recorded_end(world, monkeypatch):
    """O11: a teardown that left the sidecar and the agent running with the mount live (`fuse_safe` false) shows no end
    of the mount, so none is recorded: the drain treats it as unknown and waits in full."""
    sbx = start(world, "mounted")
    held = teardown.TeardownResult(False, [], ["the FUSE mount could be neither unmounted nor aborted"], [])
    monkeypatch.setattr(teardown, "ordered_fuse_teardown", lambda *args, **kwargs: held)
    with pytest.raises(SandboxError):
        sbx.stop()
    assert "mount" not in sbx.session_ends()


def test_o11_a_stop_that_ends_after_a_failed_one_records_the_end_it_could_show(world, monkeypatch):
    """O11: a first stop that failed records nothing; a stop tried again that shows the sandbox gone records it."""
    sbx = start(world, "mounted")
    real = teardown.ordered_fuse_teardown
    held = teardown.TeardownResult(False, [], ["held"], [])
    monkeypatch.setattr(teardown, "ordered_fuse_teardown", lambda *args, **kwargs: held)
    with pytest.raises(SandboxError):
        sbx.stop()
    monkeypatch.setattr(teardown, "ordered_fuse_teardown", real)
    sbx.stop(force=True)
    assert "mount" in sbx.session_ends()
