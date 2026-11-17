"""S20: the read-log part of an analysis (`expcore.readlog.sensitivity`): every analysed run judged once a batch's
records carry the `read_log` instrument, and the preregistered analysis again without the flagged runs, as a
sensitivity analysis."""

import pytest
from egress_logs import record
from readlog_support import FORGER, preregistration, rows_of_pairs, runs_for

from expcore.readlog import sensitivity
from expcore.readlog.sensitivity import LEFT_OUT_AS, checked, read_log_analysis
from expcore.stats.analysis import ContrastRefused, analyse


@pytest.fixture
def prereg():
    return preregistration()


def test_a_batch_whose_records_carry_no_read_log_instrument_is_not_checked(prereg):
    """S20: no verdicts and no section, so the analysis and the report of any other batch are what they were."""
    rows = rows_of_pairs(4)
    runs = runs_for(rows, lambda i: [FORGER], instruments={})
    assert read_log_analysis(runs, rows, [], prereg) is None
    assert checked([run for run, _ in runs]) is False
    assert checked([run for run, _ in runs_for(rows, lambda i: [])]) is True


def test_once_one_record_carries_the_instrument_every_run_is_judged(prereg):
    """S20: a run whose record lacks the instrument is judged too, by its code, as every other run is."""
    rows = rows_of_pairs(4)
    runs = runs_for(rows, lambda i: [FORGER] if i == 1 else ["print(1)"])
    runs[2] = (record(run_key=rows[2].run_key, cells=[FORGER]), rows[2].condition)  # no instrument on this one
    done = read_log_analysis(runs, rows, [], prereg)
    assert [run.run_key for run in done.runs] == [row.run_key for row in rows]
    assert {run.run_key for run in done.flagged} == {rows[1].run_key, rows[2].run_key}


def test_with_no_run_flagged_there_is_no_second_analysis(prereg):
    rows = rows_of_pairs(4)
    done = read_log_analysis(runs_for(rows, lambda i: ["print(1)"]), rows, [], prereg)
    assert done.flagged == ()
    assert (done.sensitivity, done.refused) == (None, None)
    assert done.counts()["flagged"] == 0


def test_the_sensitivity_analysis_is_the_analysis_without_the_flagged_runs(prereg):
    """S20: two flagged runs, each in its own pair: the rerun uses the other pairs and counts the two as left out, in
    the primary analysis's own vocabulary of causes."""
    rows = rows_of_pairs(8)
    done = read_log_analysis(runs_for(rows, lambda i: [FORGER] if i in {0, 3} else ["print(1)"]), rows, [], prereg)
    primary = analyse(rows, prereg).contrast("a-vs-b")
    again = done.sensitivity.contrast("a-vs-b")
    assert primary.n_pairs == 8
    assert again.n_pairs == 6
    assert again.pairs_missing["unpaired_or_left_out"] == 2
    assert [a.lost[LEFT_OUT_AS] for a in again.arm_runs] == [1, 1]
    assert {r.run_key for r in done.flagged} == {rows[0].run_key, rows[3].run_key}


def test_the_sensitivity_analysis_is_deterministic(prereg):
    rows = rows_of_pairs(8)
    runs = runs_for(rows, lambda i: [FORGER] if i == 2 else ["print(1)"])
    first = read_log_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    second = read_log_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    assert (first.estimate, first.ci_low, first.ci_high, first.p_value) == (
        second.estimate,
        second.ci_low,
        second.ci_high,
        second.p_value,
    )


def test_a_contrast_left_with_no_pair_is_untestable_in_the_sensitivity_analysis(prereg):
    rows = rows_of_pairs(2)
    done = read_log_analysis(runs_for(rows, lambda i: [FORGER] if i in (0, 2) else ["print(1)"]), rows, [], prereg)
    assert done.sensitivity.contrast("a-vs-b").conclusion == "untestable"


def test_a_record_paired_with_another_runs_row_is_refused(prereg):
    rows = rows_of_pairs(2)
    runs = runs_for(rows, lambda i: ["print(1)"])
    runs[0], runs[1] = runs[1], runs[0]
    with pytest.raises(ValueError, match="paired with the row"):
        read_log_analysis(runs, rows, [], prereg)


def test_a_different_number_of_records_and_rows_is_refused(prereg):
    rows = rows_of_pairs(2)
    with pytest.raises(ValueError, match="analysed record"):
        read_log_analysis(runs_for(rows, lambda i: ["print(1)"])[:-1], rows, [], prereg)


def test_a_rerun_the_kernel_refuses_keeps_the_primary_and_says_why(prereg, monkeypatch):
    def refuse(*args, **kwargs):
        raise ContrastRefused("a treatment attempt's outcome is None")

    monkeypatch.setattr(sensitivity, "analyse", refuse)
    rows = rows_of_pairs(4)
    done = read_log_analysis(runs_for(rows, lambda i: [FORGER] if i == 0 else ["print(1)"]), rows, [], prereg)
    assert done.sensitivity is None
    assert done.refused == "a treatment attempt's outcome is None"


def test_the_counts_are_by_flag_state_and_by_kind(prereg):
    rows = rows_of_pairs(4)
    cells = {0: [FORGER], 1: ["open('/proc/1/environ')"], 2: ["x = 'readlog'"]}
    done = read_log_analysis(runs_for(rows, lambda i: cells.get(i, ["print(1)"])), rows, [], prereg)
    assert done.counts() == {
        "analysed": len(rows),
        "flagged": 3,
        "channel_path": 1,
        "client": 0,
        "unix_datagram": 1,
        "proc_environ": 1,
        "channel_discovery": 0,
        "channel_patch": 0,
        "channel_name": 1,
    }
