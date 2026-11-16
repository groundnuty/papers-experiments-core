"""The mount-guard part of an analysis: every run judged once a batch is checked, and the analysis again without the
runs that are not clean, as a sensitivity analysis."""

import tomllib

import pytest
from mountguard_logs import NONCE, inotify, log, proof, record
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.mountguard import NAMESPACE, MountGuard
from expcore.mountguard.sensitivity import LEFT_OUT_AS, mount_guard_analysis
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

    def mount_watch_log(self):
        return self.data


def guarded(data):
    return {NAMESPACE: MountGuard(Source(data), NONCE).finish()}


def clean():
    return guarded(log())


def touched():
    return guarded(log([inotify("dir", "IN_OPEN", "IN_ISDIR")]))


def unproven():
    return guarded(log(proofs=[proof(ok=False, problems=["same_pid_namespace"])]))


def runs_for(rows, instruments_of) -> list:
    return [(record(run_key=row.run_key, instruments=instruments_of(i)), row.condition) for i, row in enumerate(rows)]


def test_a_batch_whose_records_carry_no_mount_guard_is_not_checked(prereg):
    """A Docker batch: no verdicts and no section, so its report is the text it always was."""
    rows = rows_of_pairs(4)
    assert mount_guard_analysis(runs_for(rows, lambda i: {}), rows, [], prereg) is None


def test_once_one_record_carries_it_a_record_without_it_is_unknown_and_left_out(prereg):
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: clean() if i else {}), rows, [], prereg)
    assert [run.run_key for run in done.left_out] == [rows[0].run_key]
    assert done.left_out[0].verdict == "unknown"
    assert done.left_out[0].unknown == ("not_recorded",)


def test_with_every_run_clean_there_is_no_second_analysis(prereg):
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: clean()), rows, [], prereg)
    assert done.left_out == ()
    assert (done.sensitivity, done.refused) == (None, None)
    assert done.counts()["clean"] == 8


def test_the_sensitivity_analysis_leaves_out_the_flagged_and_the_unknown_runs(prereg):
    """O60: one run touched the token's directory, one has an unproven isolation; each in its own pair."""
    rows = rows_of_pairs(8)
    by_index = {0: touched, 3: unproven}
    done = mount_guard_analysis(runs_for(rows, lambda i: by_index.get(i, clean)()), rows, [], prereg)
    primary = analyse(rows, prereg).contrast("a-vs-b")
    again = done.sensitivity.contrast("a-vs-b")
    assert primary.n_pairs == 8
    assert again.n_pairs == 6
    assert again.pairs_missing["unpaired_or_left_out"] == 2
    assert [a.lost[LEFT_OUT_AS] for a in again.arm_runs] == [1, 1]
    counts = done.counts()
    assert (counts["flagged"], counts["unknown"], counts["clean"], counts["left_out"]) == (1, 1, 14, 2)
    assert (counts["token_access"], counts["isolation_unproven"]) == (1, 1)


def test_the_sensitivity_analysis_is_deterministic(prereg):
    rows = rows_of_pairs(8)
    runs = runs_for(rows, lambda i: touched() if i == 2 else clean())
    first = mount_guard_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    second = mount_guard_analysis(runs, rows, [], prereg).sensitivity.contrast("a-vs-b")
    assert (first.estimate, first.ci_low, first.ci_high, first.p_value) == (
        second.estimate,
        second.ci_low,
        second.ci_high,
        second.p_value,
    )


def test_a_record_paired_with_another_runs_row_is_refused(prereg):
    rows = rows_of_pairs(2)
    runs = runs_for(rows, lambda i: clean())
    runs[0], runs[1] = runs[1], runs[0]
    with pytest.raises(ValueError, match="paired with the row"):
        mount_guard_analysis(runs, rows, [], prereg)


def test_a_rerun_the_kernel_refuses_keeps_the_primary_and_says_why(prereg, monkeypatch):
    from expcore.mountguard import sensitivity
    from expcore.stats.analysis import ContrastRefused

    def refuse(*args, **kwargs):
        raise ContrastRefused("a treatment attempt's outcome is None")

    monkeypatch.setattr(sensitivity, "analyse", refuse)
    rows = rows_of_pairs(4)
    done = mount_guard_analysis(runs_for(rows, lambda i: touched() if i == 0 else clean()), rows, [], prereg)
    assert done.sensitivity is None
    assert done.refused == "a treatment attempt's outcome is None"
