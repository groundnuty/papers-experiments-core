"""O97: the host mount's connection and its generations, as the guard, the I/O-mode reading and the diagnostics read
them: the device and the fusectl state of the live connection, one watcher log and one oneclient log per generation,
and what the run's metadata says of both. Fake node, fake mount table, a fusectl tree in the test's own directory."""

import pytest
from apptainer_support import make_settings, make_spec, options, pinned_bundle
from fake_mount_node import FUSE_DEV, FakeMountNode, fusectl_tree
from sandbox_support import SleptClock

from expcore_onedata.sandbox import directio
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox


@pytest.fixture
def node() -> FakeMountNode:
    return FakeMountNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))


@pytest.fixture
def fusectl_root(tmp_path):
    root = tmp_path / "fusectl"
    root.mkdir()
    return root


def mount_of(node, settings, fusectl_root) -> HM.HostMount:
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    return HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                        clock=clock, fusectl_root=fusectl_root)  # fmt: skip


def test_o97_before_the_mount_is_up_its_connection_is_unknown(node, settings, fusectl_root):
    mount = mount_of(node, settings, fusectl_root)
    assert mount.connection() == {"device": None, "fusectl": None}


def test_o97_the_start_records_the_connections_device_and_a_writable_fusectl(node, settings, fusectl_root):
    fusectl_tree(fusectl_root, FUSE_DEV)
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        assert mount.connection() == {"device": FUSE_DEV, "fusectl": "writable"}
        assert mount.metadata()["fusectl"] == "writable"
    finally:
        mount.stop()


def test_o97_a_node_with_no_fusectl_directory_for_the_connection_records_absent(node, settings, fusectl_root):
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        assert mount.connection() == {"device": FUSE_DEV, "fusectl": "absent"}
        assert mount.metadata()["fusectl"] == "absent"
    finally:
        mount.stop()


def test_o97_a_connection_whose_abort_file_the_owner_cannot_use_records_not_writable(node, settings, fusectl_root):
    fusectl_tree(fusectl_root, FUSE_DEV, abort=False)
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        assert mount.connection()["fusectl"] == "not_writable"
    finally:
        mount.stop()


def test_o97_the_metadata_of_a_mount_never_restored_names_one_generation_and_no_remount(node, settings, fusectl_root):
    fusectl_tree(fusectl_root, FUSE_DEV)
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        meta = mount.metadata()
        assert meta["remounts"] == 0
        assert meta["generations"] == [{"wait_s": meta["wait_s"], "fusectl": "writable", "abort": None}]
    finally:
        mount.stop()


def test_o97_a_mount_with_one_generation_gives_its_one_log_in_both_forms(node, settings, fusectl_root):
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        node.cell()
        logs = mount.mount_watch_logs()
        assert len(logs) == 1
        assert logs[0] is not None
        assert mount.mount_watch_log() == logs[0]
        assert mount.metadata()["guard"]["verdict"] == "clean"
    finally:
        mount.stop()


def test_o97_a_mount_with_one_generation_gives_its_one_oneclient_log(node, settings, fusectl_root):
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        (mount.where.log_dir / directio.LOG_FILE).write_text("I start line\n")
        assert mount.generation_oneclient_logs() == [b"I start line\n"]
        assert mount.oneclient_log() == b"I start line\n"
    finally:
        mount.stop()


def test_o97_a_log_of_a_generation_that_cannot_be_read_is_none_never_empty(node, settings, fusectl_root):
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        assert mount.generation_oneclient_logs() == [None]
        assert mount.oneclient_log() is None
    finally:
        mount.stop()


def test_o97_the_first_generation_has_the_roles_and_the_log_name_a_run_never_restored_always_had(
    node, settings, fusectl_root
):
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        assert set(mount.infra_state()) == {"mount", "mount_watch"}
        assert mount.where.watch_log == mount.where.watch_log_of(1)
        assert mount.where.watch_log_of(1).name == "mountwatch.jsonl"
        assert mount.where.watch_log_of(2).name == "mountwatch-2.jsonl"
    finally:
        mount.stop()


def test_o97_the_sandbox_gives_the_plural_logs_of_its_mount(node, settings, fusectl_root):
    factory = HM.host_mount_factory(mountinfo=node.mountinfo, fusectl_root=fusectl_root)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    try:
        node.cell()
        logs = box.mount_watch_logs()
        assert len(logs) == 1
        assert logs[0] is not None
        assert box.mount_watch_log() == logs[0]
    finally:
        box.stop()


def test_o97_a_sandbox_with_no_mount_has_no_mount_watch_logs(node, settings):
    box = ApptainerSandbox.start(node, settings, make_spec("isolated"), **options())
    try:
        assert box.mount_watch_logs() == []
    finally:
        box.stop()


def test_o97_the_live_oneclient_log_is_read_at_each_call_not_kept_from_the_windows_close(node, settings, fusectl_root):
    """O97: a run that is never restored reads oneclient's log as it always did: a later write shows."""
    mount = mount_of(node, settings, fusectl_root)
    try:
        mount.start()
        log = mount.where.log_dir / directio.LOG_FILE
        log.write_text("before the close\n")
        mount.close_watch()
        log.write_text("after the close\n")
        assert mount.oneclient_log() == b"after the close\n"
        assert mount.generation_oneclient_logs() == [b"after the close\n"]
    finally:
        mount.stop()
