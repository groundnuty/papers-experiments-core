"""Pairing: named-condition and factor contrasts, aggregation per cluster, and what is dropped and counted."""

import math
import random

import numpy as np
import pytest
from stats_support import both_arms, by_factor, make_row, named

from expcore.stats.pairing import ContrastSpec, pair_rows
from expcore.stats.rows import Row


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


def test_r10_a_missing_outcome_on_the_b_side_and_nan_are_missing_too():
    """R10: missing on either side counts, and NaN is an undefined value like None."""
    rows = (
        both_arms("task-a", "model-a", 1, 0.9, None)
        + both_arms("task-a", "model-a", 2, math.nan, 0.2)
        + both_arms("task-a", "model-a", 3, 0.6, 0.2)
    )
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
    with pytest.raises(ValueError, match=r"share one run key \(task 'task-a', model 'model-a', seed 1\)"):
        pair_rows(rows, named())


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (Row("task-a", "model-a", 1, {}, {"group": "grp-a"}, {}, "cond-a"), "has no outcome 'score'"),
        (Row("task-a", "model-a", 1, {"score": 0.1}, {}, {}, "cond-a"), "has no cluster attribute 'group'"),
        (Row("task-a", "model-a", 1, {"score": 0.1}, {"group": "grp-a"}, {}, None), "has no condition"),
        (Row("task-a", "model-a", 1, {"score": True}, {"group": "grp-a"}, {}, "cond-a"), "must be a number or None"),
        (Row("task-a", "model-a", 1, {"score": "0.1"}, {"group": "grp-a"}, {}, "cond-a"), "must be a number or None"),
        (Row("task-a", "model-a", 1, {"score": math.inf}, {"group": "grp-a"}, {}, "cond-a"), "is infinite"),
    ],
)
def test_a_malformed_row_is_refused_and_named(row, message):
    """Validate at the boundary: the message names the contrast and the run, and never echoes an outcome."""
    rows = [row, make_row("task-a", "model-a", 1, 0.5, condition="cond-b")]
    with pytest.raises(ValueError, match=message) as caught:
        pair_rows(rows, named())
    assert "'a-vs-b'" in str(caught.value)
    assert "task 'task-a'" in str(caught.value)


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
