"""O97: `HostMount.restore`, the next generation of the run's mount after a cell's deadline aborted the old one. The old
generation's watch is closed first, then its oneclient stopped and the dead mount unmounted, and only then is the token
file written again for a new oneclient with a watcher of its own (a log of its own, the run's one nonce), so the
rewritten token file is never inside an open window. A failure removes the token file and raises `SandboxError`.
Fake node, fake mount table, a fusectl tree in the test's own directory; no real mount."""

import json
import os
import signal

import pytest
from apptainer_support import TOKEN, make_settings, make_spec, pinned_bundle
from fake_mount_node import FUSE_DEV, FakeMountNode, fusectl_tree
from sandbox_support import SleptClock

from expcore.mountguard import read_log
from expcore_onedata.sandbox import directio
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer import infra
from expcore_onedata.sandbox.hostops import CmdResult


def device(n: int) -> str:
    """The device of the n-th mount of the fake node (1 is the first)."""
    major, _, minor = FUSE_DEV.partition(":")
    return f"{major}:{int(minor) + n - 1}"


@pytest.fixture
def node() -> FakeMountNode:
    node = FakeMountNode()
    node.keep_mount_on_death = True  # the kernel keeps a dead oneclient's mount in the table until it is unmounted
    return node


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, oneclient_bundle=pinned_bundle(tmp_path / "files"))


@pytest.fixture
def fusectl_root(tmp_path):
    root = tmp_path / "fusectl"
    root.mkdir()
    return root


@pytest.fixture
def mount(node, settings, fusectl_root):
    """A started host mount, aborted through fusectl: the state a restore is called in."""
    fusectl_tree(fusectl_root, device(1), waiting=1)
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    made = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=clock.sleep,
                        clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    made.start()
    yield made
    made.stop()


def aborted(mount):
    assert mount.abort(30.0)["ok"] is True


def lines_of(log: bytes) -> list[dict]:
    return [json.loads(line) for line in log.decode().splitlines()]


# ---------------------------------------------------------------------------------------------------- the generation
def test_o97_a_restore_makes_generation_two_and_says_so(mount):
    aborted(mount)
    answer = mount.restore()
    assert answer["ok"] is True
    assert answer["generation"] == 2
    assert answer["wait_s"] >= 0
    assert mount.metadata()["remounts"] == 1


def test_o97_the_new_mount_is_up_at_the_same_mount_point_as_the_start_made_it(mount, node):
    point = str(mount.where.mountpoint)
    aborted(mount)
    mount.restore()
    assert node.mounted_at == point
    assert node.oneclient_argv[-1] == point
    assert HM.fuse_state(node.mountinfo(), point, os.getuid()) is None  # this uid's, read-only, no allow_other


def test_o97_the_new_oneclient_has_the_old_ones_argv_and_a_token_that_is_there_when_it_starts(mount, node):
    first = list(node.oneclient_argv)
    aborted(mount)
    mount.restore()
    assert node.oneclient_argv == first
    assert node.oneclient_tokens == [TOKEN, TOKEN]


def test_o97_the_token_file_is_gone_and_no_argument_of_any_process_holds_the_token(mount, node):
    aborted(mount)
    mount.restore()
    assert not os.path.lexists(mount.where.token_file)
    for argv in node.spawned:
        assert TOKEN not in " ".join(argv)


def test_o97_the_second_generation_has_a_watcher_log_name_stream_files_and_the_one_run_nonce(mount, node):
    where = mount.where
    aborted(mount)
    mount.restore()
    first, second = node.watches
    assert first.log == where.watch_dir / "mountwatch.jsonl"
    assert second.log == where.watch_dir / "mountwatch-2.jsonl"
    assert first.nonce == second.nonce == mount.nonce
    assert (where.guard / "oneclient-2.stdout").exists()
    assert (where.watch_dir / "mountwatch-2.stdout").exists()


def test_o97_the_new_watcher_watches_oneclient_two_not_oneclient_one(mount, node):
    first = mount.oneclient.process
    aborted(mount)
    mount.restore()
    second = mount.oneclient.process
    assert second != first
    argv = node.watches[1].argv
    assert argv[argv.index("--oneclient") + 1] == f"{second.pid}:{second.start}"


def test_o97_a_third_generation_is_named_by_its_number(mount, node):
    aborted(mount)
    mount.restore()
    assert mount.abort(30.0)["ok"] is True
    assert mount.restore()["generation"] == 3
    assert node.watches[2].log.name == "mountwatch-3.jsonl"
    assert mount.metadata()["remounts"] == 2


