"""Pairing: named-condition and factor contrasts, aggregation per cluster, and what is dropped and counted."""

import dataclasses
import functools
import math
import random

import numpy as np
import pytest
from stats_support import both_arms, by_factor, make_row, named, ratio_arms, ratio_row, ratio_spec

from expcore.stats.estimators import WILCOXON
from expcore.stats.pairing import MISSING_CAUSES, ContrastSpec, pair_rows
from expcore.stats.ratio import POOLED, RATIO_CAUSES, RatioForm
from expcore.stats.rows import AbsentRun, Row


def bad_row(outcomes, attributes):
    """A row for condition cond-a with the given outcomes and task attributes, as it would be if it got past `Row`.

    A row refuses a non-number, NaN or an infinity when it is built, but its outcomes are a plain dict that an edit can
    still change, so pairing checks the values it reads as well. The outcomes are put in after construction.
    """
    factors = {"task": "task-a", "model": "model-a", "seed": 1}
    row = Row("task=task-a|model=model-a|seed=1|condition=cond-a", "cond-a", factors, {}, attributes)
    row.outcomes.update(outcomes)
    return row


def basic_rows():
    """Two clusters by two models; the hand-computed matrix is [[0.15, nan], [0.4, 0.0]]."""
    rows = []
    rows += both_arms("task-a", "model-a", 1, 0.9, 0.5)  # difference 0.4
    rows += both_arms("task-a", "model-a", 2, 0.7, 0.5)  # 0.2, so (task-a, model-a) is the mean 0.3
    rows += both_arms("task-b", "model-a", 1, 0.5, 0.5)  # 0.0, so (grp-a, model-a) is the mean of 0.3 and 0.0
    rows += both_arms("task-c", "model-a", 1, 0.6, 0.2, cluster="grp-b")  # 0.4
    rows += both_arms("task-c", "model-b", 1, 0.3, 0.3, cluster="grp-b")  # 0.0
    return rows


def test_a_named_condition_contrast_pairs_on_task_model_and_seed():
    """Differences A - B are aggregated seeds to (task, model), then tasks to (cluster, model)."""
    pairing = pair_rows(basic_rows(), named())
    assert pairing.differences.clusters == ("grp-a", "grp-b")
    assert pairing.differences.models == ("model-a", "model-b")
    assert pairing.differences.matrix == pytest.approx(np.array([[0.15, math.nan], [0.4, 0.0]]), nan_ok=True)
    assert (pairing.n_pairs, pairing.n_pairs_missing, pairing.n_unpaired, pairing.n_clusters_dropped) == (5, 0, 0, 0)


def test_the_result_does_not_depend_on_the_order_of_the_rows():
    """Pairing is a function of the set of rows."""
    rows = basic_rows()
    shuffled = random.Random(3).sample(rows, len(rows))
    first, second = pair_rows(rows, named()), pair_rows(shuffled, named())
    assert first.differences.clusters == second.differences.clusters
    np.testing.assert_array_equal(first.differences.matrix, second.differences.matrix)  # NaN cells match as well


def test_a_named_condition_contrast_ignores_other_conditions_and_other_factors():
    """Rows of a third condition do not enter, and the named form pairs across any other declared factor."""
    rows = both_arms("task-a", "model-a", 1, 0.6, 0.2, gate="open")
    rows.append(make_row("task-a", "model-a", 1, 0.99, condition="cond-c", gate="open"))
    other = [
        make_row("task-b", "model-a", 1, 0.9, condition="cond-a", gate="open"),
        make_row("task-b", "model-a", 1, 0.5, condition="cond-b", gate="shut"),
    ]
    pairing = pair_rows(rows + other, named())
    assert pairing.differences.matrix == pytest.approx(np.array([[0.4]]))  # (0.4 for task-a and 0.4 for task-b)
    assert pairing.n_pairs == 2


def test_the_contrast_is_a_minus_b():
    """Swapping the arms changes the sign and nothing else."""
    forward = pair_rows(basic_rows(), named()).differences.matrix
    backward = pair_rows(basic_rows(), named(a="cond-b", b="cond-a")).differences.matrix
    assert backward == pytest.approx(-forward, nan_ok=True)


def test_a_factor_contrast_pairs_on_every_other_factor():
    """Open against shut is paired within each level of the other factor, never across it."""
    rows = [
        make_row("task-a", "model-a", 1, 0.8, gate="open", mode="x"),
        make_row("task-a", "model-a", 1, 0.5, gate="shut", mode="x"),
        make_row("task-a", "model-a", 1, 0.6, gate="open", mode="y"),
        make_row("task-a", "model-a", 1, 0.5, gate="shut", mode="y"),
        make_row("task-a", "model-a", 1, 0.9, gate="open", mode="z"),  # no shut counterpart at mode z
        make_row("task-a", "model-a", 1, 0.1, gate="shut", mode="w"),  # nor does this one have an open one
    ]
    pairing = pair_rows(rows, by_factor())
    assert pairing.differences.matrix == pytest.approx(np.array([[0.2]]))  # mean of 0.3 and 0.1
    assert (pairing.n_pairs, pairing.n_unpaired) == (2, 2)


def test_a_factor_contrast_needs_no_condition_and_ignores_levels_it_does_not_name():
    """The factor form reads the factor values; a third level is left out of the A - B pairs."""
    rows = [
        make_row("task-a", "model-a", 1, 0.8, gate="open"),
        make_row("task-a", "model-a", 1, 0.5, gate="shut"),
        make_row("task-a", "model-a", 1, 0.0, gate="half"),
    ]
    assert pair_rows(rows, by_factor()).differences.matrix == pytest.approx(np.array([[0.3]]))
    assert pair_rows(rows, by_factor(levels=("open", "half"))).differences.matrix == pytest.approx(np.array([[0.8]]))


