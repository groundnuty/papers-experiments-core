"""The post-run readings of a run's infrastructure on an HPC node, with Docker's names and shape (O17, O18).

`infra_check()` reads the run's other infrastructure by role (the collector, the gate, the mount helper's oneclient
with its mount, and the mount guard's watcher) before and after the collection, which stops some of them itself;
`mount_errors()` and `mount_log()` read oneclient's own log from the host mount's log directory, the token redacted.
"""

import json
from pathlib import Path

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, options, pinned_bundle, start
from fake_mount_node import FakeMountNode
from fake_node import FakeNode

from expcore_onedata.sandbox import kinds, oneclient_log
from expcore_onedata.sandbox.api import InfraReadings
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

LOG = (Path(__file__).resolve().parents[3] / "data" / "sandbox" / "oneclient.log").read_text(encoding="utf-8")
DEATH_KEYS = {"role", "condition", "cause", "category", "basis", "problems"}  # a Docker death's keys


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))


@pytest.fixture
def mount_node() -> FakeMountNode:
    return FakeMountNode()


def mounted(mount_node, settings) -> ApptainerSandbox:
    factory = HM.host_mount_factory(mountinfo=mount_node.mountinfo)
    return ApptainerSandbox.start(mount_node, settings, make_spec("mounted"), **options(mount_helper=factory))


def roles(check) -> list[str]:
    return [death["role"] for death in check["deaths"]]


def test_o17_a_healthy_rest_run_has_dockers_shape_and_no_death_before_or_after_its_collection(node, settings, tmp_path):
    """O17: the gate and the collector by role, as Docker's containers are; the collection stops both itself, and a
    stop of the harness's own is no death: each is judged by its state just before it."""
    box = start(node, settings)
    try:
        assert isinstance(box, InfraReadings)
        check = box.infra_check()
        assert set(check) == {"containers", "deaths", "death"}
        assert set(check["containers"]) == {"collector", "gate"}
        assert (check["deaths"], check["death"]) == ([], [])
        assert all(entry["end"]["running"] is True for entry in check["containers"].values())
        assert box.collect(tmp_path / "out")["deaths"] == []
        after = box.infra_check()
        assert (after["deaths"], after["death"]) == ([], [])
        for role in ("collector", "gate"):
            assert after["containers"][role]["stopped_by_harness"] is True
            assert node.alive(*_process(box, role)) is False  # stopped, by the harness
    finally:
        box.stop()


def _process(box, role):
    helper = box._helpers[role]
    return helper.process.pid, helper.process.start


@pytest.mark.parametrize("role", ["gate", "collector"])
def test_o17_a_helper_that_died_during_the_run_is_a_death_before_and_after_the_collection(
    node, settings, tmp_path, role
):
    """O17: a helper gone before the harness stopped it is infrastructure, with Docker's death keys, read the same
    before the collection, by the collection's own deaths and after it, and `kinds.death_error` sets the run aside."""
    box = start(node, settings)
    try:
        proc = node.procs[_process(box, role)[0]]
        proc.alive, proc.exit_status = False, 137
        before = box.infra_check()
        assert roles(before) == [role]
        death = before["deaths"][0]
        assert set(death) == DEATH_KEYS
        assert (death["category"], death["cause"], death["condition"]) == ("infrastructure", "undetermined", "rest")
        assert "exit status 137" in before["death"][0]
        assert roles(box.collect(tmp_path / "out")) == [role]
        assert roles(box.infra_check()) == [role]
        assert kinds.death_error(before["deaths"]).kind == "unattributed_kill"
    finally:
        box.stop()


def test_o17_a_helper_whose_state_cannot_be_read_is_a_death_never_a_clean_one(node, settings):
    """O17: unknown is not clean: a gate the node cannot tell alive or gone is a problem."""
    box = start(node, settings)
    try:
        pid = _process(box, "gate")[0]
        alive = node.alive
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(node, "alive", lambda p, s: None if p == pid else alive(p, s))
            check = box.infra_check()
        assert roles(check) == ["gate"]
        assert "could not be read" in check["death"][0]
    finally:
        box.stop()


def test_o17_a_run_with_no_data_path_has_the_collector_alone(node, settings):
    """O17: no gate and no mount: the collector is the run's one other process."""
    box = start(node, settings, make_spec("isolated"))
    try:
        assert set(box.infra_check()["containers"]) == {"collector"}
    finally:
        box.stop()


def test_o17_a_healthy_mount_run_reads_oneclient_its_mount_and_the_watcher_clean_through_its_collection(
    mount_node, settings, tmp_path
):
    """O17: the mount helper's oneclient with its mount, as Docker's sidecar, and the guard's watcher: no death before
    or after the collection, which stops the watcher itself when it closes the guard's window, and leaves oneclient
    running until the stop; after the stop each reads as it was just before the harness stopped it."""
    box = mounted(mount_node, settings)
    try:
        check = box.infra_check()
        assert set(check["containers"]) == {"collector", "mount", "mount_watch"}
        assert check["containers"]["mount"]["end"] == {"running": True, "exit_status": None, "mount": None}
        assert check["deaths"] == []
        mount_node.cell()
        assert box.collect(tmp_path / "out")["deaths"] == []
        after = box.infra_check()
        assert after["deaths"] == []
        assert not mount_node.watch().proc.alive  # the collection closed the guard's window
        assert after["containers"]["mount_watch"]["stopped_by_harness"] is True
        assert "stopped_by_harness" not in after["containers"]["mount"]  # oneclient runs until the stop
    finally:
        box.stop()
    stopped = box.infra_check()
    assert stopped["deaths"] == []
    assert all(entry.get("stopped_by_harness") for entry in stopped["containers"].values())


