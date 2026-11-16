"""O60: the mount of a run on an HPC node, oneclient on the node outside every cell with its token in a file no cell
binds, and the guard that watches it; on the fake node, through the helper and through the sandbox."""

import dataclasses
import json
import os
import signal
import stat

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, options, pinned
from fake_mount_node import FakeMountNode
from sandbox_support import SleptClock

from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.oneclient_args import UNSET_ENV
from expcore_onedata.sandbox.settings import MountSettings


@pytest.fixture
def node() -> FakeMountNode:
    return FakeMountNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def binary(tmp_path):
    return pinned(tmp_path / "files", "oneclient", b"an invented oneclient\n")


def helper(node, settings, binary, *, spec=None, **more) -> HM.HostMount:
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    return HM.HostMount(node, settings, spec or make_spec("mounted"), run, oneclient=binary, mountinfo=node.mountinfo,
                        sleep=clock.sleep, clock=clock, **more)  # fmt: skip


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_start_returns_the_mount_point_with_oneclient_mounted_and_the_window_open(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        assert mount.start() == mount.where.mountpoint
        assert node.mounted_at == str(mount.where.mountpoint)
        assert node.watch().armed
    finally:
        mount.stop()


def test_oneclient_reads_the_token_from_a_private_file_that_is_gone_when_start_returns(node, settings, binary):
    """O60: the token reaches oneclient through a 0600 file in a 0700 directory, removed before any cell starts."""
    mount = helper(node, settings, binary)
    try:
        mount.start()
        assert node.oneclient_tokens == [TOKEN]
        assert not os.path.lexists(mount.where.token_file)
        for directory in (mount.where.guard, *mount.where.protected(), mount.where.mountpoint):
            assert mode(directory) == 0o700
    finally:
        mount.stop()


def test_oneclient_and_the_watcher_carry_the_runs_tag_so_a_reaper_finds_them(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        assert node.spawn_env == [{"EXPCORE_SANDBOX": "run-1"}] * 2
    finally:
        mount.stop()


def test_no_argument_of_any_process_holds_the_token(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        for argv in node.spawned:
            assert TOKEN not in " ".join(argv)
    finally:
        mount.stop()


def test_oneclient_runs_read_only_without_allow_other_with_every_option_the_core_declares(node, settings, binary):
    """The node has no `user_allow_other`: the mount is this uid's alone, which is every cell's uid."""
    mount = helper(node, settings, binary)
    try:
        mount.start()
        argv = node.oneclient_argv
        where = mount.where
        assert argv[argv.index("-o") + 1] == "ro"
        assert not any("allow_other" in word for word in argv)
        for name in UNSET_ENV:
            assert argv[argv.index(name) - 1] == "-u"
        assert f"HOME={where.home_dir}" in argv
        assert argv[argv.index("--port") + 1] == "8443"
        assert argv[argv.index("--log-dir") + 1] == str(where.log_dir)
        assert argv[argv.index("--config") + 1] == "/dev/null"
        assert "--force-proxy-io" in argv
        assert "--disable-log-buffering" in argv
        assert argv[-1] == str(where.mountpoint)
        assert argv[argv.index("-f") - 1] == str(binary.path)
    finally:
        mount.stop()


def test_the_watcher_starts_after_the_mount_and_watches_the_token_and_the_four_private_directories(
    node, settings, binary
):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        watch = node.watch()
        assert watch.token == mount.where.token_file
        assert watch.protect == [str(path) for path in mount.where.protected()]
        assert watch.nonce == mount.nonce
    finally:
        mount.stop()


def test_no_protected_directory_lies_under_the_mount_point_or_holds_it(node, settings, binary):
    where = helper(node, settings, binary).where
    for path in where.protected():
        assert not str(path).startswith(str(where.mountpoint) + "/")
        assert not str(where.mountpoint).startswith(str(path) + "/")


def test_a_direct_mount_is_refused(node, tmp_path, binary):
    settings = make_settings(tmp_path)
    direct = MountSettings("/data", "direct", storages=(("st1", "/invented/storage"),), direct_check_file="sp/f")
    mount = helper(node, dataclasses.replace(settings, mount=direct), binary)
    with pytest.raises(StartRefused, match="direct"):
        mount.start()
    assert node.spawned == []


def test_an_extra_argument_that_sets_the_port_is_refused(node, tmp_path, binary):
    settings = make_settings(tmp_path)
    extra = MountSettings("/data", "proxy", extra_args=("-P", "1"))
    with pytest.raises(StartRefused, match="port"):
        helper(node, dataclasses.replace(settings, mount=extra), binary).start()


def test_a_binary_that_is_not_the_pinned_file_is_refused(node, settings, tmp_path):
    other = pinned(tmp_path / "files", "oneclient-other", b"x\n")
    other.path.write_bytes(b"changed\n")
    with pytest.raises(StartRefused, match="pinned"):
        helper(node, settings, other).start()
    assert node.spawned == []


def test_oneclient_that_ends_before_its_mount_fails_the_start_and_the_stop_removes_the_token(node, settings, binary):
    node.oneclient_fails = True
    mount = helper(node, settings, binary)
    with pytest.raises(SandboxError, match="ended before its mount"):
        mount.start()
    mount.stop()
    assert not os.path.lexists(mount.where.token_file)


def test_a_mount_that_never_appears_fails_the_start(node, settings, binary):
    node.mount_never = True
    mount = helper(node, settings, binary)
    with pytest.raises(SandboxError, match="did not appear"):
        mount.start()
    mount.stop()


def test_stop_closes_the_window_unmounts_and_shows_oneclient_gone_and_is_safe_to_call_again(node, settings, binary):
    mount = helper(node, settings, binary)
    mount.start()
    mount.stop()
    assert node.mounted_at is None
    assert node.unmounts[0][:2] == ["fusermount3", "-u"]
    assert (node.watch().proc.pid, signal.SIGTERM) in node.signals
    assert not node.watch().proc.alive
    mount.stop()


def test_a_mount_that_stays_in_the_table_fails_the_stop_naming_it(node, settings, binary):
    mount = helper(node, settings, binary)
    mount.start()
    node.stuck_mount = True
    with pytest.raises(SandboxError, match="mount"):
        mount.stop()


def test_a_run_without_access_and_with_a_proved_cell_is_clean_in_the_metadata(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        before = mount.metadata()
        assert before["guard"]["verdict"] == "not_collected"
        assert before["io_mode"]["effective"] == "unknown"
        node.cell()
        mount.close_watch()
        after = mount.metadata()
        assert after["guard"]["verdict"] == "clean"
        assert after["guard"]["snapshot"]["accesses"] == {"token_file": 0, "token_dir": 0}
    finally:
        mount.stop()


def test_a_touch_of_the_token_directory_flags_the_run(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        node.cell()
        node.touch()
        assert b"IN_OPEN" in mount.mount_watch_log()  # read lazily: the window closes first
        assert mount.metadata()["guard"]["verdict"] == "flagged"
    finally:
        mount.stop()


def test_a_run_whose_cells_were_never_proved_is_unknown(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        mount.close_watch()
        assert mount.metadata()["guard"]["verdict"] == "unknown"
    finally:
        mount.stop()


def test_the_metadata_holds_no_token_and_no_path(node, settings, binary):
    mount = helper(node, settings, binary)
    try:
        mount.start()
        node.cell()
        mount.close_watch()
        text = json.dumps(mount.metadata())
        assert TOKEN not in text
        assert str(settings.work_root) not in text
        assert str(binary.path) not in text
    finally:
        mount.stop()


def test_a_log_file_of_oneclient_that_holds_the_token_is_removed_at_the_stop(node, settings, binary):
    mount = helper(node, settings, binary)
    mount.start()
    leaked = mount.where.log_dir / "oneclient.INFO"
    leaked.write_text(f"a line with {TOKEN}\n")
    mount.stop()
    assert not leaked.exists()
    assert mount.metadata()["log_files_scrubbed"] == 1


def test_through_the_sandbox_every_cell_binds_the_mount_point_alone_and_the_collection_closes_the_window(
    node, settings, binary, tmp_path
):
    factory = HM.host_mount_factory(binary, mountinfo=node.mountinfo)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    try:
        mount = box._mount
        guard = str(mount.where.guard)
        for done in node.execs:
            assert done.binds["/data"] == (str(mount.where.mountpoint), False)
            assert not any(source.startswith(guard) for source, _ in done.binds.values())
            assert TOKEN not in " ".join(done.argv)
        assert box.mount_guard_nonce == mount.nonce
        node.cell()
        result = box.collect(tmp_path / "out")
        assert result["mount_watch_log"] == "mount-watch.jsonl"
        assert (tmp_path / "out" / "mount-watch.jsonl").read_bytes() == box.mount_watch_log()
        assert box.metadata()["mount"]["guard"]["verdict"] == "clean"
        assert box.mount_io_mode()["mount"] is True
    finally:
        box.stop()
    assert node.mounted_at is None