def test_s03_clusters_without_a_pair_are_dropped_and_counted():
    """S03: clusters undefined in either arm are dropped and counted, whether unpaired or all missing."""
    rows = basic_rows()
    rows.append(
        make_row("task-d", "model-a", 1, 0.9, condition="cond-a", cluster="grp-c")
    )  # grp-c has an A row and no B row
    rows += both_arms("task-e", "model-a", 1, None, 0.3, cluster="grp-d")  # grp-d has a pair whose A outcome is missing
    pairing = pair_rows(rows, named())
    assert pairing.differences.clusters == ("grp-a", "grp-b")
    assert pairing.n_clusters_dropped == 2
    assert pairing.n_unpaired == 1
    assert pairing.n_pairs_missing == 1


def test_r10_a_missing_outcome_is_excluded_from_its_pair_and_never_read_as_zero():
    """R10: None is missing; the pair is dropped and counted, and the mean is not pulled toward 0."""
    rows = both_arms("task-a", "model-a", 1, None, 0.2) + both_arms("task-a", "model-a", 2, 0.5, 0.2)
    pairing = pair_rows(rows, named())
    assert pairing.differences.matrix == pytest.approx(np.array([[0.3]]))  # a zero for the missing A would give 0.05
    assert (pairing.n_pairs, pairing.n_pairs_missing) == (1, 1)


def test_r10_a_missing_outcome_on_the_b_side_and_a_nan_that_got_past_the_row_are_missing_too():
    """R10: missing on either side counts, and pairing still reads a NaN edited into a row as missing, like None."""
    nan_row = both_arms("task-a", "model-a", 2, 0.5, 0.2)
    nan_row[0].outcomes["score"] = math.nan  # a row refuses NaN when built; this is an edit after the fact
    rows = both_arms("task-a", "model-a", 1, 0.9, None) + nan_row + both_arms("task-a", "model-a", 3, 0.6, 0.2)
    pairing = pair_rows(rows, named())
    assert pairing.differences.matrix == pytest.approx(np.array([[0.4]]))
    assert (pairing.n_pairs, pairing.n_pairs_missing) == (1, 2)


def test_s05_an_outcome_that_is_missing_everywhere_leaves_no_cluster_not_a_zero_effect():
    """S05: with every outcome missing there is nothing to estimate, so the matrix is empty rather than zero."""
    pairing = pair_rows(
        both_arms("task-a", "model-a", 1, None, None) + both_arms("task-b", "model-a", 1, None, 0.1), named()
    )
    assert pairing.differences.matrix.shape == (0, 0)
    assert pairing.n_clusters_dropped == 1
    assert pairing.n_pairs_missing == 2


def test_a_seed_is_compared_as_text_so_1_and_the_string_1_are_one_seed():
    """The run key writes the seed canonically; an int and its decimal string name the same pair."""
    rows = [
        make_row("task-a", "model-a", 1, 0.9, condition="cond-a"),
        make_row("task-a", "model-a", "1", 0.4, condition="cond-b"),
    ]
    assert pair_rows(rows, named()).n_pairs == 1


def test_two_rows_with_one_run_key_in_an_arm_are_refused():
    """Run keys are unique; a repeated attempt is not averaged away."""
    rows = [*both_arms("task-a", "model-a", 1, 0.9, 0.5), make_row("task-a", "model-a", 1, 0.8, condition="cond-a")]
    with pytest.raises(
        ValueError, match=r"share one run key \(run 'task=task-a\|model=model-a\|seed=1\|condition=cond-a'\)"
    ):
        pair_rows(rows, named())


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (bad_row({}, {"group": "grp-a"}), "has no outcome 'score'"),
        (bad_row({"score": 0.1}, {}), "has no cluster attribute 'group'"),
        (bad_row({"score": True}, {"group": "grp-a"}), "must be a number or None"),
        (bad_row({"score": "0.1"}, {"group": "grp-a"}), "must be a number or None"),
        (bad_row({"score": math.inf}, {"group": "grp-a"}), "is infinite"),
    ],
)
def test_a_malformed_row_is_refused_and_named(row, message):
    """Validate at the boundary: the message names the contrast and the run, and never echoes an outcome."""
    rows = [row, make_row("task-a", "model-a", 1, 0.5, condition="cond-b")]
    with pytest.raises(ValueError, match=message) as caught:
        pair_rows(rows, named())
    assert "'a-vs-b'" in str(caught.value)
    assert "run 'task=task-a|model=model-a|seed=1" in str(caught.value)


def test_a_factor_contrast_refuses_a_row_without_the_factor():
    """The varying factor must be declared on every row the contrast reads."""
    with pytest.raises(ValueError, match="has no factor 'gate'"):
        pair_rows([make_row("task-a", "model-a", 1, 0.5)], by_factor())


