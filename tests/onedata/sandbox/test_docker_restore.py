"""O97: the Docker backend brings a run's mount back after a deadline aborted it and the survivors were the agent's.

The abort ended the sidecar's oneclient, so its container is stopped and its FUSE mount is dead. The restore removes
that sidecar, unmounts the dead share, writes the token file again (owner-only, removed once oneclient has read it) and
starts a new sidecar on the same share, with a log directory of its own so the first one's log stays as evidence. It is
shown back by what the host's and the agent's own mount tables say, never by reading through the mount, which could
wait. The old sidecar's end is the harness's, marked before the abort, so the new one's real death still counts. A step
that fails ends the run (the runner publishes `mount_lost`) with a reason in fixed words, and nothing is left running
or on disk that the stop does not remove."""

import json

import pytest
from deadline_support import cell_processes  # noqa: F401  (the fixtures' cell)

from expcore_onedata.sandbox import deadline as DL
from expcore_onedata.sandbox import hostops
from expcore_onedata.sandbox import remount as RM
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import TOKEN_FILE

ATTRIBUTION = {"verdict": "agent", "why": "blocked_on_held_down_holder", "target": "providers[1]", "space_holder": "x"}


def share_of(mounted) -> str:
    return str(mounted.dir / "mount")


def devices(host, mounted) -> list[str]:
    return [m["dev"] for m in hostops.fuse_mounts(host.mountinfo()) if m["mountpoint"] == share_of(mounted)]


@pytest.fixture
def aborted(world, mounted):
    """The run after a restorable abort that freed the agent's survivor: the abort made, oneclient gone (its container
    stopped, its mount dead), and the restore pending as the runner keeps it; the agent's own mount table follows
    the share, as propagation makes it."""
    host = world.host
    host.ns_mountinfo = True
    before = devices(host, mounted)
    note = mounted._abort_mount(30.0)
    assert note["ok"] is True
    sidecar = host.containers[mounted.names.sidecar]
    sidecar.running = False
    host.alive.discard(sidecar.pid)
    record = {
        "utc": "2026-01-01T00:00:00.000+00:00", "grace_s": 5.0, "margin_s": 30.0, "outcome": DL.MOUNT_ABORTED,
        "restore": DL.RESTORE_PENDING, "attribution": dict(ATTRIBUTION), "steps": [{"step": "abort", "ok": True}],
    }  # fmt: skip
    mounted._cells._publish(1, record)
    return type("Aborted", (), {"world": world, "host": host, "sandbox": mounted, "devices": before, "record": record})


def calls_after(host, start: int) -> list[tuple]:
    return host.calls[start:]


def test_o97_the_restore_brings_the_mount_back_as_a_new_sidecar_on_a_new_device(aborted):
    """O97: the old sidecar is removed, the dead mount unmounted, a new sidecar started on the same share, and the host
    and the agent each show one FUSE mount there on a device the dead one did not have."""
    sbx, host = aborted.sandbox, aborted.host
    old_pid = host.containers[sbx.names.sidecar].pid
    start = len(host.calls)
    answer = sbx.restore_mount()
    assert answer["ok"] is True
    assert answer["generation"] == 2
    new = host.containers[sbx.names.sidecar]
    assert new.pid != old_pid
    assert new.running
    now = devices(host, sbx)
    assert len(now) == 1
    assert now[0] not in aborted.devices
    kinds = [(c[0], c[1]) for c in calls_after(host, start) if c[0] in ("docker", "run")]
    removed = kinds.index(("docker", "rm"))
    unmounted = next(i for i, c in enumerate(kinds) if c[0] == "run" and c[1][0] == "fusermount")
    started = kinds.index(("docker", "run"))
    assert removed < unmounted < started
    assert sbx._cells.pending_restore is None
    assert sbx._cells.ended is None
    assert aborted.record["outcome"] == DL.MOUNT_RESTORED


def test_o97_no_copy_of_the_token_is_left_after_the_restore_and_the_sidecar_started_with_a_fresh_one(aborted):
    """O97: the file is written again at the same place (its directory was made at the start), bound into the new
    sidecar alone, and removed once the mount is up, so the host keeps none."""
    sbx, host = aborted.sandbox, aborted.host
    sbx.restore_mount()
    assert not sbx._token_files_left()
    assert not (sbx.dir / TOKEN_FILE).exists()
    new = host.containers[sbx.names.sidecar]
    assert any(str(sbx.dir / TOKEN_FILE) in spec for spec in new.mount_specs())  # bound as the start bound it
    assert any("oneclient-log-2" in spec for spec in new.mount_specs())  # the first generation's log is not reused


