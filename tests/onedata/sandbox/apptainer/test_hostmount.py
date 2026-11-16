"""O60: the mount of a run on an HPC node, oneclient on the node outside every cell with its token in a file no cell
binds, and the guard that watches it; on the fake node, through the helper and through the sandbox."""

import dataclasses
import json
import os
import signal
import stat

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, options, pinned_bundle
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
def bundle(tmp_path):
    """oneclient's bundle as the platform pins it (`[sandbox.apptainer] oneclient_bundle`): an invented tar."""
    return pinned_bundle(tmp_path / "files")


@pytest.fixture
def settings(tmp_path, bundle):
    return make_settings(tmp_path, oneclient_bundle=bundle)


def helper(node, settings, *, spec=None, **more) -> HM.HostMount:
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    return HM.HostMount(node, settings, spec or make_spec("mounted"), run, mountinfo=node.mountinfo,
                        sleep=clock.sleep, clock=clock, **more)  # fmt: skip


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_start_returns_the_mount_point_with_oneclient_mounted_and_the_window_open(node, settings):
    mount = helper(node, settings)
    try:
        assert mount.start() == mount.where.mountpoint
        assert node.mounted_at == str(mount.where.mountpoint)
        assert node.watch().armed
    finally:
        mount.stop()


def test_oneclient_reads_the_token_from_a_private_file_that_is_gone_when_start_returns(node, settings):
    """O60: the token reaches oneclient through a 0600 file in a 0700 directory, removed before any cell starts."""
    mount = helper(node, settings)
    try:
        mount.start()
        assert node.oneclient_tokens == [TOKEN]
        assert not os.path.lexists(mount.where.token_file)
        for directory in (mount.where.guard, *mount.where.protected(), mount.where.mountpoint):
            assert mode(directory) == 0o700
    finally:
        mount.stop()


def test_oneclient_and_the_watcher_carry_the_runs_tag_so_a_reaper_finds_them(node, settings):
    mount = helper(node, settings)
    try:
        mount.start()
        assert node.spawn_env == [{"EXPCORE_SANDBOX": "run-1"}] * 2
    finally:
        mount.stop()


def test_no_argument_of_any_process_holds_the_token(node, settings):
    mount = helper(node, settings)
    try:
        mount.start()
        for argv in node.spawned:
            assert TOKEN not in " ".join(argv)
    finally:
        mount.stop()


def test_oneclient_runs_read_only_without_allow_other_with_every_option_the_core_declares(node, settings):
    """The node has no `user_allow_other`: the mount is this uid's alone, which is every cell's uid."""
    mount = helper(node, settings)
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
        at = argv.index("-f")
        assert argv[at - len(mount.bundle.launcher()) : at] == mount.bundle.launcher()  # the bundle's, from its copy
    finally:
        mount.stop()


def test_the_watcher_starts_after_the_mount_and_watches_the_token_and_the_five_private_directories(node, settings):
    mount = helper(node, settings)
    try:
        mount.start()
        watch = node.watch()
        assert watch.token == mount.where.token_file
        assert watch.protect == [str(path) for path in mount.where.protected()]
        assert watch.nonce == mount.nonce
    finally:
        mount.stop()


def test_no_protected_directory_lies_under_the_mount_point_or_holds_it(node, settings):
    where = helper(node, settings).where
    for path in where.protected():
        assert not str(path).startswith(str(where.mountpoint) + "/")
        assert not str(where.mountpoint).startswith(str(path) + "/")


def test_a_direct_mount_is_refused(node, tmp_path):
    settings = make_settings(tmp_path)
    direct = MountSettings("/data", "direct", storages=(("st1", "/invented/storage"),), direct_check_file="sp/f")
    mount = helper(node, dataclasses.replace(settings, mount=direct))
    with pytest.raises(StartRefused, match="direct"):
        mount.start()
    assert node.spawned == []


def test_an_extra_argument_that_sets_the_port_is_refused(node, tmp_path):
    settings = make_settings(tmp_path)
    extra = MountSettings("/data", "proxy", extra_args=("-P", "1"))
    with pytest.raises(StartRefused, match="port"):
        helper(node, dataclasses.replace(settings, mount=extra)).start()


def test_a_bundle_that_is_not_the_pinned_file_is_refused_before_the_token_is_written(node, settings, bundle):
    with open(bundle.path, "ab") as handle:
        handle.write(b"changed\n")
    mount = helper(node, settings)
    with pytest.raises(StartRefused, match="pinned"):
        mount.start()
    assert node.spawned == []
    assert not os.path.lexists(mount.where.token_file)
    assert os.listdir(mount.where.bundle_dir) == []


