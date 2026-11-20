"""The mount guard over the generations of a restored mount: one complete watcher log for each oneclient the run had,
judged together, the verdict the worst and the proofs their union (O97)."""

import copy

import pytest
from mountguard_logs import NONCE, inotify, log, proof, record

from expcore.mountguard import (
    GENERATIONS,
    NAMESPACE,
    MountGuard,
    aggregate,
    combine,
    judge,
    logs_of,
    read_log,
    snapshot,
)

OTHER_NONCE = "ffeeddccbbaa99887766554433221100"
ONECLIENT_ENDED = {"event": "sampling", "state": "stopped", "why": "gone"}
NO_CELL = {"proofs": []}  # a window in which no cell was caught


def verdict_of(logs, nonce=NONCE):
    data = aggregate(logs, nonce)
    return judge(record(instruments={NAMESPACE: data}), "control"), data


def test_o97_one_log_gives_exactly_the_snapshot_it_always_did():
    """O97: a run with one generation has the snapshot of its one log, with no `generations` key."""
    data = log([inotify("dir", "IN_OPEN")])
    assert aggregate([data], NONCE) == snapshot(read_log(data, NONCE))
    assert GENERATIONS not in aggregate([data], NONCE)


def test_o97_a_source_that_gives_one_log_or_a_list_of_them_is_read_in_order():
    """O97: bytes and None are one log, a sequence is one for each generation, and an empty one is a missing log."""
    assert logs_of(b"x") == [b"x"]
    assert logs_of(None) == [None]
    assert logs_of([b"a", None, b"c"]) == [b"a", None, b"c"]
    assert logs_of([]) == [None]
    assert logs_of(bytearray(b"x")) == [b"x"]


def test_o97_two_clean_generations_are_clean_and_each_is_listed():
    """O97: two intact logs with no access and a proof each: clean, with both generations' snapshots."""
    run, data = verdict_of([log(), log()])
    assert run.verdict == "clean"
    assert not run.left_out
    assert len(data[GENERATIONS]) == 2
    assert data["proofs"] == 2
    assert data["log"] == "intact"
    assert data["isolation"] == "proven"


def test_o97_a_flagged_generation_flags_the_run_whichever_it_is():
    """O97: an access in the window of the second generation, or of the first, makes the run flagged."""
    touched = log([inotify("dir", "IN_OPEN")])
    for logs in ([log(), touched], [touched, log()]):
        run, data = verdict_of(logs)
        assert run.verdict == "flagged"
        assert run.left_out
        assert data["accesses"] == {"token_file": 0, "token_dir": 1}


def test_o97_accesses_in_both_generations_are_summed():
    """O97: the run's counts are the generations' total."""
    touched = log([inotify("dir", "IN_OPEN")])
    assert aggregate([touched, touched], NONCE)["accesses"]["token_dir"] == 2


def test_o97_a_missing_second_log_is_unknown():
    """O97: a generation with no log reads unknown, with its reason, and the isolation is not shown for it."""
    run, data = verdict_of([log(), None])
    assert run.verdict == "unknown"
    assert run.left_out
    assert "log_missing" in data["unknown"]
    assert data["log"] == "unknown"
    assert data["isolation"] == "unproven"


def test_o97_a_log_of_another_run_in_a_generation_is_unknown():
    """O97: every generation is read with the run's nonce: another run's log is not this run's."""
    run, data = verdict_of([log(), log(nonce=OTHER_NONCE)])
    assert run.verdict == "unknown"
    assert "nonce_mismatch" in data["unknown"]


def test_o97_a_generation_whose_oneclient_was_stopped_by_the_abort_is_still_intact():
    """O97: the watcher of the aborted generation sees oneclient gone before its window closes: the log stays intact."""
    run, data = verdict_of([log([ONECLIENT_ENDED]), log()])
    assert run.verdict == "clean"
    assert data["log"] == "intact"


def test_o97_a_restored_generation_with_no_cell_of_its_own_does_not_unprove_the_isolation():
    """O97: the run ended after the restore, so generation 2 caught no cell; generation 1's proof is the union."""
    run, data = verdict_of([log(), log(**NO_CELL)])
    assert run.verdict == "clean"
    assert data["proofs"] == 1
    assert data["isolation"] == "proven"