# ---------------------------------------------------------------------------------------------------------- the order
def test_o97_the_old_watch_is_closed_before_the_token_file_is_written_again(mount, node, monkeypatch):
    """O97: the rewritten token file is never inside an open window: the old watcher has said `disarmed` first."""
    seen = []
    real = mount._write_token

    def write():
        seen.append(b'"disarmed"' in node.watches[0].log.read_bytes())
        real()

    aborted(mount)
    monkeypatch.setattr(mount, "_write_token", write)
    mount.restore()
    assert seen == [True]


def test_o97_oneclient_one_is_stopped_and_the_dead_mount_unmounted_before_the_new_oneclient_starts(mount, node):
    process = mount.oneclient.process
    aborted(mount)
    mount.restore()
    assert (process.pid, signal.SIGTERM) in node.signals  # a fusectl abort leaves oneclient running: it is stopped
    assert node.unmounts
    assert node.unmounts[0][0] == "fusermount3"
    assert node.mount_count == 2


def test_o97_a_oneclient_that_already_ended_with_the_kill_is_not_signalled_again_but_its_mount_is_unmounted(
    mount, node
):
    process = mount.oneclient.process
    node.signal(process.pid, process.start, signal.SIGKILL)  # the abort's other path: oneclient ended
    node.signals.clear()
    mount.restore()
    assert all(pid != process.pid for pid, _ in node.signals)  # only the old watcher was stopped
    assert node.unmounts
    assert node.mounted_at == str(mount.where.mountpoint)


def test_o97_the_old_generations_watch_log_is_closed_and_intact_and_the_new_one_is_its_own(mount, node):
    aborted(mount)
    mount.restore()
    first, second = (read_log(log.read_bytes(), mount.nonce) for log in (w.log for w in node.watches[:2]))
    assert any(line["event"] == "disarmed" for line in lines_of(node.watches[0].log.read_bytes()))
    assert first.intact
    assert not second.intact  # the live generation's window is still open: it closes at the collection


def test_o97_every_generations_watcher_log_is_given_in_order_and_each_reads_intact(mount, node):
    aborted(mount)
    mount.restore()
    node.cell()
    logs = mount.mount_watch_logs()
    assert [read_log(log, mount.nonce).intact for log in logs] == [True, True]
    assert mount.mount_watch_log() == logs  # the singular form gives the logs in order for a restored mount


# ------------------------------------------------------------------------------------ what is kept per generation
def test_o97_each_generation_has_its_own_device_and_the_fusectl_state_read_at_its_start(mount, node, fusectl_root):
    assert mount.connection() == {"device": device(1), "fusectl": "writable"}
    fusectl_tree(fusectl_root, device(2), waiting=0)
    aborted(mount)
    mount.restore()
    assert mount.connection() == {"device": device(2), "fusectl": "writable"}


def test_o97_a_second_abort_is_checked_and_written_against_the_second_generations_connection(mount, node, fusectl_root):
    """O97: the identity of generation 2 is its own device, not generation 1's."""
    fusectl_tree(fusectl_root, device(2), waiting=1)
    aborted(mount)
    mount.restore()
    record = mount.abort(30.0)
    assert record["method"] == "fusectl"
    assert (fusectl_root / FC.conn(device(2)) / "abort").read_bytes() == b"1"
    assert (fusectl_root / FC.conn(device(1)) / "abort").read_bytes() == b"1"  # the first abort's, once
    assert record["ok"] is True


def test_o97_a_generation_without_a_fusectl_directory_is_aborted_by_the_kill(mount, node):
    aborted(mount)
    mount.restore()
    assert mount.connection()["fusectl"] == "absent"
    assert mount.abort(30.0)["method"] == "daemon_kill"


def test_o97_the_metadata_names_every_generation_with_its_abort_and_the_first_wait(mount):
    aborted(mount)
    mount.restore()
    meta = mount.metadata()
    first, second = meta["generations"]
    assert first["abort"]["method"] == "fusectl"
    assert second["abort"] is None
    assert first["wait_s"] == meta["wait_s"]
    assert second["wait_s"] >= 0
    assert meta["fusectl"] == "writable"


def test_o97_oneclients_log_of_each_generation_is_kept_as_it_was_before_the_next_one_started(mount):
    log = mount.where.log_dir / directio.LOG_FILE
    log.write_text("generation one\n")
    aborted(mount)
    mount.restore()
    log.write_text("generation two\n")  # glog's link now names the newest log
    assert mount.generation_oneclient_logs() == [b"generation one\n", b"generation two\n"]
    assert mount.oneclient_log() == b"generation one\ngeneration two\n"


FORCED_PROXY = (
    "I1116 10:00:00.000000  1 logging.cc:1] Forced direct IO: 0\n"
    "I1116 10:00:00.000000  1 logging.cc:1] Forced proxy IO: 1\n"
)


