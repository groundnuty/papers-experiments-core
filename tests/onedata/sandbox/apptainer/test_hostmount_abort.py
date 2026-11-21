"""O97: `HostMount.abort`, the one action that frees a cell blocked in a read of the mount. The mount's owner writes `1`
to its own connection's fusectl `abort`, or, without that, SIGKILLs the run's own oneclient by pid and start time. Only
that freeing action runs inside the deadline's margin: oneclient's orderly stop and the unmount are the restore's. Fake
node, fake mount table, a fusectl tree in the test's own directory; no real mount."""

import os
import signal

import pytest
from apptainer_support import make_settings, make_spec, pinned_bundle
from fake_mount_node import BASE_TABLE, FUSE_DEV, FakeMountNode, fusectl_tree
from sandbox_support import SleptClock

from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer import infra


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


@pytest.fixture
def mounted(node, settings, fusectl_root):
    """A started host mount whose connection has a fusectl directory with two requests waiting."""
    fusectl_tree(fusectl_root, FUSE_DEV, waiting=2)
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                         clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    mount.start()
    yield mount
    mount.stop()


def abort_file(fusectl_root):
    return fusectl_root / FC.conn(FUSE_DEV) / "abort"


def kernel_ends_the_requests(monkeypatch, fusectl_root):
    """What the kernel does at the write: no request is waiting any more."""
    real = FC.write_abort

    def write(root, connection):
        real(root, connection)
        (root / connection / "waiting").write_text("0\n")

    monkeypatch.setattr(FC, "write_abort", write)


def test_o97_the_abort_writes_one_to_the_fusectl_abort_of_the_runs_own_connection(mounted, node, fusectl_root):
    record = mounted.abort(30.0)
    assert abort_file(fusectl_root).read_bytes() == b"1"
    assert record["ok"] is True
    assert record["method"] == "fusectl"


def test_o97_the_fusectl_abort_signals_nothing_so_oneclients_orderly_stop_is_the_restores(mounted, node):
    """O97: only the freeing write runs inside the margin; the unmount and oneclient's stop run later."""
    mounted.abort(30.0)
    assert node.signals == []
    assert node.unmounts == []
    assert node.mounted_at is not None


def test_o97_the_record_has_the_waiting_counts_before_and_after_and_the_seconds(mounted, monkeypatch, fusectl_root):
    kernel_ends_the_requests(monkeypatch, fusectl_root)
    record = mounted.abort(30.0)
    assert record["waiting_before"] == 2
    assert record["waiting_after"] == 0
    assert record["abort_s"] >= 0
    assert set(record) == {"ok", "method", "waiting_before", "waiting_after", "abort_s"}


def test_o97_nothing_is_written_when_the_mount_table_shows_another_device_at_the_point(
    mounted, node, fusectl_root, monkeypatch
):
    """O97: a connection number is reused after an unmount; the identity is the device the mount came up with."""
    mountpoint, owner = str(mounted.where.mountpoint), f"user_id={os.getuid()},group_id={os.getgid()}"
    other = f"3 1 0:77 / {mountpoint} ro,nosuid shared:3 - fuse.oneclient oneclient rw,{owner}\n"
    monkeypatch.setattr(mounted, "_mountinfo", lambda: BASE_TABLE + other)
    record = mounted.abort(30.0)
    monkeypatch.undo()
    assert abort_file(fusectl_root).read_bytes() == b""
    assert record["method"] == "daemon_kill"
    assert record["fusectl_skipped"] == "identity_changed"


def test_o97_nothing_is_written_when_the_mount_at_the_point_is_not_this_uids(mounted, node, fusectl_root, monkeypatch):
    mountpoint = str(mounted.where.mountpoint)
    foreign = (
        f"3 1 {FUSE_DEV} / {mountpoint} ro,nosuid shared:3 - fuse.oneclient oneclient rw,user_id={os.getuid() + 1}\n"
    )
    monkeypatch.setattr(mounted, "_mountinfo", lambda: BASE_TABLE + foreign)
    record = mounted.abort(30.0)
    monkeypatch.undo()
    assert abort_file(fusectl_root).read_bytes() == b""
    assert record["fusectl_skipped"] == "identity_changed"


