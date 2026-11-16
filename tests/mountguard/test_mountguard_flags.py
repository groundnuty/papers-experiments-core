"""One run's mount-guard verdict from its record alone: clean, flagged for an access, or unknown; never clean without a
readable snapshot of an intact log and a proven isolation."""

import pytest
from mountguard_logs import NONCE, inotify, log, proof, record

from expcore.mountguard import (
    CLEAN,
    FLAGGED,
    ISOLATION_UNPROVEN,
    LOG_UNKNOWN,
    NAMESPACE,
    PROCESS_ACCESS,
    TOKEN_ACCESS,
    UNKNOWN_VERDICT,
    MountGuard,
    judge,
    read_log,
    snapshot,
)
from expcore.mountguard import flags as guard_flags


class Source:
    def __init__(self, data):
        self.data = data

    def mount_watch_log(self):
        return self.data


def judged(data: bytes | None, **options):
    """The verdict of a record whose instrument read `data`."""
    instrument = MountGuard(Source(data), NONCE)
    instrument.start({})
    return judge(record(instruments={NAMESPACE: instrument.finish()}, **options), "treatment")


def test_a_run_with_an_intact_log_no_access_and_a_proven_isolation_is_clean():
    """O60: the one way to be clean; every other run is flagged or unknown."""
    run = judged(log())
    assert run.verdict == CLEAN
    assert run.flags == ()
    assert not run.left_out


def test_a_run_that_touched_the_token_directory_is_flagged():
    run = judged(log([inotify("dir", "IN_OPEN", "IN_ISDIR")]))
    assert run.verdict == FLAGGED
    assert run.flags == (TOKEN_ACCESS,)
    assert run.accesses == {"token_file": 0, "token_dir": 1}
    assert run.left_out


def test_a_run_whose_oneclient_got_a_tracer_is_flagged():
    run = judged(log([{"event": "tracer", "attached": True}]))
    assert run.verdict == FLAGGED
    assert run.flags == (PROCESS_ACCESS,)


def test_a_missing_log_is_unknown_never_clean():
    run = judged(None)
    assert run.verdict == UNKNOWN_VERDICT
    assert LOG_UNKNOWN in run.flags
    assert ISOLATION_UNPROVEN in run.flags  # no proof was read either


def test_a_damaged_log_is_unknown():
    run = judged(log()[:-9])
    assert run.verdict == UNKNOWN_VERDICT
    assert LOG_UNKNOWN in run.flags


def test_a_damaged_log_that_still_shows_an_access_is_flagged_and_unknown():
    run = judged(log([inotify("dir", "IN_OPEN", "IN_ISDIR")], end=False))
    assert run.verdict == FLAGGED
    assert run.flags == (TOKEN_ACCESS, LOG_UNKNOWN, ISOLATION_UNPROVEN)  # a cut log never proves the isolation


def test_a_failed_namespace_proof_makes_the_run_unknown():
    failed = proof(ok=False, problems=["same_pid_namespace"])
    run = judged(log(proofs=[proof(), failed]))
    assert run.verdict == UNKNOWN_VERDICT
    assert run.flags == (ISOLATION_UNPROVEN,)
    assert "same_pid_namespace" in run.unproven


def test_a_run_whose_record_has_no_snapshot_is_unknown():
    run = judge(record(), "control")
    assert run.verdict == UNKNOWN_VERDICT
    assert run.unknown == (guard_flags.NOT_RECORDED,)
    assert set(run.flags) == {LOG_UNKNOWN, ISOLATION_UNPROVEN}


def test_a_failed_instrument_is_unknown():
    run = judge(record(instruments={NAMESPACE: {"ok": False, "error": "OSError"}}), "control")
    assert run.unknown == (guard_flags.INSTRUMENT_FAILED,)
    assert run.verdict == UNKNOWN_VERDICT


def clean_snapshot():
    return snapshot(read_log(log(), NONCE))


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"log": "fine"},
        {"unknown": ["log_missing"]},  # intact with a reason
        {"log": "unknown"},  # unknown without one
        {"isolation": "unproven"},  # unproven without a reason
        {"unproven": ["no_end_proof"]},  # proven with one
        {"accesses": {"token_file": 0}},
        {"accesses": {"token_file": 0, "token_dir": -1}},
        {"accesses": {"token_file": 0, "token_dir": True}},
        {"traced": 1.5},
        {"proofs": 0},  # proven with no proof
        {"ptrace_scope": 9},
        {"extra": 1},
    ],
)
def test_a_snapshot_that_is_not_one_this_code_reads_is_unknown(change):
    data = {**clean_snapshot(), **change}
    run = judge(record(instruments={NAMESPACE: data}), "control")
    assert run.unknown == (guard_flags.NOT_A_SNAPSHOT,)
    assert run.verdict == UNKNOWN_VERDICT


def test_the_snapshot_holds_counts_and_reasons_and_never_a_pid_or_a_path():
    data = snapshot(read_log(log([inotify("dir", "IN_OPEN", "IN_ISDIR")]), NONCE))
    assert set(data) == guard_flags.SNAPSHOT_KEYS
    assert data["accesses"] == {"token_file": 0, "token_dir": 1}
    assert data["log"] == "intact"
    assert data["isolation"] == "proven"
    text = repr(data)
    assert "4242" not in text  # the cell's pid
    assert "99" not in text  # oneclient's


def test_the_verdict_of_a_snapshot_alone_is_the_runs():
    assert guard_flags.verdict_of(clean_snapshot()) == CLEAN
    assert guard_flags.verdict_of(snapshot(read_log(log([inotify("dir", "IN_OPEN")]), NONCE))) == FLAGGED
    assert guard_flags.verdict_of(None) == UNKNOWN_VERDICT


def test_the_instrument_refuses_an_empty_nonce():
    with pytest.raises(ValueError, match="nonce"):
        MountGuard(Source(None), "")