def test_o97_a_generation_that_wrote_no_log_does_not_pass_on_the_io_mode_of_the_one_before(mount):
    """O97 (Codex, final review): glog's link keeps naming the old file until the new oneclient logs; a second
    generation that logged nothing read generation one's lines, so its mode was never judged. It is unknown."""
    log_dir = mount.where.log_dir
    (log_dir / "oneclient.host.log.INFO.1").write_text(FORCED_PROXY)
    (log_dir / "oneclient.INFO").symlink_to("oneclient.host.log.INFO.1")
    aborted(mount)
    mount.restore()  # generation two logs nothing: the link still names generation one's file
    mount.close_watch()
    assert mount.metadata()["io_mode"]["effective"] == "unknown"


def test_o97_a_generation_that_wrote_its_own_log_is_judged_by_it(mount):
    """O97: the same restore, when generation two's oneclient wrote a log of its own and glog's link moved to it."""
    log_dir = mount.where.log_dir
    (log_dir / "oneclient.host.log.INFO.1").write_text(FORCED_PROXY)
    (log_dir / "oneclient.INFO").symlink_to("oneclient.host.log.INFO.1")
    aborted(mount)
    mount.restore()
    (log_dir / "oneclient.host.log.INFO.2").write_text("Log file created at: 10:05:00\n" + FORCED_PROXY)
    (log_dir / "oneclient.INFO").unlink()
    (log_dir / "oneclient.INFO").symlink_to("oneclient.host.log.INFO.2")
    mount.close_watch()
    assert mount.metadata()["io_mode"]["effective"] == "proxy"


# --------------------------------------------------------------------------------------------------------- the infra
def test_o97_the_earlier_generation_is_listed_as_stopped_by_the_harness_and_the_live_one_by_its_own_state(mount, node):
    aborted(mount)
    mount.restore()
    state = mount.infra_state()
    assert set(state) == {"mount", "mount_watch", "mount_1", "mount_watch_1"}
    for role in ("mount_1", "mount_watch_1"):
        assert state[role]["stopped_by_harness"] is True
        assert infra.problems(role, "c", state[role]["end"]) == []
    assert "stopped_by_harness" not in state["mount"]
    assert state["mount"]["start"] == mount.oneclient.process.record()


def test_o97_a_real_death_of_generation_twos_oneclient_is_not_hidden_by_generation_ones_note(mount, node):
    aborted(mount)
    mount.restore()
    process = mount.oneclient.process
    node.signal(process.pid, process.start, signal.SIGKILL)
    state = mount.infra_state()
    assert infra.problems("mount", "c", state["mount"]["end"])  # the live oneclient ended: a death


# ----------------------------------------------------------------------------------------------------------- failure
def test_o97_a_mount_that_never_appears_fails_the_restore_and_removes_the_token_file(mount, node):
    aborted(mount)
    node.mount_never = True
    node.mounts.clear()  # the old mount is unmounted by the restore: nothing is left at the point afterwards
    with pytest.raises(SandboxError, match="did not appear"):
        mount.restore()
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_oneclient_that_ends_before_its_mount_appears_fails_the_restore_and_removes_the_token_file(mount, node):
    aborted(mount)
    node.oneclient_fails = True
    with pytest.raises(SandboxError, match="ended before"):
        mount.restore()
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_node_that_cannot_start_oneclient_fails_the_restore_with_a_fixed_phrase(mount, node, monkeypatch):
    aborted(mount)

    def refuse(*args, **kwargs):
        raise OSError("invented failure naming /some/where")

    monkeypatch.setattr(node, "spawn", refuse)
    with pytest.raises(SandboxError) as caught:
        mount.restore()
    monkeypatch.undo()
    assert "/some/where" not in str(caught.value)
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_failed_restore_still_stops_clean_with_nothing_left(mount, node):
    aborted(mount)
    node.oneclient_fails = True
    with pytest.raises(SandboxError):
        mount.restore()
    mount.stop()
    assert node.mounted_at is None
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_dead_mount_that_cannot_be_unmounted_fails_the_restore_before_any_token_is_written(mount, node):
    aborted(mount)
    node.stuck_mount = True
    with pytest.raises(SandboxError):
        mount.restore()
    assert node.mount_count == 1
    assert not os.path.lexists(mount.where.token_file)
    node.stuck_mount = False


def test_o97_a_restore_of_a_mount_whose_guard_is_closed_is_refused(mount, node):
    aborted(mount)
    mount.close_watch()
    with pytest.raises(SandboxError, match="closed"):
        mount.restore()
    assert node.mount_count == 1


# ----------------------------------------------------------------------------------------------- stop and the restore
def test_o97_a_restore_after_a_stop_began_writes_no_token_file_and_starts_nothing(mount, node):
    aborted(mount)
    mount.refuse_restores()
    with pytest.raises(SandboxError, match="being stopped"):
        mount.restore()
    assert not os.path.lexists(mount.where.token_file)
    assert node.mount_count == 1
    assert len(node.watches) == 1