def test_a_task_in_two_clusters_is_refused():
    """The unit of inference is unambiguous: one task, one cluster."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5) + both_arms("task-a", "model-a", 2, 0.9, 0.5, cluster="grp-b")
    with pytest.raises(ValueError, match="task 'task-a' belongs to two clusters"):
        pair_rows(rows, named())


def test_a_spec_has_exactly_one_form_with_two_different_arms():
    """A contrast is two conditions or a factor with two levels, and never the same arm twice."""
    with pytest.raises(ValueError, match="not both"):
        ContrastSpec("x", "score", "est", "group", conditions=("a", "b"), factor="gate", levels=("open", "shut"))
    with pytest.raises(ValueError, match="give two conditions, or a factor with two levels"):
        ContrastSpec("x", "score", "est", "group", factor="gate")
    with pytest.raises(ValueError, match="two different names"):
        ContrastSpec("x", "score", "est", "group", conditions=("a", "a"))
    with pytest.raises(ValueError, match="two different names"):
        ContrastSpec("x", "score", "est", "group", factor="gate", levels=("open", "open"))


def test_a_task_one_arm_did_not_run_is_dropped_named_and_counted_and_its_runs_are_unpaired():
    """A condition that runs on some tasks only: the tasks missing in either arm leave the contrast, by name."""
    rows = [
        *both_arms("task-a", "model-a", 1, 0.9, 0.5, cluster="grp-a"),
        make_row("task-b", "model-a", 1, 0.8, condition="cond-a", cluster="grp-a"),  # task-b has no B run
        make_row("task-b", "model-a", 2, 0.8, condition="cond-a", cluster="grp-a"),
        make_row("task-c", "model-a", 1, 0.2, condition="cond-b", cluster="grp-b"),  # task-c has no A run
        *both_arms("task-d", "model-a", 1, 0.7, 0.4, cluster="grp-b"),
    ]
    pairing = pair_rows(rows, named())
    assert pairing.tasks_dropped == ("task-b", "task-c")
    assert pairing.n_tasks_dropped == 2
    assert pairing.n_unpaired == 3  # the three runs on the dropped tasks
    assert pairing.differences.matrix == pytest.approx(np.array([[0.4], [0.3]]))  # only task-a and task-d count


def test_a_cluster_whose_tasks_are_all_dropped_is_dropped_with_them():
    """The clusters of the dropped tasks are counted as dropped when nothing else keeps them."""
    rows = [
        *both_arms("task-a", "model-a", 1, 0.9, 0.5, cluster="grp-a"),
        make_row("task-b", "model-a", 1, 0.8, condition="cond-a", cluster="grp-b"),
        make_row("task-c", "model-a", 1, 0.8, condition="cond-a", cluster="grp-b"),
    ]
    pairing = pair_rows(rows, named())
    assert pairing.differences.clusters == ("grp-a",)
    assert pairing.n_clusters_dropped == 1
    assert pairing.tasks_dropped == ("task-b", "task-c")


def test_no_task_is_dropped_when_both_arms_run_the_same_ones_even_with_runs_missing():
    """A run missing on a task both arms ran is an unpaired run, not a dropped task."""
    rows = [*both_arms("task-a", "model-a", 1, 0.9, 0.5), make_row("task-a", "model-a", 2, 0.4, condition="cond-a")]
    pairing = pair_rows(rows, named())
    assert (pairing.tasks_dropped, pairing.n_unpaired) == ((), 1)


def test_a_factor_contrast_drops_tasks_the_same_way():
    """The count is by arm, whichever way the arms are named."""
    rows = [
        make_row("task-a", "model-a", 1, 0.8, gate="open"),
        make_row("task-a", "model-a", 1, 0.5, gate="shut"),
        make_row("task-b", "model-a", 1, 0.8, gate="open"),
    ]
    assert pair_rows(rows, by_factor()).tasks_dropped == ("task-b",)


@pytest.mark.parametrize("margin", [0, -0.1, math.inf, math.nan, "wide", True])
def test_a_spec_refuses_an_equivalence_margin_that_is_not_a_positive_finite_number(margin):
    """The margin is checked where the contrast is built, whoever builds it."""
    with pytest.raises(ValueError, match="equivalence margin must be a positive finite number"):
        ContrastSpec("x", "score", "est", "group", conditions=("a", "b"), equivalence_margin=margin)


def test_a_spec_keeps_a_valid_margin_and_defaults_to_none():
    """No margin unless one is declared."""
    assert ContrastSpec("x", "score", "est", "group", conditions=("a", "b")).equivalence_margin is None
    assert (
        ContrastSpec("x", "score", "est", "group", conditions=("a", "b"), equivalence_margin=0.1).equivalence_margin
        == 0.1
    )


# --- treatment attempts and the pairs a contrast is missing ---------------------------------------------------------


def treated(task, model, seed, a, b, *, cluster="grp-a"):
    """A pair whose cond-a run is a treatment attempt scored `a` and whose cond-b run is an ordinary one scored `b`."""
    first, second = both_arms(task, model, seed, a, b, cluster=cluster)
    return [dataclasses.replace(first, treatment=True), second]


def absent(task, condition, reason, *, model="model-a", seed=1):
    """A planned run of `condition` with no analysed attempt, as the export lists it, and why it has none."""
    factors = {"task": task, "model": model, "seed": seed}
    return AbsentRun(f"task={task}|model={model}|seed={seed}|condition={condition}", condition, factors, reason)


@pytest.mark.parametrize("reason", ["exhausted", "unfinished"])
def test_s10_a_treatment_attempt_left_none_is_counted_as_such_whatever_became_of_its_partner(reason):
    """S10: the manipulation left its contrast because of the scorer, so the partner's fate must not hide it."""
    first, _ = treated("task-a", "model-a", 1, None, 0.5)
    pairing = pair_rows([first], named(), [absent("task-a", "cond-b", reason)])
    assert pairing.missing["treatment_outcome_none"] == 1
    assert (pairing.missing["exhausted"], pairing.missing["unpaired_or_left_out"]) == (0, 0)
    assert (pairing.n_pairs, pairing.n_treatment_measured) == (0, 0)