def test_o97_a_run_with_no_proof_in_any_generation_is_unproven():
    """O97: with no generation having caught a cell, the isolation is not shown."""
    run, data = verdict_of([log(**NO_CELL), log(**NO_CELL)])
    assert run.verdict == "unknown"
    assert data["isolation"] == "unproven"
    assert data["unproven"] == ["no_proof"]


def test_o97_a_failed_proof_in_one_generation_unproves_the_isolation_of_the_run():
    """O97: the proofs are the union, so a failed one anywhere is a failed isolation."""
    failed = log(proofs=[proof(ok=False, problems=["same_pid_namespace"])])
    run, data = verdict_of([log(), failed])
    assert run.verdict == "unknown"
    assert data["isolation"] == "unproven"
    assert "proof_failed" in data["unproven"]
    assert "same_pid_namespace" in data["unproven"]


def test_o97_a_generation_with_no_cell_and_a_log_that_is_not_intact_keeps_its_own_reason():
    """O97: `no_proof` is only forgiven for a log that is whole; a damaged one stays unproven."""
    data = aggregate([log(), b"not a log\n"], NONCE)
    assert data["isolation"] == "unproven"
    assert "no_proof" in data["unproven"]


def test_o97_the_isolation_of_the_run_needs_a_proof_whichever_generations_were_listed():
    """O97: a combined snapshot never says proven with no proof in it."""
    data = aggregate([log(**NO_CELL), log()], NONCE)
    assert data["isolation"] == "proven"
    assert data["proofs"] == 1


def test_o97_the_ptrace_scope_is_the_strictest_of_the_generations():
    """O97: the kernel's setting is one number for the run: the highest seen."""
    assert aggregate([log(ptrace_scope=0), log(ptrace_scope=2)], NONCE)["ptrace_scope"] == 2
    assert aggregate([log(ptrace_scope=None), log(ptrace_scope=1)], NONCE)["ptrace_scope"] == 1


def test_o97_the_judge_reads_the_verdict_from_the_generations_not_the_top_level_alone():
    """O97: a top level that says clean over a generation that is flagged is no snapshot, so it reads unknown."""
    data = aggregate([log(), log([inotify("dir", "IN_OPEN")])], NONCE)
    forged = copy.deepcopy(data)
    forged["accesses"] = {"token_file": 0, "token_dir": 0}
    run = judge(record(instruments={NAMESPACE: forged}), "control")
    assert run.verdict == "unknown"
    assert run.unknown == ("not_a_snapshot",)


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.update({GENERATIONS: d[GENERATIONS][:1]}),  # one generation under the key
        lambda d: d.update({GENERATIONS: "x"}),
        lambda d: d[GENERATIONS][0].update({"extra": 1}),
        lambda d: d[GENERATIONS][0].update({GENERATIONS: []}),  # nested
        lambda d: d.update({"extra": 1}),
        lambda d: d.update({"log": "unknown"}),  # the top level differs from the generations
    ],
)
def test_o97_a_snapshot_whose_generations_are_not_whole_is_not_a_snapshot(damage):
    """O97: generations that are fewer than two, not a list, not snapshots or not what the top level says are damage."""
    data = copy.deepcopy(aggregate([log(), log()], NONCE))
    damage(data)
    assert judge(record(instruments={NAMESPACE: data}), "control").unknown == ("not_a_snapshot",)


def test_o97_combine_of_one_snapshot_is_that_snapshot():
    """O97: `combine` of a single generation changes nothing."""
    one = snapshot(read_log(log(), NONCE))
    assert combine([one]) == one


def test_o97_the_instrument_reads_every_generation_of_a_source_that_lists_them():
    """O97: a source that returns the logs in order gives the instrument a snapshot with each generation."""

    class Source:
        def mount_watch_log(self):
            return [log(), log([inotify("dir", "IN_OPEN")])]

    data = MountGuard(Source(), NONCE).finish()
    assert len(data[GENERATIONS]) == 2
    assert data["accesses"]["token_dir"] == 1


def test_o97_the_instrument_reads_a_source_that_gives_no_log_as_missing():
    """O97: a source with an empty list of logs is one missing log: unknown, never clean."""

    class Source:
        def mount_watch_log(self):
            return []

    data = MountGuard(Source(), NONCE).finish()
    assert data["log"] == "unknown"
    assert "log_missing" in data["unknown"]