def test_o97_without_fusectl_oneclient_gets_sigkill_by_pid_and_start_and_the_abort_is_ok_once_it_is_gone(
    node, settings, fusectl_root
):
    run = settings.work_root / "run-2"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                         clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    mount.start()
    try:
        assert mount.connection()["fusectl"] == "absent"
        process = mount.oneclient.process
        record = mount.abort(30.0)
        assert node.signals == [(process.pid, signal.SIGKILL)]
        assert node.alive(process.pid, process.start) is False
        assert record["ok"] is True
        assert record["method"] == "daemon_kill"
        assert record["waiting_before"] is None
        assert record["waiting_after"] is None
    finally:
        mount.stop()


def test_o97_a_connection_the_owner_may_not_write_is_aborted_by_the_kill_too(node, settings, fusectl_root):
    fusectl_tree(fusectl_root, FUSE_DEV, abort=False)
    run = settings.work_root / "run-3"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                         clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    mount.start()
    try:
        assert mount.connection()["fusectl"] == "not_writable"
        record = mount.abort(30.0)
        assert record["method"] == "daemon_kill"
        assert "fusectl_skipped" not in record  # it was never tried: the start found it unusable
    finally:
        mount.stop()


def test_o97_a_fusectl_write_that_fails_falls_back_to_the_kill(mounted, node, monkeypatch):
    def refused(root, connection):
        raise PermissionError("invented refusal")

    monkeypatch.setattr(FC, "write_abort", refused)
    record = mounted.abort(30.0)
    assert record["ok"] is True
    assert record["method"] == "daemon_kill"
    assert record["fusectl_skipped"] == "write_failed"
    assert "invented" not in str(record)  # no error text of the node reaches the record


def test_o97_a_oneclient_that_stays_after_the_kill_is_not_ok_and_the_wait_ends_at_the_margin_left(
    node, settings, fusectl_root, monkeypatch
):
    run = settings.work_root / "run-4"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                         clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    mount.start()
    try:
        monkeypatch.setattr(node, "signal", lambda pid, start, sig: True)  # sent, and the process is in D: it stays
        began = clock()
        record = mount.abort(4.0)
        assert record["ok"] is False
        assert record["why"] == "oneclient did not end within the time left"
        assert 4.0 <= clock() - began < 5.0
    finally:
        monkeypatch.undo()
        mount.stop()


def test_o97_a_signal_that_cannot_be_sent_is_not_ok_and_says_so(node, settings, fusectl_root, monkeypatch):
    run = settings.work_root / "run-5"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                         clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    mount.start()
    try:
        monkeypatch.setattr(node, "signal", lambda pid, start, sig: None)
        record = mount.abort(30.0)
        assert record["ok"] is False
        assert record["why"] == "oneclient could not be signalled"
    finally:
        monkeypatch.undo()
        mount.stop()


def test_o97_an_abort_with_no_time_left_does_nothing(mounted, node, fusectl_root):
    record = mounted.abort(0.0)
    assert record["ok"] is False
    assert record["why"] == "no time was left"
    assert abort_file(fusectl_root).read_bytes() == b""
    assert node.signals == []


def test_o97_an_abort_never_raises_even_when_the_node_does(mounted, node, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("invented failure with /some/path")

    monkeypatch.setattr(FC, "write_abort", broken)
    monkeypatch.setattr(node, "signal", broken)
    record = mounted.abort(30.0)
    monkeypatch.undo()
    assert record["ok"] is False
    assert record["why"] == "the abort raised"
    assert "/some/path" not in str(record)


def test_o97_an_abort_of_a_mount_whose_oneclient_never_started_is_not_attempted(node, settings, fusectl_root):
    run = settings.work_root / "run-6"
    run.mkdir(mode=0o700)
    mount = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, fusectl_root=fusectl_root)
    record = mount.abort(30.0)
    assert record["ok"] is None


def test_o97_oneclients_state_is_noted_before_the_abort_so_its_end_is_never_read_as_a_death(mounted, node):
    """O97: the kill path ends oneclient; `infra_check` must read it as the harness's own end."""
    record = mounted.abort(30.0)  # no fusectl tree for another device: this one is writable, so no kill yet
    assert record["method"] == "fusectl"
    process = mounted.oneclient.process
    node.signal(process.pid, process.start, signal.SIGKILL)  # oneclient ends on its own after an abort
    state = mounted.infra_state()["mount"]
    assert state["stopped_by_harness"] is True
    assert state["end"]["running"] is True
    assert state["end"]["mount"] is None
    assert infra.problems("mount", "c", state["end"]) == []


def test_o97_the_abort_is_in_the_generations_metadata_and_the_mount_is_not_yet_a_remount(mounted):
    record = mounted.abort(30.0)
    meta = mounted.metadata()
    assert meta["generations"][0]["abort"] == record
    assert meta["remounts"] == 0