def test_s10_a_measured_treatment_attempt_or_an_ordinary_none_beside_an_exhausted_partner_stays_exhausted():
    """S10: the scorer did nothing wrong there, so the pair is missing because of the exhausted run."""
    measured, _ = treated("task-a", "model-a", 1, 0.2, 0.5)
    ordinary, _ = both_arms("task-b", "model-a", 1, None, 0.5)
    partners = [absent("task-a", "cond-b", "exhausted"), absent("task-b", "cond-b", "exhausted")]
    pairing = pair_rows([measured, ordinary], named(), partners)
    assert (pairing.missing["exhausted"], pairing.missing["treatment_outcome_none"]) == (2, 0)


def test_s10_under_excluded_a_treatment_attempt_with_a_missing_partner_counts_as_excluded():
    """S10: the preregistration leaves out every pair with a treatment attempt, whatever became of the other run."""
    first, _ = treated("task-a", "model-a", 1, None, 0.5)
    spec = dataclasses.replace(named(), treatment_outcomes="excluded")
    pairing = pair_rows([first], spec, [absent("task-a", "cond-b", "unfinished")])
    assert (pairing.missing["treatment_excluded"], pairing.missing["treatment_outcome_none"]) == (1, 0)
    assert pairing.missing["unpaired_or_left_out"] == 0


def test_s10_a_treatment_attempt_on_a_task_the_other_arm_never_planned_has_no_pair_to_leave():
    """S10: a chain contrast's extra task is outside its pairs, so a None there costs the contrast nothing."""
    first, _ = treated("task-a", "model-a", 1, None, 0.5)
    pairing = pair_rows([first], named())
    assert (pairing.missing["unpaired_or_left_out"], pairing.missing["treatment_outcome_none"]) == (1, 0)
    assert pairing.n_treatment_measured == 0


def test_s10_an_ordinary_run_on_a_task_only_one_arm_runs_may_lack_the_contrasts_outcome():
    """S10: such a run stays unpaired, as it always was: only a treatment attempt of a planned pair is read."""
    first, _ = both_arms("task-a", "model-a", 1, 0.5, 0.5)
    pairing = pair_rows([dataclasses.replace(first, outcomes={})], named())
    assert pairing.missing["unpaired_or_left_out"] == 1


def test_s10_a_treatment_attempt_of_a_planned_pair_without_the_contrasts_outcome_is_refused():
    """S10: a contrast on an outcome the scorer never gives is refused for a treatment attempt, partner or none."""
    first, _ = treated("task-a", "model-a", 1, 0.5, 0.5)
    with pytest.raises(ValueError, match="has no outcome 'score'"):
        pair_rows([dataclasses.replace(first, outcomes={})], named(), [absent("task-a", "cond-b", "unfinished")])


def test_s10_a_measured_treatment_attempt_counts_whatever_became_of_its_pair():
    """S10: what tells a contrast that measured some treatment attempts from one that measured none: paired, beside
    an ordinary None, or with an unfinished partner, but not on a task outside the contrast's pairs."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5) + treated("task-b", "model-a", 1, 0.0, 0.5)
    rows += treated("task-c", "model-a", 1, 0.0, None)
    rows += treated("task-d", "model-a", 1, 0.0, 0.5)[:1] + treated("task-e", "model-a", 1, 0.0, 0.5)[:1]
    pairing = pair_rows(rows, named(), [absent("task-d", "cond-b", "unfinished")])
    assert (pairing.n_pairs, pairing.n_treatment_measured) == (2, 3)


def test_s10_a_pair_dropped_because_a_treatment_attempts_outcome_is_none_is_counted_apart():
    """S10: a treatment attempt scored None takes its pair out of the contrast, and that drop is counted under its own
    cause, apart from a None on an ordinary run; a scored treatment attempt is paired like any other run."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows += treated("task-b", "model-a", 1, None, 0.5)  # the treatment attempt's outcome is None
    rows += both_arms("task-c", "model-a", 1, None, 0.5)  # an ordinary run's outcome is None
    rows += treated("task-d", "model-a", 1, 0.0, None)  # the None is on the ordinary side
    rows += treated("task-e", "model-a", 1, 0.0, 0.5)  # a treatment attempt the scorer measured
    pairing = pair_rows(rows, named())
    assert (pairing.n_pairs, pairing.n_pairs_missing) == (2, 3)
    assert dict(pairing.missing) == {
        "treatment_outcome_none": 1,
        "treatment_excluded": 0,
        "exhausted": 0,
        "unpaired_or_left_out": 0,
        "outcome_none": 2,
    }