def test_o97_the_log_of_the_first_oneclient_stays_where_it_was_and_the_new_one_logs_beside_it(aborted):
    """O97: generation 2 logs in a directory of its own, owner-only, beside generation 1's, which is kept as the run's
    evidence; the first is not bound into the new sidecar."""
    sbx = aborted.sandbox
    first = sbx.dir / "oneclient-log"
    first.joinpath("keep").write_text("generation 1")
    sbx.restore_mount()
    second = sbx.dir / "oneclient-log-2"
    assert second.is_dir()
    assert oct(second.stat().st_mode & 0o777) == "0o700"
    assert first.joinpath("keep").read_text() == "generation 1"


def test_o97_the_old_sidecars_end_is_the_harness_and_the_new_ones_death_is_not(aborted):
    """O97: the abort ended the first oneclient, which is no death of infrastructure (the end was marked as the
    harness's before the abort); the baseline of the new sidecar is its own, so it dying later is a problem."""
    sbx, host = aborted.sandbox, aborted.host
    check = sbx.infra_check()
    assert check["containers"]["sidecar"].get("stopped_by_harness") is True
    assert check["death"] == []
    sbx.restore_mount()
    assert sbx.infra_check()["death"] == []
    new = host.containers[sbx.names.sidecar]
    new.running, new.exit_code = False, 137
    host.alive.discard(new.pid)
    died = sbx.infra_check()
    assert died["containers"]["sidecar"]["problems"]
    assert "stopped_by_harness" not in died["containers"]["sidecar"]


def test_o97_the_metadata_says_the_mount_was_aborted_and_restored_with_no_path_or_name(aborted):
    """O97: `aborted` stays true (it was), `remounts` counts the restores, and `generations` lists every generation as
    the Apptainer host mount does (`wait_s`, `fusectl`, `abort`), the one the restore closed with what its log showed;
    nothing of the run's directory or the sidecar's name."""
    sbx = aborted.sandbox
    sbx.restore_mount()
    mount = sbx.metadata()["mount"]
    assert mount["aborted"] is True
    assert mount["remounts"] == 1
    first, second = mount["generations"]
    assert first["wait_s"] == mount["wait_s"]
    assert first["abort"]["ok"] is True
    assert first["io_mode"]["declared"] == sbx.s.mount.io_mode
    assert isinstance(second["wait_s"], float)
    assert second["abort"] is None
    assert "io_mode" not in second  # the live generation is read at the collection
    text = json.dumps(mount)
    assert str(sbx.dir) not in text
    assert sbx.names.sidecar not in text


def test_o97_a_second_restore_after_a_second_abort_is_generation_three(aborted):
    """O97: the generation counts up, and the next log directory is the next one's."""
    sbx, host = aborted.sandbox, aborted.host
    assert sbx.restore_mount()["generation"] == 2
    sbx._abort_mount(30.0)
    sidecar = host.containers[sbx.names.sidecar]
    sidecar.running = False
    host.alive.discard(sidecar.pid)
    record = {"outcome": DL.MOUNT_ABORTED, "restore": DL.RESTORE_PENDING, "steps": [], "attribution": ATTRIBUTION}
    sbx._cells._publish(2, record)
    assert sbx.restore_mount()["generation"] == 3
    assert (sbx.dir / "oneclient-log-3").is_dir()
    mount = sbx.metadata()["mount"]
    assert mount["remounts"] == 2
    assert [bool(g["abort"]) for g in mount["generations"]] == [True, True, False]  # each abort is on its generation


@pytest.mark.parametrize(
    ("break_it", "why"),
    [
        (lambda w, s: w.host.fail_remove.add(s.names.sidecar), "the old sidecar could not be removed"),
        (lambda w, s: w.host.stuck_mounts.add(share_of(s)), "the dead mount could not be unmounted"),
        (lambda w, s: w.host.fail_run.add(s.names.sidecar), "the new sidecar did not start"),
        (
            lambda w, s: w.host.ns_override.update({w.host.containers[s.names.agent].pid: "1 0 8:1 / / ro - x x ro\n"}),
            "the new mount is not in the agent's mount table",
        ),
    ],
    ids=["old sidecar stays", "mount stays", "new sidecar fails", "agent does not see it"],
)
def test_o97_a_restore_that_fails_says_why_in_fixed_words_ends_the_run_and_leaves_nothing_the_stop_misses(
    aborted, break_it, why
):
    """O97: each step's failure is `ok: False` with its own words, the runner ends the run as `mount_lost` by the
    restore, no token file is left on the host, and the stop afterwards still removes whatever was started."""
    sbx = aborted.sandbox
    break_it(aborted.world, sbx)
    answer = sbx.restore_mount()
    assert answer == {"ok": False, "why": why}
    assert sbx._cells.ended["by"] == DL.RESTORE
    assert not sbx._token_files_left()
    aborted.host.fail_remove.clear()
    aborted.host.stuck_mounts.clear()
    aborted.host.fail_run.clear()
    sbx.stop()  # the fixture's stop finds it stopped; a leak would raise here


