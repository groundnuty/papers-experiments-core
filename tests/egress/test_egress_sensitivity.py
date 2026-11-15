"""The egress part of an analysis: every run judged once a batch is checked, and the analysis again without the flagged
runs, as a sensitivity analysis."""

import tomllib

import pytest
from egress_logs import CANARY, NONCE, log, rec, record
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.egress import NAMESPACE, EgressLog, declaration
from expcore.egress.sensitivity import LEFT_OUT_AS, egress_analysis
from expcore.stats.analysis import analyse
from expcore.stats.prereg import parse

SCHEMA = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
TEXT = """\
alpha = 0.05
holm_family = ["a-vs-b"]

[bootstrap]
replicates = 200
seed = 5

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""
DECLARED = declaration([CANARY], CANARY)


@pytest.fixture
def prereg():
    return parse(tomllib.loads(TEXT), "inline", "a" * 64, SCHEMA)


def rows_of_pairs(n: int) -> list:
    """`n` pairs in `n` clusters, cond-a ahead of cond-b by 0.2."""
    rows = []
    for i in range(n):
        rows += both_arms(f"t{i}", "m", 1, 0.7 + 0.01 * i, 0.5 + 0.01 * i, cluster=f"g{i}", arm="a")
    for row in rows:  # the factor schema's arm follows the condition
        row.factors["arm"] = row.condition[-1]
    return rows


class Source:
    def __init__(self, data):
        self.data = data

    def egress_log(self):
        return self.data


def clean():
    return {NAMESPACE: EgressLog(Source(log([])), DECLARED, NONCE).finish()}


def outside():
    return {NAMESPACE: EgressLog(Source(log([rec("connect", "192.0.2.1", 443)])), DECLARED, NONCE).finish()}


def runs_for(rows, instruments_of) -> list:
    return [(record(run_key=row.run_key, instruments=instruments_of(i)), row.condition) for i, row in enumerate(rows)]


def test_a_batch_whose_records_carry_no_egress_instrument_is_not_checked(prereg):
    """A Docker batch: no verdicts and no section, so its report is the text it always was."""
    rows = rows_of_pairs(4)
    assert egress_analysis(runs_for(rows, lambda i: {}), rows, [], prereg) is None


def test_once_one_record_carries_it_a_record_without_it_is_flagged_as_unknown(prereg):
    rows = rows_of_pairs(4)
    done = egress_analysis(runs_for(rows, lambda i: clean() if i else {}), rows, [], prereg)
    assert [run.run_key for run in done.flagged] == [rows[0].run_key]
    assert done.flagged[0].unknown == ("not_recorded",)


def test_with_no_run_flagged_there_is_no_second_analysis(prereg):
    rows = rows_of_pairs(4)
    done = egress_analysis(runs_for(rows, lambda i: clean()), rows, [], prereg)
    assert done.flagged == ()
    assert (done.sensitivity, done.refused) == (None, None)


def test_the_sensitivity_analysis_is_the_analysis_without_the_flagged_runs(prereg):
    """Two flagged runs, each in its own pair: the rerun uses the other pairs and counts the two as left out."""
    rows = rows_of_pairs(8)
    flagged = {0, 3}
    done = egress_analysis(runs_for(rows, lambda i: outside() if i in flagged else clean()), rows, [], prereg)
    primary = analyse(rows, prereg).contrast("a-vs-b")
    again = done.sensitivity.contrast("a-vs-b")
    assert primary.n_pairs == 8
    assert again.n_pairs == 6
    assert again.pairs_missing["unpaired_or_left_out"] == 2
    assert [a.lost[LEFT_OUT_AS] for a in again.arm_runs] == [1, 1]
    assert {r.run_key for r in done.flagged} == {rows[0].run_key, rows[3].run_key}


def test_the_sensitivity_analysis_is_deterministic(prereg):
    rows = rows_of_pairs(8)
    runs = runs_for(rows, lambda i: outside() if i == 2 else clean())
    first = egress_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    second = egress_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    assert (first.estimate, first.ci_low, first.ci_high, first.p_value) == (
        second.estimate,
        second.ci_low,
        second.ci_high,
        second.p_value,
    )


def test_a_contrast_left_with_no_pair_is_untestable_in_the_sensitivity_analysis(prereg):
    rows = rows_of_pairs(2)
    done = egress_analysis(runs_for(rows, lambda i: outside() if i in (0, 2) else clean()), rows, [], prereg)
    assert done.sensitivity.contrast("a-vs-b").conclusion == "untestable"


def test_a_record_paired_with_another_runs_row_is_refused(prereg):
    rows = rows_of_pairs(2)
    runs = runs_for(rows, lambda i: clean())
    runs[0], runs[1] = runs[1], runs[0]
    with pytest.raises(ValueError, match="paired with the row"):
        egress_analysis(runs, rows, [], prereg)


def test_a_rerun_the_kernel_refuses_keeps_the_primary_and_says_why(prereg, monkeypatch):
    from expcore.egress import sensitivity
    from expcore.stats.analysis import ContrastRefused

    def refuse(*args, **kwargs):
        raise ContrastRefused("a treatment attempt's outcome is None")

    monkeypatch.setattr(sensitivity, "analyse", refuse)
    rows = rows_of_pairs(4)
    done = egress_analysis(runs_for(rows, lambda i: outside() if i == 0 else clean()), rows, [], prereg)
    assert done.sensitivity is None
    assert done.refused == "a treatment attempt's outcome is None"