def test_s10_a_contrast_that_excludes_treatment_attempts_leaves_out_every_pair_with_one_whatever_its_outcome():
    """S10: under `treatment_outcomes = "excluded"` the preregistration, not the scorer, decides: each pair with a
    treatment attempt is left out and counted as excluded, scored or not."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows += treated("task-b", "model-a", 1, None, 0.5)
    rows += treated("task-c", "model-a", 1, 0.0, 0.5)
    excluded = pair_rows(rows, dataclasses.replace(named(), treatment_outcomes="excluded"))
    assert (excluded.n_pairs, excluded.n_pairs_missing) == (1, 0)
    assert (excluded.missing["treatment_excluded"], excluded.missing["treatment_outcome_none"]) == (2, 0)
    scored = pair_rows(rows, dataclasses.replace(named(), treatment_outcomes="scored"))
    assert (scored.n_pairs, scored.missing["treatment_outcome_none"], scored.missing["treatment_excluded"]) == (2, 1, 0)


@pytest.mark.parametrize("declared", ["", "kept", "Scored", True])
def test_s10_a_spec_refuses_a_treatment_declaration_that_is_not_scored_or_excluded(declared):
    """S10: the declaration says how treatment attempts enter the contrast, in one of two words."""
    with pytest.raises(ValueError, match="treatment_outcomes must be 'scored' or 'excluded'"):
        ContrastSpec("x", "score", "est", "group", conditions=("a", "b"), treatment_outcomes=declared)


def test_c23_a_pair_missing_because_a_run_is_exhausted_is_counted_apart_from_one_unpaired_or_left_out():
    """C23: a pair whose run in either arm exhausted its attempts is missing because of it, even when both runs are
    absent; a pair whose other run is unfinished, left out of a partial analysis or never planned is unpaired or left
    out. Every cause is reported, zeros included."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows.append(make_row("task-b", "model-a", 1, 0.8, condition="cond-a"))  # its cond-b run is exhausted
    rows.append(make_row("task-d", "model-a", 1, 0.8, condition="cond-a"))  # its cond-b run is left out
    rows.append(make_row("task-e", "model-a", 1, 0.8, condition="cond-a"))  # cond-b never planned it
    gone = [
        absent("task-b", "cond-b", "exhausted"),
        absent("task-c", "cond-a", "exhausted"),  # neither run analysed: one exhausted, the other unfinished
        absent("task-c", "cond-b", "unfinished"),
        absent("task-d", "cond-b", "excluded"),
        absent("task-f", "cond-a", "unfinished"),  # nothing of cond-b on task-f
        absent("task-a", "cond-c", "exhausted"),  # another condition: not in this contrast
    ]
    pairing = pair_rows(rows, named(), absent=gone)
    assert pairing.n_pairs == 1
    assert pairing.n_unpaired == 3  # run keys analysed in one arm only, as before
    assert dict(pairing.missing) == {
        "treatment_outcome_none": 0,
        "treatment_excluded": 0,
        "exhausted": 2,
        "unpaired_or_left_out": 3,
        "outcome_none": 0,
    }
    assert pair_rows(rows, named()).missing["unpaired_or_left_out"] == 3  # without the absent runs: all unpaired


def test_c23_an_absent_run_with_an_unknown_reason_or_a_run_key_an_analysed_row_holds_is_refused():
    """C23: an absent run is exhausted, unfinished or excluded, and never also analysed."""
    with pytest.raises(ValueError, match="reason must be one of 'exhausted', 'unfinished', 'excluded'"):
        absent("task-a", "cond-a", "lost")
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    with pytest.raises(ValueError, match="two runs of 'cond-a' share one run key"):
        pair_rows(rows, named(), absent=[absent("task-a", "cond-a", "exhausted")])


# --- the ratio form: sums per cluster, and what the zero rule and a failed run's missing numerator cost ----------


def ratio_rows():
    """grp-a: model-a's A spends 100 + 200 for 1 + 0 wins, its B 50 + 50 for 1 + 1 (ln 6); grp-b: equal ratios (0)."""
    rows = []
    rows += ratio_arms("task-a", "model-a", 1, (100.0, 1.0), (50.0, 1.0))
    rows += ratio_arms("task-b", "model-a", 1, (200.0, 0.0), (50.0, 1.0))  # a failed run of A: its spend counts
    rows += ratio_arms("task-c", "model-a", 1, (100.0, 2.0), (50.0, 1.0), cluster="grp-b")
    return rows


def test_s13_a_ratio_contrast_sums_its_pairs_per_cluster_into_log_ratios():
    """S13: the sums run over every paired run of the cluster, tasks and seeds together, the failed run included."""
    pairing = pair_rows(ratio_rows(), ratio_spec())
    assert pairing.differences.clusters == ("grp-a", "grp-b")
    assert pairing.differences.models == (POOLED,)
    assert pairing.differences.matrix[:, 0] == pytest.approx([math.log(6.0), 0.0])
    assert pairing.n_pairs == 3
    assert pairing.n_pairs_missing == 0


def test_s13_a_ratio_contrast_pools_the_models_of_a_cluster_so_one_models_failures_are_not_excluded():
    """S13: model-b never wins in A, and pooled with model-a's runs its spend still counts: A 300 / 1, B 100 / 2. By
    model, model-b would have no ratio, and dropping it would leave a cost conditional on success."""
    rows = ratio_arms("task-a", "model-a", 1, (100.0, 1.0), (50.0, 1.0))
    rows += ratio_arms("task-a", "model-b", 1, (200.0, 0.0), (50.0, 1.0))
    pairing = pair_rows(rows, ratio_spec())
    assert pairing.differences.matrix[0, 0] == pytest.approx(math.log(6.0))
    assert (pairing.n_pairs, pairing.missing["zero_denominator"]) == (2, 0)


def test_s13_a_ratio_contrast_counts_the_ratio_causes_and_a_difference_contrast_does_not():
    """S13: a ratio contrast's missing pairs hold its three causes beside the others, zeros included; a difference
    contrast's hold exactly the causes they always held, so its outputs are what they were."""
    ratio = pair_rows(ratio_rows(), ratio_spec())
    assert tuple(ratio.missing) == MISSING_CAUSES + RATIO_CAUSES
    assert all(count == 0 for count in ratio.missing.values())
    assert tuple(pair_rows(both_arms("task-a", "model-a", 1, 0.9, 0.5), named()).missing) == MISSING_CAUSES


