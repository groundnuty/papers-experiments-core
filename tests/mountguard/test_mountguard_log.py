"""Reading the mount watcher's log: intact only when every check holds, otherwise unknown with its reasons; the
accesses inside the agent's window counted, and the isolation proofs read."""

import pytest
from mountguard_logs import NONCE, canary, encode, inotify, lines, log, proof

from expcore.mountguard import log as guard_log
from expcore.mountguard import read_log

TOUCH = inotify("dir", "IN_OPEN", "IN_ISDIR")


def test_a_complete_log_of_this_run_is_intact_with_no_access_and_proven_isolation():
    reading = read_log(log(), NONCE)
    assert reading.intact
    assert reading.problems == ()
    assert reading.accesses == {"token": 0, "dir": 0}
    assert reading.traced == 0
    assert reading.isolation_problems == ()
    assert reading.proofs == 1
    assert reading.ptrace_scope == 1


def test_the_canary_before_the_window_is_the_hosts_own_and_never_an_access():
    """The host's removal of the token file comes before `armed`: it shows the watches live, nothing more."""
    assert read_log(log(), NONCE).accesses == {"token": 0, "dir": 0}


def test_an_event_on_the_token_directory_inside_the_window_is_an_access():
    reading = read_log(log([TOUCH, inotify("dir", "IN_ACCESS", "IN_ISDIR")]), NONCE)
    assert reading.intact
    assert reading.accesses == {"token": 0, "dir": 2}


def test_an_event_on_the_token_file_inside_the_window_is_an_access():
    reading = read_log(log([inotify("token", "IN_OPEN")]), NONCE)
    assert reading.accesses == {"token": 1, "dir": 0}


def test_events_after_the_window_closes_are_the_hosts_teardown_and_not_accesses():
    reading = read_log(log(after=[inotify("dir", "IN_DELETE_SELF"), inotify("dir", "IN_IGNORED")]), NONCE)
    assert reading.intact
    assert reading.accesses == {"token": 0, "dir": 0}


def test_a_tracer_attached_to_oneclient_inside_the_window_is_counted():
    reading = read_log(log([{"event": "tracer", "attached": True}, {"event": "tracer", "attached": False}]), NONCE)
    assert reading.intact
    assert reading.traced == 1


def test_a_tracer_already_attached_when_the_window_opens_is_counted():
    setup = [*canary(), {"event": "tracer", "attached": True}]
    assert read_log(log(setup=setup), NONCE).traced == 1


def damaged(kind: str) -> bytes:
    """A log damaged in one way (line 3 is in the canary, 8 is the window's access)."""
    out = lines([TOUCH])
    if kind == "cut_end":
        return encode(out[:-1])
    if kind == "cut_line":
        return encode(out)[:-7]
    if kind == "gap":
        del out[3]
    elif kind == "repeat":
        out[4]["seq"] = 3
    elif kind == "clock_back":
        out[5]["mono"] = 0.0
    elif kind == "after_end":
        out.append({**out[3], "seq": len(out), "mono": 999.0})
    elif kind == "end_counts":
        out[-1]["lines"] = 3
    elif kind == "end_shape":
        del out[-1]["polls_failed"]
    elif kind == "no_start":
        out[0]["event"] = "begin"
    elif kind == "other_format":
        out[0]["format"] = "expcore-mountwatch/0"
    elif kind == "unknown_event":
        out[3] = {**out[3], "event": "chatter"}
    elif kind == "bad_inotify":
        out[3] = {**out[3], "target": "elsewhere"}
    elif kind == "bad_mask":
        out[3] = {**out[3], "mask": ["IN_WHATEVER"]}
    elif kind == "not_json":
        return encode(out).replace(b'"event": "armed"', b'"event": armed')
    return encode(out)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("cut_end", guard_log.NOT_CLOSED),
        ("cut_line", guard_log.UNPARSEABLE),
        ("gap", guard_log.NOT_MONOTONIC),
        ("repeat", guard_log.NOT_MONOTONIC),
        ("clock_back", guard_log.NOT_MONOTONIC),
        ("after_end", guard_log.NOT_CLOSED),
        ("end_counts", guard_log.COUNTS_DIFFER),
        ("end_shape", guard_log.NOT_CLOSED),
        ("no_start", guard_log.NO_START),
        ("other_format", guard_log.NO_START),
        ("unknown_event", guard_log.UNPARSEABLE),
        ("bad_inotify", guard_log.UNPARSEABLE),
        ("bad_mask", guard_log.UNPARSEABLE),
        ("not_json", guard_log.UNPARSEABLE),
    ],
)
def test_a_damaged_log_is_unknown_with_its_reason(kind, reason):
    reading = read_log(damaged(kind), NONCE)
    assert not reading.intact
    assert reason in reading.problems


@pytest.mark.parametrize("kind", ["cut_end", "cut_line", "unknown_event", "after_end"])
def test_a_log_cut_or_damaged_before_its_end_never_proves_the_isolation(kind):
    """What a cut log lacks may be a failed proof."""
    assert read_log(damaged(kind), NONCE).isolation_problems


@pytest.mark.parametrize(
    ("data", "reason"),
    [(None, guard_log.MISSING), (b"", guard_log.EMPTY), (b"\xff\xfe\n", guard_log.UNPARSEABLE)],
)
def test_a_missing_empty_or_undecodable_log_is_unknown_and_never_proven(data, reason):
    reading = read_log(data, NONCE)
    assert reading.problems == (reason,)
    assert reading.isolation_problems == (guard_log.NO_PROOF,)