def test_o97_a_stop_that_begins_while_a_restore_waits_for_its_mount_ends_it_and_leaves_no_token_file(
    node, settings, fusectl_root
):
    """O97: the restore notices the stop at its next poll, removes its token file and raises; the generation it began
    is the live one, so the stop's own steps end its oneclient."""
    run = settings.work_root / "run-1"
    run.mkdir(mode=0o700)
    clock = SleptClock()
    holder = {}

    def sleep(seconds):
        clock.sleep(seconds)
        if node.mount_never and "mount" in holder:
            holder["mount"].refuse_restores()  # a stop begins while the restore waits

    made = HM.HostMount(node, settings, make_spec("mounted"), run, mountinfo=node.mountinfo, sleep=sleep,
                        clock=clock, fusectl_root=fusectl_root)  # fmt: skip
    holder["mount"] = made
    made.start()
    try:
        assert made.abort(30.0)["ok"] is True
        node.mount_never = True
        with pytest.raises(SandboxError, match="being stopped"):
            made.restore()
        assert not os.path.lexists(made.where.token_file)
        assert made.connection()["device"] is None  # the generation it began never had its mount
    finally:
        node.mount_never = False
        made.stop()
    assert node.mounted_at is None
    assert all(not proc.alive for proc in node.procs.values() if proc.role in ("oneclient", "mountwatch"))


def test_o97_a_stop_after_a_restore_ends_both_generations_and_removes_every_file(mount, node):
    aborted(mount)
    mount.restore()
    mount.stop()
    assert node.mounted_at is None
    assert not os.path.lexists(mount.where.token_file)
    assert not mount.where.bundle_dir.exists()
    assert all(not proc.alive for proc in node.procs.values() if proc.role in ("oneclient", "mountwatch"))


# ------------------------------------------------------------------------------------------------- one total deadline
def slow_unmounts(mount, node, monkeypatch):
    """`fusermount3` takes all the time it is given and fails: a busy mount, on the mount's own (virtual) clock."""
    real = node.run

    def run(argv, timeout_s, **kwargs):
        if argv and argv[0] == "fusermount3":
            mount._clock.sleep(timeout_s)
            return CmdResult(1, "", "fusermount3: invented busy\n")
        return real(argv, timeout_s, **kwargs)

    monkeypatch.setattr(node, "run", run)


def test_o97_a_restore_whose_unmount_overruns_the_deadline_starts_nothing_and_leaves_no_token_file(
    mount, node, monkeypatch
):
    """O97: one total deadline: each step gets the lesser of its own bound and the time left, and past it the token
    file is not written and no oneclient is started."""
    aborted(mount)
    slow_unmounts(mount, node, monkeypatch)
    began = mount._clock()
    with pytest.raises(SandboxError, match="ran out of time"):
        mount.restore(timeout_s=5.0)
    monkeypatch.undo()
    assert mount._clock() - began <= 5.5
    assert not os.path.lexists(mount.where.token_file)
    assert node.oneclient_tokens == [TOKEN]  # the first only: the token was never written again
    assert len(node.watches) == 1


def test_o97_a_restore_whose_old_oneclient_will_not_end_in_time_is_cut_at_the_deadline(mount, node, monkeypatch):
    aborted(mount)
    real = node.signal
    monkeypatch.setattr(node, "signal", lambda pid, start, sig: True)  # sent, and oneclient stays: a thread in D
    began = mount._clock()
    with pytest.raises(SandboxError):
        mount.restore(timeout_s=2.0)
    monkeypatch.setattr(node, "signal", real)
    assert mount._clock() - began <= 2.5
    assert not os.path.lexists(mount.where.token_file)
    assert len(node.watches) == 1
    assert node.mount_count == 1


def test_o97_a_restore_cut_after_the_token_file_was_written_removes_it_within_the_deadline(mount, node):
    aborted(mount)
    node.mount_never = True  # the new oneclient is started on the rewritten token file and its mount never appears
    node.mounts.clear()
    began = mount._clock()
    with pytest.raises(SandboxError, match="did not appear"):
        mount.restore(timeout_s=10.0)
    assert 9.0 <= mount._clock() - began <= 10.5  # the mount wait was cut at the deadline, not at its own 60 s
    assert node.oneclient_tokens == [TOKEN, TOKEN]  # the second was started on it
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_restore_given_no_time_touches_nothing(mount, node):
    aborted(mount)
    signals = list(node.signals)
    with pytest.raises(SandboxError, match="ran out of time"):
        mount.restore(timeout_s=0.0)
    assert node.signals == signals
    assert node.unmounts == []
    assert not os.path.lexists(mount.where.token_file)


def test_o97_a_restore_that_has_time_for_every_step_is_not_cut_by_the_deadline(mount, node):
    aborted(mount)
    assert mount.restore(timeout_s=90.0)["ok"] is True