def test_s13_a_failed_run_whose_numerator_is_none_is_counted_apart_from_any_other_none():
    """S13: a run with 0 wins and spend None would make the ratio a ratio over successes only, so its pair is counted
    under its own cause; spend None on a run that won, and wins None, are ordinary missing outcomes."""
    rows = ratio_rows()
    rows += ratio_arms("task-d", "model-a", 1, (None, 0.0), (50.0, 1.0))
    rows += ratio_arms("task-e", "model-a", 1, (None, 1.0), (50.0, 1.0))
    rows += ratio_arms("task-f", "model-a", 1, (90.0, None), (50.0, 1.0))
    rows += ratio_arms("task-g", "model-a", 1, (90.0, 1.0), (None, 0.0))  # on either arm
    pairing = pair_rows(rows, ratio_spec())
    assert pairing.missing["numerator_none_at_zero"] == 2
    assert pairing.missing["outcome_none"] == 2
    assert pairing.n_pairs_missing == 4
    assert pairing.n_pairs == 3
    assert pairing.differences.matrix[:, 0] == pytest.approx([math.log(6.0), 0.0])  # the lost pairs left the sums


def test_s13_a_treatment_attempt_with_no_numerator_is_counted_as_a_treatment_loss_first():
    """S13, S10: the treatment cause comes first, so the manipulation's loss is never hidden under the ratio's."""
    rows = ratio_rows()
    rows.append(ratio_row("task-d", "model-a", 1, None, 0.0, condition="cond-a", treatment=True))
    rows.append(ratio_row("task-d", "model-a", 1, 50.0, 1.0, condition="cond-b"))
    pairing = pair_rows(rows, ratio_spec())
    assert pairing.missing["treatment_outcome_none"] == 1
    assert pairing.missing["numerator_none_at_zero"] == 0


def test_s13_a_cluster_the_zero_rule_excludes_counts_its_pairs_and_is_dropped():
    """S13: grp-b's B has no win, so its two pairs are counted under zero_denominator, not used, and it is dropped."""
    rows = ratio_rows()[:4]
    rows += ratio_arms("task-c", "model-a", 1, (100.0, 2.0), (50.0, 0.0), cluster="grp-b")
    rows += ratio_arms("task-c", "model-a", 2, (100.0, 2.0), (50.0, 0.0), cluster="grp-b")
    pairing = pair_rows(rows, ratio_spec())
    assert pairing.differences.clusters == ("grp-a",)
    assert pairing.missing["zero_denominator"] == 2
    assert pairing.n_pairs == 2
    assert pairing.n_pairs_missing == 0  # nothing was None: the pairs are out by the declared rule
    assert pairing.n_clusters_dropped == 1


def test_s13_under_a_continuity_correction_no_cluster_is_excluded():
    """S13: the same rows under a declared correction keep grp-b, with the corrected log ratio."""
    rows = ratio_rows()[:4]
    rows += ratio_arms("task-c", "model-a", 1, (100.0, 2.0), (50.0, 0.0), cluster="grp-b")
    pairing = pair_rows(rows, ratio_spec(zero="continuity", correction=0.5))
    assert pairing.differences.clusters == ("grp-a", "grp-b")
    assert pairing.differences.matrix[1, 0] == pytest.approx(math.log((100.5 / 2.5) / (50.5 / 0.5)))
    assert pairing.missing["zero_denominator"] == 0


@pytest.mark.parametrize("outcome", ["spend", "wins"])
def test_s13_a_negative_numerator_or_denominator_is_refused_naming_the_run_and_the_outcome(outcome):
    """S13: a total of costs or counts cannot be negative, and a log ratio of one is no number."""
    rows = ratio_arms("task-a", "model-a", 1, (100.0, 1.0), (50.0, 1.0))
    rows[1].outcomes[outcome] = -1.0  # past the row's own checks, as an edit could leave it
    with pytest.raises(ValueError, match=f"outcome {outcome!r} of run .* is negative"):
        pair_rows(rows, ratio_spec())


def test_a_ratio_contrast_needs_both_of_its_outcomes_on_every_row():
    """A row without the denominator is refused naming the outcome, as a row without a contrast's outcome is."""
    rows = ratio_arms("task-a", "model-a", 1, (100.0, 1.0), (50.0, 1.0))
    del rows[0].outcomes["wins"]
    with pytest.raises(ValueError, match="has no outcome 'wins'"):
        pair_rows(rows, ratio_spec())


def test_a_ratio_contrast_spec_labels_its_outcome_with_its_two_outcomes():
    """The outcome of a ratio contrast is its label, numerator over denominator, so it cannot name a third outcome."""
    form = RatioForm("spend", "wins", "exclude")
    with pytest.raises(ValueError, match="outcome must be 'spend/wins'"):
        ContrastSpec("x", "spend", "ratio", "group", conditions=("cond-a", "cond-b"), ratio=form)


# --- the difference form's units: estimated per (model, group) cell, the group the independent unit ----------------