def test_a_log_past_the_size_limit_is_not_read():
    assert read_log(log(), NONCE, max_bytes=100).problems == (guard_log.TOO_LARGE,)


def test_a_log_of_another_run_is_unknown():
    assert guard_log.NOT_THIS_RUN in read_log(log(nonce="ff" * 16), NONCE).problems


def test_a_log_whose_watches_were_not_both_set_is_unknown():
    reading = read_log(log(watches={"dir": True, "token": False}), NONCE)
    assert guard_log.WATCH_NOT_SET in reading.problems


def test_a_log_without_the_canary_is_unknown():
    """inotify that never watched anything leaves a start, an end and nothing between: never a clean run."""
    reading = read_log(log(setup=[]), NONCE)
    assert guard_log.NO_CANARY in reading.problems


@pytest.mark.parametrize("drop", [("token", "IN_DELETE_SELF"), ("dir", "IN_DELETE")])
def test_a_canary_one_watch_did_not_see_is_unknown(drop):
    setup = [line for line in canary() if (line["target"], line["mask"][0]) != drop]
    assert guard_log.NO_CANARY in read_log(log(setup=setup), NONCE).problems


def test_a_queue_overflow_is_unknown_wherever_it_is():
    reading = read_log(log([{"event": "overflow"}]), NONCE)
    assert guard_log.EVENTS_LOST in reading.problems


def test_lines_left_unwritten_make_the_log_unknown():
    assert guard_log.UNWRITTEN in read_log(log(unwritten=3), NONCE).problems


def test_a_log_never_armed_is_unknown():
    assert guard_log.NOT_ARMED in read_log(log(armed=False), NONCE).problems


def test_a_log_armed_twice_is_unknown():
    reading = read_log(log([{"event": "armed"}]), NONCE)
    assert guard_log.NOT_ARMED in reading.problems


def test_a_log_whose_window_never_closed_is_unknown_and_counts_to_its_end():
    reading = read_log(log([TOUCH], disarmed=False), NONCE)
    assert guard_log.NOT_DISARMED in reading.problems
    assert reading.accesses == {"token": 0, "dir": 1}


def test_oneclient_not_seen_before_the_window_is_unknown():
    reading = read_log(log(oneclient={"event": "oneclient", "pid": 99, "seen": False, "nspid_depth": None}), NONCE)
    assert guard_log.ONECLIENT_UNWATCHED in reading.problems


def test_sampling_oneclient_that_became_unreadable_inside_the_window_is_unknown():
    reading = read_log(log([{"event": "sampling", "state": "stopped", "why": "unreadable"}]), NONCE)
    assert guard_log.SAMPLING_FAILED in reading.problems


def test_sampling_that_stopped_because_oneclient_ended_is_not_a_gap():
    """oneclient gone holds no token any more: there is nothing left to read or trace."""
    reading = read_log(log([{"event": "sampling", "state": "stopped", "why": "gone"}]), NONCE)
    assert reading.intact


def test_a_damaged_log_still_counts_the_accesses_it_shows_as_a_lower_bound():
    reading = read_log(log([TOUCH], end=False), NONCE)
    assert not reading.intact
    assert reading.accesses == {"token": 0, "dir": 1}


def test_every_proof_passing_proves_the_isolation():
    reading = read_log(log(proofs=[proof(), proof(cell={"pid": 5000, "start_time": 900, "nspid_depth": 2})]), NONCE)
    assert reading.isolation_problems == ()
    assert reading.proofs == 2


def test_a_window_without_a_proof_leaves_the_isolation_unproven():
    reading = read_log(log(proofs=[]), NONCE)
    assert reading.intact  # the log itself is whole: what it lacks is the proof
    assert reading.isolation_problems == (guard_log.NO_PROOF,)


def test_one_failed_proof_among_passing_ones_leaves_the_isolation_unproven_with_its_problems():
    failed = proof(ok=False, problems=["same_pid_namespace"])
    reading = read_log(log(proofs=[proof(), failed]), NONCE)
    assert guard_log.PROOF_FAILED in reading.isolation_problems
    assert "same_pid_namespace" in reading.isolation_problems


def test_a_proof_with_problems_is_failed_whatever_its_ok_says():
    forged = proof(ok=True, problems=["host_proc_in_cell"])
    assert guard_log.PROOF_FAILED in read_log(log(proofs=[forged]), NONCE).isolation_problems


def test_a_proof_that_found_no_cell_is_failed():
    found = {**proof(ok=False, problems=["no_cell_namespace"]), "cell": None}
    assert guard_log.PROOF_FAILED in read_log(log(proofs=[found]), NONCE).isolation_problems


def test_a_watcher_that_did_not_find_the_runs_mount_cannot_prove_the_isolation():
    reading = read_log(log(mount_seen=False), NONCE)
    assert guard_log.MOUNT_UNSEEN in reading.isolation_problems


def test_a_poll_that_could_not_list_the_processes_leaves_the_isolation_unproven():
    reading = read_log(log(polls_failed=2), NONCE)
    assert guard_log.POLL_FAILED in reading.isolation_problems


def test_a_proof_outside_the_window_does_not_count():
    """A proof before `armed` was taken before the window opened: a cell ran before the window, so the log is unknown."""
    reading = read_log(log(setup=[*canary(), proof()], proofs=[]), NONCE)
    assert guard_log.PROOF_OUTSIDE_WINDOW in reading.problems
    assert guard_log.NO_PROOF in reading.isolation_problems