def test_a_platform_that_declares_no_bundle_is_refused(node, settings):
    """O65: oneclient is the platform's declared bundle, never a binary found on the node."""
    bare = dataclasses.replace(settings, apptainer=dataclasses.replace(settings.apptainer, oneclient_bundle=None))
    with pytest.raises(StartRefused, match="oneclient_bundle"):
        helper(node, bare).start()
    assert node.spawned == []


def test_the_run_records_the_oneclient_that_served_it(node, settings, bundle):
    """O65: its own --version, run on the node through the bundle's launcher, the binary's GNU build id, the pin."""
    mount = helper(node, settings)
    try:
        mount.start()
        assert node.version_runs[-1][-1] == "--version"
        assert node.version_runs[-1][-2] == str(mount.bundle.binary)
        assert mount.metadata()["oneclient_build"] == {
            "version": "25.0", "fuse": "3.2", "build_id": "ab" * 20, "bundle_sha256": bundle.sha256,
        }  # fmt: skip
    finally:
        mount.stop()


def test_a_bundle_whose_oneclient_does_not_name_its_version_fails_the_start_before_the_token_is_written(node, settings):
    node.oneclient_version = "Segmentation fault\n"
    mount = helper(node, settings)
    with pytest.raises(SandboxError, match="version"):
        mount.start()
    assert node.spawned == []
    assert not os.path.lexists(mount.where.token_file)


def test_the_stop_removes_the_bundle_s_copy_once_oneclient_is_gone(node, settings):
    mount = helper(node, settings)
    mount.start()
    assert (mount.where.bundle_dir / "bin" / "oneclient").exists()
    mount.stop()
    assert not mount.where.bundle_dir.exists()


def test_the_factory_refuses_a_oneclient_given_in_code():
    """O65: a binary given to the factory would serve runs the campaign fingerprint does not tell apart."""
    with pytest.raises(TypeError, match="oneclient_bundle"):
        HM.host_mount_factory(object())
    with pytest.raises(TypeError, match="oneclient_bundle"):
        HM.host_mount_factory(oneclient=object())


def test_oneclient_that_ends_before_its_mount_fails_the_start_and_the_stop_removes_the_token(node, settings):
    node.oneclient_fails = True
    mount = helper(node, settings)
    with pytest.raises(SandboxError, match="ended before its mount"):
        mount.start()
    mount.stop()
    assert not os.path.lexists(mount.where.token_file)


def test_a_mount_that_never_appears_fails_the_start(node, settings):
    node.mount_never = True
    mount = helper(node, settings)
    with pytest.raises(SandboxError, match="did not appear"):
        mount.start()
    mount.stop()


def test_stop_closes_the_window_unmounts_and_shows_oneclient_gone_and_is_safe_to_call_again(node, settings):
    mount = helper(node, settings)
    mount.start()
    mount.stop()
    assert node.mounted_at is None
    assert node.unmounts[0][:2] == ["fusermount3", "-u"]
    assert (node.watch().proc.pid, signal.SIGTERM) in node.signals
    assert not node.watch().proc.alive
    mount.stop()


def test_a_mount_that_stays_in_the_table_fails_the_stop_naming_it(node, settings):
    mount = helper(node, settings)
    mount.start()
    node.stuck_mount = True
    with pytest.raises(SandboxError, match="mount"):
        mount.stop()


def test_a_run_without_access_and_with_a_proved_cell_is_clean_in_the_metadata(node, settings):
    mount = helper(node, settings)
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


def test_the_io_mode_is_read_through_glogs_link_to_oneclients_log(node, settings):
    """oneclient.INFO is glog's link to the current log file of the same directory: followed there, never elsewhere."""
    mount = helper(node, settings)
    try:
        mount.start()
        lines = "I1116 10:00:00.000000  1 logging.cc:1] Forced direct IO: 0\n"
        lines += "I1116 10:00:00.000000  1 logging.cc:1] Forced proxy IO: 1\n"
        (mount.where.log_dir / "oneclient.host.log.INFO.1").write_text(lines)
        (mount.where.log_dir / "oneclient.INFO").symlink_to("oneclient.host.log.INFO.1")
        node.cell()
        mount.close_watch()
        assert mount.metadata()["io_mode"]["effective"] == "proxy"
    finally:
        mount.stop()


def test_a_log_link_that_leaves_the_log_directory_is_not_followed(node, settings, tmp_path):
    outside = tmp_path / "elsewhere.INFO"
    outside.write_text("I1116 10:00:00.000000  1 logging.cc:1] Forced proxy IO: 1\n")
    mount = helper(node, settings)
    try:
        mount.start()
        (mount.where.log_dir / "oneclient.INFO").symlink_to(outside)
        mount.close_watch()
        assert mount.metadata()["io_mode"]["effective"] == "unknown"
    finally:
        mount.stop()