def test_a_difference_contrast_estimates_per_model_cell_and_takes_the_group_as_its_independent_unit():
    """Two models in one group, model-a with three tasks and four runs and model-b with one task: each model's cell is
    the mean of its tasks' means, and the group, one row, is the equal-weight mean of its two cells, (0.2 + 0.6) / 2 =
    0.4, not the mean over its five runs (0.28) nor over its four tasks (0.3). The group is the one cluster counted."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)  # 0.4
    rows += both_arms("task-a", "model-a", 2, 0.5, 0.5)  # 0.0: task-a's mean is 0.2
    rows += both_arms("task-b", "model-a", 1, 0.5, 0.5)  # 0.0
    rows += both_arms("task-c", "model-a", 1, 0.9, 0.5)  # 0.4: model-a's cell is (0.2 + 0.0 + 0.4) / 3 = 0.2
    rows += both_arms("task-d", "model-b", 1, 0.8, 0.2)  # 0.6: model-b's cell
    pairing = pair_rows(rows, named())
    assert named().estimation_unit == ("model", "group")
    assert (pairing.differences.clusters, pairing.differences.models) == (("grp-a",), ("model-a", "model-b"))
    assert pairing.differences.matrix.ravel().tolist() == pytest.approx([0.2, 0.6])
    assert list(pairing.differences.cluster_differences) == pytest.approx([0.4])
    assert (pairing.n_cells, pairing.cells_lost) == (2, {"no_pair": 0})


def test_a_difference_contrast_counts_a_cell_left_without_a_pair():
    """model-b ran grp-a in arm A only, so its cell is seen and lost for want of a pair; the group keeps model-a's."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows.append(make_row("task-a", "model-b", 1, 0.9, condition="cond-a"))
    pairing = pair_rows(rows, named())
    assert (pairing.n_cells, pairing.cells_lost, pairing.n_clusters_dropped) == (1, {"no_pair": 1}, 0)


# --- the ratio form's estimation unit: the models of a group pooled, or one cell per model of it ----------------------

BY_MODEL = ("model", "group")


def by_model_rows():
    """ratio_rows (model-a: ln 6 in grp-a, 0 in grp-b), and model-b: in grp-a A 60 for 1 win, B 20 for 1 (ln 3); in
    grp-b A 40 for 1, B 20 for 0 wins, a cell with no correct result in B."""
    rows = ratio_rows()
    rows += ratio_arms("task-a", "model-b", 1, (60.0, 1.0), (20.0, 1.0))
    rows += ratio_arms("task-c", "model-b", 1, (40.0, 1.0), (20.0, 0.0), cluster="grp-b")
    return rows


def test_s13_a_cell_estimation_unit_gives_each_group_its_cells_log_ratios_and_excludes_by_cell():
    """S13, hand-computed nested: grp-a holds model-a's ln 6 and model-b's ln(60 / 20) = ln 3, so its value is their
    equal-weight mean; model-b's grp-b cell has no win in B, so that cell alone is excluded, its one pair counted under
    zero_denominator and the cell under the same cause, and grp-b keeps model-a's 0. No group is dropped."""
    pairing = pair_rows(by_model_rows(), ratio_spec(estimation_unit=BY_MODEL))
    assert (pairing.differences.clusters, pairing.differences.models) == (("grp-a", "grp-b"), ("model-a", "model-b"))
    assert list(pairing.differences.cluster_differences) == pytest.approx([(math.log(6.0) + math.log(3.0)) / 2, 0.0])
    assert (pairing.n_pairs, pairing.missing["zero_denominator"], pairing.n_clusters_dropped) == (4, 1, 0)
    assert pairing.n_cells == 3
    assert pairing.cells_lost == {"no_pair": 0, "zero_denominator": 1, "zero_numerator": 0}


def test_s13_a_group_whose_every_cell_is_lost_is_dropped_and_counted_and_a_cell_with_no_pair_is_counted():
    """S13: in grp-c neither model ever wins in B, so both its cells are excluded and the group is dropped, counted in
    n_clusters_dropped; model-a ran grp-d in arm A only, a cell with no pair, so grp-d is dropped too."""
    rows = by_model_rows()
    rows += ratio_arms("task-e", "model-a", 1, (10.0, 1.0), (10.0, 0.0), cluster="grp-c")
    rows += ratio_arms("task-e", "model-b", 1, (10.0, 1.0), (10.0, 0.0), cluster="grp-c")
    rows.append(ratio_row("task-f", "model-a", 1, 10.0, 1.0, condition="cond-a", cluster="grp-d"))
    pairing = pair_rows(rows, ratio_spec(estimation_unit=BY_MODEL))
    assert pairing.differences.clusters == ("grp-a", "grp-b")
    assert pairing.n_clusters_dropped == 2
    assert pairing.cells_lost == {"no_pair": 1, "zero_denominator": 3, "zero_numerator": 0}
    assert pairing.missing["zero_denominator"] == 3


def test_s13_the_estimation_unit_changes_the_estimand_the_pooled_sums_weigh_the_models_that_spend_most():
    """S13: on the same rows, pooling the models of a group sums A's 360 for 2 wins against B's 120 for 3 in grp-a,
    ln 4.5, and A's 140 for 3 against B's 70 for 1 in grp-b, ln(2 / 3): model-b's zero is absorbed, not excluded.
    Each group is its own one cell."""
    pairing = pair_rows(by_model_rows(), ratio_spec())
    assert pairing.differences.models == (POOLED,)
    assert pairing.differences.matrix[:, 0] == pytest.approx([math.log(4.5), math.log(2.0 / 3.0)])
    assert (pairing.n_pairs, pairing.missing["zero_denominator"], pairing.n_clusters_dropped) == (5, 0, 0)
    assert (pairing.n_cells, pairing.cells_lost) == (2, {"no_pair": 0, "zero_denominator": 0, "zero_numerator": 0})