def test_o97_a_host_mount_table_that_cannot_be_read_fails_the_restore_before_anything_is_touched(aborted):
    """O97: unknown is never gone: no table, no restore, and nothing removed or started."""
    sbx, host = aborted.sandbox, aborted.host
    host.denied.add("/proc/self/mountinfo")
    start = len(host.calls)
    answer = sbx.restore_mount()
    host.denied.clear()  # the stop that follows reads the table
    assert answer == {"ok": False, "why": "the host's mount table could not be read"}
    assert not [c for c in calls_after(host, start) if c[0] == "docker" and c[1] in ("rm", "run")]


def test_o97_a_restore_the_run_no_longer_wants_creates_nothing(aborted):
    """O97: a stop that gave up on the restore (the runner no longer holds it as running) leaves nothing for a late
    restore to start: it answers at its first step, with no container, mount or file made."""
    sbx, host = aborted.sandbox, aborted.host
    start = len(host.calls)
    sbx._cells.pending_restore = None  # what the runner's abandon leaves
    assert sbx._restore_host_mount() == {"ok": False, "why": "the restore is not wanted any more"}
    assert not [c for c in calls_after(host, start) if c[0] in ("docker", "run")]


def test_o97_a_restore_given_up_on_between_its_steps_starts_no_sidecar(aborted):
    """O97: the runner gives up on the restore after the dead mount is unmounted (a stop that waited its bound): the
    restore sees it before it writes the token and starts nothing, so the stop that follows has nothing to remove
    that it did not know."""
    sbx, host = aborted.sandbox, aborted.host
    real = host.run_handlers.get("fusermount")

    def gives_up(argv):
        sbx._cells.pending_restore = None
        return real(argv) if real else host._run_default(argv)

    host.run_handlers["fusermount"] = gives_up
    start = len(host.calls)
    answer = sbx._restore_host_mount()
    assert answer == {"ok": False, "why": "the restore is not wanted any more"}
    assert not [c for c in calls_after(host, start) if c == ("docker", "run", ())]
    assert not sbx._token_files_left()


def test_o97_the_restore_bar_is_what_the_mount_tables_show_not_a_read_through_the_mount():
    """O97: `remount.bar` judges the two tables: one FUSE mount at the share on a new device on the host, and in the
    agent's namespace one at the mount path, on that device. Each failing shape has its own words."""
    root = "1 0 8:1 / / rw - ext4 /dev/root rw\n"
    host_ok = root + "10 1 0:77 / /run/share rw - fuse.oneclient s rw\n"
    ns_ok = root + "20 1 0:77 / /data ro - fuse.oneclient s ro\n"
    assert RM.bar(host_ok, ns_ok, share="/run/share", mount_path="/data", old_devices={"0:70"}) is None
    cases = {
        "the new mount is not on the host": (root, ns_ok),
        "the new mount is the dead one's device": (host_ok.replace("0:77", "0:70"), ns_ok.replace("0:77", "0:70")),
        "the agent's mount table could not be read": (host_ok, None),
        "the new mount is not in the agent's mount table": (host_ok, root),
        "the agent's mount table shows the mount more than once": (host_ok, ns_ok + ns_ok.splitlines()[1] + "\n"),
        "the agent's mount is another device": (host_ok, ns_ok.replace("0:77", "0:99")),
        "the host's mount table could not be read": ("", ns_ok),
    }
    for why, (host_table, ns_table) in cases.items():
        got = RM.bar(host_table, ns_table, share="/run/share", mount_path="/data", old_devices={"0:70"})
        assert got == why