def test_a_touch_of_the_token_directory_flags_the_run(node, settings):
    mount = helper(node, settings)
    try:
        mount.start()
        node.cell()
        node.touch()
        assert b"IN_OPEN" in mount.mount_watch_log()  # read lazily: the window closes first
        assert mount.metadata()["guard"]["verdict"] == "flagged"
    finally:
        mount.stop()


def test_a_run_whose_cells_were_never_proved_is_unknown(node, settings):
    mount = helper(node, settings)
    try:
        mount.start()
        mount.close_watch()
        assert mount.metadata()["guard"]["verdict"] == "unknown"
    finally:
        mount.stop()


def test_the_metadata_holds_no_token_and_no_path(node, settings, bundle):
    mount = helper(node, settings)
    try:
        mount.start()
        node.cell()
        mount.close_watch()
        text = json.dumps(mount.metadata())
        assert TOKEN not in text
        assert str(settings.work_root) not in text
        assert str(bundle.path) not in text
    finally:
        mount.stop()


def test_a_log_file_of_oneclient_that_holds_the_token_is_removed_at_the_stop(node, settings):
    mount = helper(node, settings)
    mount.start()
    leaked = mount.where.log_dir / "oneclient.INFO"
    leaked.write_text(f"a line with {TOKEN}\n")
    mount.stop()
    assert not leaked.exists()
    assert mount.metadata()["log_files_scrubbed"] == 1


def test_through_the_sandbox_every_cell_binds_the_mount_point_alone_and_the_collection_closes_the_window(
    node, settings, tmp_path
):
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
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


def test_the_sandbox_s_metadata_names_the_oneclient_that_served_the_run_and_the_backend_s_pin(node, settings, bundle):
    """O65: what a study keeps of a run's sandbox names its oneclient (version, build id) and the bundle's pin."""
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    try:
        meta = box.metadata()
        assert meta["mount"]["oneclient_build"]["version"] == "25.0"
        assert meta["mount"]["oneclient_build"]["build_id"] == "ab" * 20
        assert meta["mount"]["oneclient_build"]["bundle_sha256"] == bundle.sha256
        assert meta["backend"]["oneclient_bundle_sha256"] == bundle.sha256
    finally:
        box.stop()


def test_the_run_record_lists_the_mounts_token_file_before_the_start_writes_it(node, settings):
    """O60 (M5's review): the reaper removes the token files the record lists before anything else it does."""
    from expcore_onedata.sandbox.apptainer import runs

    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    box = ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    try:
        document = json.loads(runs.record_path(settings.work_root, box.sid).read_text())
        assert document["token_files"] == ["mount-guard/token/token"]
    finally:
        box.stop()


def test_a_dead_drivers_mount_run_whose_unmount_fails_still_has_its_token_file_removed_by_the_reaper(node, settings):
    """O60 (M5's review): a driver killed outright while the token file was on disk, and a mount no unmount takes:
    the next start's reaper removes the token file first, kills oneclient and the watcher by the run's tag, and keeps
    the run for a later reap since its mount stays."""
    from expcore_onedata.sandbox.apptainer import runs
    from expcore_onedata.sandbox.apptainer.node import Spawned

    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    old = ApptainerSandbox.start(
        node, settings, make_spec("mounted", lease_key="slot-a"), **options(mount_helper=factory)
    )
    token = old._mount.where.token_file
    token.write_text(TOKEN)  # the file as the start had it on disk, before its removal
    node.stuck_mount = True
    node.procs[node.owner.pid].alive = False  # the driver is gone, its teardown never run
    node.owner = Spawned(1001, "5151")
    node.procs[1001] = type(node.procs[999])(1001, "5151", "pid:[4026531836]", "driver", role="driver")
    fresh = start_box(node, settings)
    try:
        assert not os.path.lexists(token)
        assert not node.watch().proc.alive
        assert fresh.metadata()["reaped_orphans"]["left"] == 1  # the mount stays: kept for a later reap
        assert runs.record_path(settings.work_root, old.sid).exists()
    finally:
        fresh.stop()


def start_box(node, settings):
    from apptainer_support import start

    return start(node, settings)


def _dead_mount_run(node, settings):
    """A mount run whose driver was killed outright while the token file was on disk."""
    factory = HM.host_mount_factory(mountinfo=node.mountinfo)
    old = ApptainerSandbox.start(
        node, settings, make_spec("mounted", lease_key="slot-a"), **options(mount_helper=factory)
    )
    token = old._mount.where.token_file
    token.write_text(TOKEN)
    node.procs[node.owner.pid].alive = False
    return old, token