def test_s13_the_cluster_attribute_alone_is_the_default_estimation_unit_and_gives_the_pooled_numbers_exactly():
    """S13: an estimation unit of the attribute alone is the ratio form's default, and pairs exactly as a contrast
    that declares none."""
    default = pair_rows(by_model_rows(), ratio_spec())
    declared = pair_rows(by_model_rows(), ratio_spec(estimation_unit=("group",)))
    assert ratio_spec().estimation_unit == ("group",)
    assert default.differences.clusters == declared.differences.clusters
    assert default.differences.models == declared.differences.models
    assert np.array_equal(default.differences.matrix, declared.differences.matrix)
    assert dataclasses.replace(default, differences=None) == dataclasses.replace(declared, differences=None)


def test_a_cell_estimation_unit_puts_a_task_run_by_two_models_in_one_group_and_two_cells():
    """The task's cluster is checked on the attribute: a task run by two models is in one group, in two cells."""
    rows = ratio_arms("task-a", "model-a", 1, (10.0, 1.0), (10.0, 1.0))
    rows += ratio_arms("task-a", "model-b", 1, (10.0, 1.0), (10.0, 1.0))
    differences = pair_rows(rows, ratio_spec(estimation_unit=BY_MODEL)).differences
    assert (differences.clusters, differences.models) == (("grp-a",), ("model-a", "model-b"))


@pytest.mark.parametrize(
    ("unit", "ratio", "message"),
    [
        (("group",), False, "a contrast of one outcome estimates per model's cell"),
        (("region",), True, "estimation_unit must be"),
        (("group", "model"), True, "estimation_unit must be"),
        (("model",), True, "estimation_unit must be"),
        (("model", "region"), True, "estimation_unit must be"),
        (("model", "group", "seed"), True, "estimation_unit must be"),
    ],
)
def test_an_estimation_unit_that_is_not_the_attribute_or_the_model_and_the_attribute_is_refused(unit, ratio, message):
    """A spec built in code holds the canonical unit: for a ratio contrast the attribute alone or the model then the
    attribute, for a contrast of one outcome the model then the attribute, the cell it already averages within."""
    build = (
        ratio_spec if ratio else functools.partial(ContrastSpec, "x", "score", WILCOXON, "group", ("cond-a", "cond-b"))
    )
    with pytest.raises(ValueError, match=message):
        build(estimation_unit=unit)


# --- what became of each arm's own planned runs -----------------------------------------------------------------------


def test_s14_each_arm_counts_its_own_planned_runs_by_what_became_of_them():
    """S14: one pair lost to A's None is one A run lost; B's exhausted run and A's unfinished one count in their arm."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows += both_arms("task-b", "model-a", 1, None, 0.5)
    rows += treated("task-e", "model-a", 1, None, 0.4)
    rows.append(make_row("task-c", "model-a", 1, 0.7, condition="cond-a"))
    rows.append(make_row("task-d", "model-a", 1, 0.3, condition="cond-b"))
    gone = [absent("task-c", "cond-b", "exhausted"), absent("task-d", "cond-a", "unfinished")]
    first, second = pair_rows(rows, named(), absent=gone).arm_runs
    assert (first.arm, first.planned, first.finished, first.measured) == ("cond-a", 5, 4, 2)
    assert first.lost == {
        "treatment_outcome_none": 1,
        "outcome_none": 1,
        "exhausted": 0,
        "unfinished": 1,
        "excluded": 0,
    }
    assert (second.arm, second.planned, second.finished, second.measured) == ("cond-b", 5, 4, 4)
    assert second.lost["exhausted"] == 1


def test_s14_a_run_on_a_task_the_other_arm_never_plans_is_outside_both_arms_counts():
    """S14: a chain design's arm runs tasks the other does not; those runs are not the contrast's, lost or not."""
    rows = both_arms("task-a", "model-a", 1, 0.9, 0.5)
    rows.append(make_row("task-z", "model-a", 1, None, condition="cond-a"))
    first, second = pair_rows(rows, named(), absent=[absent("task-y", "cond-b", "unfinished")]).arm_runs
    assert (first.planned, first.measured, second.planned, second.measured) == (1, 1, 1, 1)


def test_s14_a_factor_contrasts_run_whose_other_factors_no_run_of_the_other_level_shares_is_outside_both_arms():
    """S14: mode y's open run has no shut run to pair with, so it is no run of the contrast's pairs."""
    rows = [
        make_row("task-a", "model-a", 1, 0.9, condition="open-x", gate="open", mode="x"),
        make_row("task-a", "model-a", 1, 0.5, condition="shut-x", gate="shut", mode="x"),
        make_row("task-a", "model-a", 1, 0.8, condition="open-y", gate="open", mode="y"),
    ]
    first, second = pair_rows(rows, by_factor()).arm_runs
    assert (first.planned, second.planned) == (1, 1)


def test_s14_a_factor_contrasts_arms_are_its_levels():
    """S14: the arms are named by the levels of the varying factor, each counting every run of its level."""
    rows = [
        make_row("task-a", "model-a", 1, 0.9, condition="open-x", gate="open"),
        make_row("task-a", "model-a", 1, 0.5, condition="shut-x", gate="shut"),
    ]
    first, second = pair_rows(rows, by_factor()).arm_runs
    assert (first.arm, first.planned, second.arm, second.planned) == ("open", 1, "shut", 1)