def test_o17_a_mount_watcher_that_died_during_the_run_is_a_death(mount_node, settings, tmp_path):
    """O17: the guard's watcher is the run's infrastructure too: one that ended before the window closed is a death,
    before the collection, in its deaths and after it, and the guard reads its log as unknown, never clean (O60)."""
    box = mounted(mount_node, settings)
    try:
        proc = mount_node.watch().proc
        proc.alive, proc.exit_status = False, 137
        assert roles(box.infra_check()) == ["mount_watch"]
        assert roles({"deaths": box.collect(tmp_path / "out")["deaths"]}) == ["mount_watch"]
        assert roles(box.infra_check()) == ["mount_watch"]
        assert box.metadata()["mount"]["guard"]["verdict"] != "clean"
    finally:
        box.stop()


def test_o17_a_oneclient_that_died_during_a_mount_run_is_a_death_the_collection_reports_too(
    mount_node, settings, tmp_path
):
    """O17: the mount's oneclient is the run's infrastructure as Docker's sidecar is: one that ended is a death, in
    the check and in the collection's deaths, which `kinds.death_error` reads."""
    box = mounted(mount_node, settings)
    try:
        proc = mount_node.procs[box._mount.oneclient.process.pid]
        proc.alive, proc.exit_status = False, 1
        mount_node.mounts.discard(mount_node.mountpoint)
        check = box.infra_check()
        assert roles(check) == ["mount"]
        assert any("exit status 1" in line for line in check["death"])
        assert any("absent" in line for line in check["death"])
        assert roles({"deaths": box.collect(tmp_path / "out")["deaths"]}) == ["mount"]
    finally:
        mount_node.mounts.discard(mount_node.mountpoint)
        box.stop()


def test_o17_a_mount_gone_from_the_table_while_oneclient_lives_is_a_death(mount_node, settings):
    """O17: oneclient alive is not enough: its mount must still be at the mount point as the host mount made it."""
    box = mounted(mount_node, settings)
    try:
        mount_node.mounts.discard(mount_node.mountpoint)
        check = box.infra_check()
        assert roles(check) == ["mount"]
        assert check["containers"]["mount"]["end"] == {"running": True, "exit_status": None, "mount": "absent"}
    finally:
        box.stop()


def test_o18_mount_errors_and_mount_log_read_oneclients_own_log_from_the_host_mount_with_the_token_redacted(
    mount_node, settings
):
    """O18: oneclient's INFO log in the host mount's log directory, read as Docker's sidecar log is read: its text
    with the token replaced (`mount_log`), and the same summary of it (`mount_errors`) a study's `platform_errors`
    counts."""
    box = mounted(mount_node, settings)
    try:
        text = LOG + f"I1111 10:00:00.000000 1 options.cc:1] token {TOKEN}\n"
        (box._mount.where.log_dir / "oneclient.INFO").write_text(text, encoding="utf-8")
        redacted = text.replace(TOKEN, "<token>")
        assert box.mount_log() == {"text": redacted}
        assert box.mount_errors() == oneclient_log.summarize(redacted)
        assert TOKEN not in json.dumps(box.mount_errors())
        assert oneclient_log.platform_errors(box.mount_errors()) > 0
    finally:
        box.stop()


def test_o18_a_mount_log_that_cannot_be_read_is_unavailable_never_an_empty_summary(mount_node, settings):
    """O18: no log in the host mount's log directory is unavailable, by both calls, never an empty count."""
    box = mounted(mount_node, settings)
    try:
        assert (box._mount.where.log_dir / "oneclient.INFO").exists() is False
        assert box.mount_errors() == {"unavailable": "the oneclient log could not be read"}
        assert box.mount_log() == box.mount_errors()
    finally:
        box.stop()


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o18_a_run_without_a_mount_has_no_mount_reading(node, settings, level):
    """O18: no mount, no reading: None from both, as on Docker."""
    box = start(node, settings, make_spec(level))
    try:
        assert (box.mount_errors(), box.mount_log()) == (None, None)
    finally:
        box.stop()


def test_o17_a_harness_stop_that_lands_during_the_checks_own_read_is_no_death(node, settings):
    """O17: the harness notes a helper's state and then stops it, possibly while a check reads it: a check whose live
    read already sees the stop finds the note, and judges by it, never calling the harness's own stop a death."""
    box = start(node, settings)
    try:
        pid = _process(box, "gate")[0]
        alive, landed = node.alive, []

        def racing(p, s):
            if p == pid and not landed:  # the check's live read of the gate: the collection stops it right then
                landed.append(True)
                box.infra.stopping("gate")
                node.procs[pid].alive, node.procs[pid].exit_status = False, 0
                return False
            return alive(p, s)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(node, "alive", racing)
            check = box.infra_check()
        assert landed
        assert check["deaths"] == []
        assert check["containers"]["gate"]["stopped_by_harness"] is True
    finally:
        box.stop()


def test_o17_a_window_close_that_lands_during_the_checks_own_read_is_no_death(mount_node, settings):
    """O17: the same for the guard's watcher, whose stop is the window's close."""
    box = mounted(mount_node, settings)
    try:
        watcher = mount_node.watch().proc
        alive, landed = mount_node.alive, []

        def racing(p, s):
            if p == watcher.pid and not landed:
                landed.append(True)
                box._mount.close_watch()
                return alive(p, s)
            return alive(p, s)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mount_node, "alive", racing)
            check = box.infra_check()
        assert landed
        assert watcher.alive is False
        assert check["deaths"] == []
        assert check["containers"]["mount_watch"]["stopped_by_harness"] is True
    finally:
        box.stop()