def test_the_reaper_removes_the_token_file_even_when_it_cannot_list_the_processes(node, settings):
    """O60 (Codex, delta round 1): the token files are removed before any step of the reap that can fail."""
    from expcore_onedata.sandbox.apptainer import runs

    old, token = _dead_mount_run(node, settings)
    node.proc_unreadable = True
    done = runs.reap(node, settings.work_root, own="another-run")
    assert done["left"] == 1  # the run itself waits for a later reap
    assert not os.path.lexists(token)
    assert runs.record_path(settings.work_root, old.sid).exists()


def test_the_reaper_never_removes_a_path_a_record_names_outside_the_run(node, settings, tmp_path):
    from expcore_onedata.sandbox.apptainer import runs

    old, _ = _dead_mount_run(node, settings)
    outside = settings.work_root / "not-the-runs"
    outside.write_text("kept")
    record = runs.record_path(settings.work_root, old.sid)
    document = json.loads(record.read_text())
    document["token_files"] += ["../not-the-runs", str(outside)]
    record.write_text(json.dumps(document))
    node.proc_unreadable = True
    runs.reap(node, settings.work_root, own="another-run")
    assert outside.read_text() == "kept"


def test_a_mount_helper_that_names_a_token_file_outside_the_run_is_refused_before_it_starts(node, settings):
    """O60 (Codex, delta round 1): the record names a token file relative to the run, and a reaper removes it there."""

    class Escaping:
        started = False

        def __init__(self, run_dir):
            self.run_dir = run_dir

        def token_paths(self):
            return [self.run_dir / ".." / "outside-token"]

        def start(self):
            Escaping.started = True
            return self.run_dir / "mount"

        def stop(self):
            pass

        def metadata(self):
            return {}

    def factory(node, settings, spec, run_dir):
        return Escaping(run_dir)

    with pytest.raises(SandboxError, match="outside the run's directory"):
        ApptainerSandbox.start(node, settings, make_spec("mounted"), **options(mount_helper=factory))
    assert not Escaping.started


def test_the_reaper_never_follows_a_link_inside_the_run_to_a_file_outside_it(node, settings):
    """O60 (Codex, delta round 2): a recorded token path under a directory that is a link leaves the run; the reaper
    walks without following links, so the file outside stays."""
    from expcore_onedata.sandbox.apptainer import runs

    old, _ = _dead_mount_run(node, settings)
    elsewhere = settings.work_root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "token").write_text("kept")
    (old.dir / "link").symlink_to(elsewhere)
    record = runs.record_path(settings.work_root, old.sid)
    document = json.loads(record.read_text())
    document["token_files"] += ["link/token"]
    record.write_text(json.dumps(document))
    node.proc_unreadable = True
    runs.reap(node, settings.work_root, own="another-run")
    assert (elsewhere / "token").read_text() == "kept"
    assert not os.path.lexists(old._mount.where.token_file)  # the run's own is still removed


def test_a_helpers_token_path_through_a_link_out_of_the_run_is_refused(node, settings, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    class Linked:
        def __init__(self, run_dir):
            self.run_dir = run_dir

        def token_paths(self):
            (self.run_dir / "link").symlink_to(elsewhere)
            return [self.run_dir / "link" / "token"]

        def start(self):
            raise AssertionError("never started")

        def stop(self):
            pass

        def metadata(self):
            return {}

    with pytest.raises(SandboxError, match="outside the run's directory"):
        ApptainerSandbox.start(
            node, settings, make_spec("mounted"), **options(mount_helper=lambda n, s, sp, run_dir: Linked(run_dir))
        )


def test_a_helpers_token_path_through_a_link_that_stays_in_the_run_is_refused_too(node, settings):
    """O60 (Codex, delta round 3): the reaper never follows a link, so a token file it could not remove is never
    accepted, whichever way the link points."""

    class Linked:
        def __init__(self, run_dir):
            self.run_dir = run_dir

        def token_paths(self):
            (self.run_dir / "real").mkdir()
            (self.run_dir / "link").symlink_to(self.run_dir / "real")
            return [self.run_dir / "link" / "token"]

        def start(self):
            raise AssertionError("never started")

        def stop(self):
            pass

        def metadata(self):
            return {}

    with pytest.raises(SandboxError, match="through a link"):
        ApptainerSandbox.start(
            node, settings, make_spec("mounted"), **options(mount_helper=lambda n, s, sp, run_dir: Linked(run_dir))
        )