def test_o97_the_direct_io_lines_of_every_generation_stand_and_an_unread_one_is_unknown():
    """O97: a direct run's reads went to the storage only if no generation's log shows a line of detection or
    fallback: the lines of the earlier generations and the live one's are one set, in the markers' order; a generation
    whose log could not be read makes the whole unknown, never direct."""
    assert RM.direct_lines([["fallback"]], ["detect"], order=("detect", "fallback")) == ["detect", "fallback"]
    assert RM.direct_lines([], [], order=("detect",)) == []
    assert RM.direct_lines([None], ["detect"], order=("detect",)) is None
    assert RM.direct_lines([["detect"]], None, order=("detect",)) is None


def log_of(mounted, generation: int):
    name = "oneclient-log" if generation == 1 else f"oneclient-log-{generation}"
    return str(mounted.dir / name / "oneclient.INFO")


START_LOG = (
    "Log file created at: 2026/11/20 10:00:00\n"
    "I1120 10:00:00.100300     1 logging.cc:84] Forced direct IO: {direct}\n"
    "I1120 10:00:00.100400     1 logging.cc:85] Forced proxy IO: {proxy}\n"
)
PROXY_LOG = START_LOG.format(direct="0", proxy="1")
NO_FORCED_LOG = START_LOG.format(direct="0", proxy="0")


@pytest.mark.parametrize(
    ("first", "agrees"),
    [(PROXY_LOG, True), (NO_FORCED_LOG, False), (None, False)],
    ids=["the first oneclient agreed", "the first ran in no forced mode", "the first log cannot be read"],
)
def test_o97_the_io_mode_of_a_restored_mount_is_the_worst_of_its_generations(aborted, first, agrees):
    """O97: a restored run had two oneclients, and the second one's log alone would show a mount that agrees with its
    declared mode while the first ran in another: each generation's log is judged (the first one's read from the run's
    own log directory when it was closed) and the worst stands, so a first oneclient is never unjudged; one whose log
    cannot be read is unknown, which is never agreement."""
    sbx, host = aborted.sandbox, aborted.host
    assert sbx.s.mount.io_mode == "proxy"
    if first is not None:
        host.files[log_of(sbx, 1)] = first
    sbx.restore_mount()
    sbx._read_sidecar_log = lambda: PROXY_LOG  # the live generation's own log agrees
    sbx._note_io_mode()
    assert sbx.mount_io_mode()["agree"] is agrees


def test_o97_a_copy_of_the_dead_mount_left_in_the_agents_namespace_is_detached_before_the_new_one(aborted):
    """O97: the host's unmount did not reach the agent's own mount table (the table the teardown reads and detaches for
    the same reason): the restore detaches it from inside the namespace, shows it gone, and only then starts the new
    sidecar, so the new mount is not shown there twice."""
    sbx, host = aborted.sandbox, aborted.host
    agent = host.containers[sbx.names.agent]
    stale = "1 0 8:1 / / ro - overlay overlay ro\n99 25 0:900 / /data ro - fuse.oneclient s ro\n"
    host.ns_override[agent.pid] = stale
    detached = []

    def nsenter(argv):
        detached.append(argv)
        host.ns_override.pop(agent.pid)  # the copy is gone: the table is the propagation's again
        return CmdResult(0)

    host.run_handlers["nsenter"] = nsenter
    assert sbx.restore_mount()["ok"] is True
    assert len(detached) == 1
    assert detached[0][-1] == "/data"


def test_o97_a_copy_of_the_dead_mount_that_cannot_be_detached_fails_the_restore_before_a_sidecar_starts(aborted):
    """O97: the stale copy stays: the restore fails in fixed words and starts no sidecar, so nothing is made beside a
    mount that would show twice."""
    sbx, host = aborted.sandbox, aborted.host
    agent = host.containers[sbx.names.agent]
    host.ns_override[agent.pid] = "1 0 8:1 / / ro - overlay overlay ro\n99 25 0:900 / /data ro - fuse.oneclient s ro\n"
    host.run_handlers["nsenter"] = lambda argv: CmdResult(1, "", "busy")
    start = len(host.calls)
    assert sbx.restore_mount() == {"ok": False, "why": "the dead mount is still in the agent's mount table"}
    assert ("docker", "run", ()) not in calls_after(host, start)
    host.ns_override.clear()
    host.run_handlers.pop("nsenter")


def test_o97_a_restore_given_up_on_while_it_waits_for_the_mount_stops_waiting_and_adds_nothing(aborted):
    """O97: the new sidecar is started but its mount does not come up in time for a stop that waited its bound: the
    runner gives the restore up (nothing is pending any more), and the restore leaves its wait at the next look instead
    of running its whole minute, so the teardown that follows finds the sidecar it registered and nothing else."""
    sbx, host = aborted.sandbox, aborted.host
    real_run = host.docker
    state = {"looks": 0}

    def docker(*args, **kwargs):
        if args[0] == "run" and "--name" in args and sbx.names.sidecar in args:
            done = real_run(*args, **kwargs)
            host.mounts.pop(str(sbx.dir / "mount"), None)  # the mount never comes up
            return done
        if args[0] == "inspect":
            state["looks"] += 1
            if state["looks"] >= 2:
                sbx._cells.pending_restore = None  # the stop gave up on it
        return real_run(*args, **kwargs)

    host.docker = docker
    try:
        answer = sbx._cells.restore(sbx._restore_host_mount)
    finally:
        host.docker = real_run
    assert answer == {"ok": False, "why": "the restore is not wanted any more"}
    assert not sbx._token_files_left()


def test_o97_the_restores_total_time_is_below_the_time_a_stop_waits_for_it():
    """O97: a stop waits `RESTORE_WAIT_S` for a restore under way and then gives up on it: the restore's own bound is
    below that, so a restore still inside its time is never one a stop has abandoned."""
    from expcore_onedata.sandbox import cells

    assert RM.RESTORE_TOTAL_S < cells.RESTORE_WAIT_S


def slows_the_unmount(host, sandbox, seconds: float):
    """The dead share takes `seconds` to unmount, by the sandbox's own clock (the abort's, the waits'): the restore
    reaches its next step with that much of its time gone."""
    real = host.run_handlers.get("fusermount")

    def slow(argv):
        sandbox._clock.now += seconds
        return real(argv) if real else host._run_default(argv)

    host.run_handlers["fusermount"] = slow


def test_o97_a_restore_that_is_out_of_time_after_the_unmount_writes_no_token_and_starts_no_sidecar(aborted):
    """O97: the unmount took the restore past its total time: nothing of the new generation is begun, no token file is
    written, no sidecar is started, and the answer is in fixed words."""
    sbx, host = aborted.sandbox, aborted.host
    slows_the_unmount(host, sbx, RM.RESTORE_TOTAL_S + 5)
    start = len(host.calls)
    assert sbx.restore_mount() == {"ok": False, "why": "the restore ran out of its time"}
    assert ("docker", "run", ()) not in calls_after(host, start)
    assert not (sbx.dir / TOKEN_FILE).exists()
    assert not sbx._token_files_left()


def test_o97_a_mount_that_does_not_come_up_by_the_restores_time_ends_the_wait_and_leaves_no_token(aborted):
    """O97: the sidecar is started with the time left (50 s of 90), the mount does not come up, and the wait ends at the
    restore's time, not at the mount's own minute: out of time, with the token file removed whatever happened."""
    sbx, host = aborted.sandbox, aborted.host
    slows_the_unmount(host, sbx, RM.RESTORE_TOTAL_S - 50)
    real_run = host.docker

    def docker(*args, **kwargs):
        done = real_run(*args, **kwargs)
        if args[0] == "run" and sbx.names.sidecar in args:
            host.mounts.pop(str(sbx.dir / "mount"), None)  # the mount never comes up
        return done

    host.docker = docker
    try:
        before = sbx._clock.now
        answer = sbx.restore_mount()
    finally:
        host.docker = real_run
    assert answer == {"ok": False, "why": "the restore ran out of its time"}
    assert sbx._clock.now - before < RM.RESTORE_TOTAL_S + 1  # the wait ended at the bound, not a minute past it
    assert not sbx._token_files_left()


def test_o97_the_new_sidecars_creation_is_bounded_by_the_restores_time_so_none_completes_after_it(aborted):
    """O97 (Codex, final review): `docker run` has its own 120 s default, past the restore's 90 and a stop's 100: a
    creation that outlasts the restore's time could finish after the runner gave the restore up and the teardown
    looked. It is given the time the restore has left, never more."""
    sbx, host = aborted.sandbox, aborted.host
    slows_the_unmount(host, sbx, RM.RESTORE_TOTAL_S - 50)  # 50 s of the restore are left when the sidecar starts
    real_run, bounds = host.docker, []

    def docker(*args, **kwargs):
        if args[0] == "run" and "--name" in args and sbx.names.sidecar in args:
            bounds.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    host.docker = docker
    try:
        assert sbx.restore_mount()["ok"] is True
    finally:
        host.docker = real_run
    assert len(bounds) == 1
    assert bounds[0] is not None
    assert 49 <= bounds[0] <= 50  # what was left, and at most the restore's bound
