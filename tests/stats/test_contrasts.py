"""The inference kernel: snapping, the Wilcoxon test, the cluster bootstrap and Holm (S01-S06)."""

import math

import numpy as np
import pytest
from scipy import stats

from expcore.stats import contrasts
from expcore.stats.contrasts import (
    _p_at_rank_sum,
    cluster_bootstrap,
    cluster_means,
    holm,
    level,
    model_means,
    one_sided_bootstrap_p,
    percentile_interval,
    resample,
    snap,
    supports,
    wilcoxon_input,
    wilcoxon_p,
    wilcoxon_p_studies,
    zeros_dropped,
)


def test_s01_sums_in_a_different_order_snap_to_the_same_value():
    """S01: differences equal in arithmetic tie regardless of summation order."""
    forward = (0.1 + 0.2) + 0.3
    backward = 0.1 + (0.2 + 0.3)
    assert forward != backward  # the premise: float noise separates them
    assert snap([forward])[0] == snap([backward])[0]
    assert (wilcoxon_input([forward, -backward]) == [0.6, -0.6]).all()


def test_s01_noise_does_not_move_the_p_value():
    """S01: values equal in arithmetic rank as ties, so the p-value is the one of the clean data."""
    clean = np.array([0.3, 0.3, -0.1, 0.5, 0.7, 0.8, -0.2])
    noisy = np.array([0.1 + 0.2, 0.3, -0.1, 0.5, 0.7, 0.1 + 0.7, -0.2])
    unsnapped = stats.wilcoxon(noisy, zero_method="wilcox", method="auto").pvalue
    assert unsnapped != stats.wilcoxon(clean, zero_method="wilcox", method="auto").pvalue  # noise would matter
    assert wilcoxon_p(noisy) == wilcoxon_p(clean) == stats.wilcoxon(clean, method="auto").pvalue


def test_s01_noise_around_zero_is_a_zero_and_a_real_difference_is_not():
    """S01: a difference below the grid is an exact zero; one above it survives."""
    assert (wilcoxon_input([1e-17, -1e-17, 5e-13, 2e-12]) == [0.0, 0.0, 0.0, 2e-12]).all()
    assert np.signbit(snap([-0.0])[0]) is np.False_


def test_s03_all_zero_differences_give_p_one():
    """S03: no evidence of a difference gives p = 1, where scipy would raise."""
    assert wilcoxon_p([0.0, 0.0, 1e-18, -1e-18]) == 1.0


def test_s03_nothing_to_test_gives_nan_not_a_p_value():
    """S03: no defined cluster gives NaN."""
    assert math.isnan(wilcoxon_p([]))
    assert math.isnan(wilcoxon_p([math.nan, math.nan]))


def test_s03_dropped_zero_differences_are_counted():
    """S03: the count is taken on the array the test sees, noise included."""
    assert zeros_dropped([0.0, 1e-17, -1e-17, 0.2, math.nan, -0.3]) == 3
    assert zeros_dropped([0.1, 0.2]) == 0
    assert zeros_dropped([]) == 0


def test_s03_zeros_are_dropped_before_ranking():
    """S03: with one zero and six positive differences the p-value is that of six, 2 / 2**6."""
    assert wilcoxon_p([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6], method="exact") == pytest.approx(2 / 64)


def test_wilcoxon_refuses_an_unknown_method():
    """S03: a method name that scipy would reinterpret is refused by name."""
    with pytest.raises(ValueError, match="unknown Wilcoxon method 'pratt'"):
        wilcoxon_p([0.1, 0.2], method="pratt")


@pytest.mark.parametrize(
    ("n", "method"), [(5, "exact"), (5, "approx"), (12, "auto"), (30, "exact"), (30, "approx"), (60, "auto")]
)
def test_s06_s09_the_p_values_of_many_studies_are_the_wilcoxon_p_of_each_one(n, method):
    """S06, S09: the simulation's batch p-values are `wilcoxon_p` itself, whichever method the analysis is set to."""
    draws = np.random.default_rng(n).normal(0.4, 1.0, size=(80, n))
    expected = [wilcoxon_p(row, method=method) for row in draws]
    assert list(wilcoxon_p_studies(draws, method=method)) == expected


def test_s06_s09_studies_with_a_zero_a_tie_or_an_undefined_cluster_go_through_the_wilcoxon_p_itself():
    """S06, S09: the rank-sum shortcut is only for studies without them; the others get the full function."""
    studies = np.array(
        [
            [0.3, 0.1, 0.5, 0.2, 0.4],
            [0.1, 0.1, 0.3, -0.2, 0.5],  # a tie
            [0.0, 0.2, 0.3, 0.4, 0.5],  # a zero
            [math.nan, 0.1, 0.2, 0.3, 0.4],  # an undefined cluster
            [0.0, 1e-17, -1e-17, 0.0, 0.0],  # all zero after snapping
            [0.1 + 0.2, 0.3, 0.5, 0.7, 0.9],  # a tie only after snapping (S01)
        ]
    )
    for method in ("exact", "approx", "auto"):
        got = wilcoxon_p_studies(studies, method=method)
        expected = np.array([wilcoxon_p(row, method=method) for row in studies])
        assert np.array_equal(got, expected, equal_nan=True), method


def test_s06_s09_many_studies_cost_one_wilcoxon_p_per_distinct_sum_of_positive_ranks(monkeypatch):
    """S06, S09: 300 studies of five clusters have at most 16 rank sums, so the real function runs at most 16 times."""
    calls = []

    def counting(differences, decimals=12, method="auto"):
        calls.append(1)
        return wilcoxon_p(differences, decimals, method)

    _p_at_rank_sum.cache_clear()
    monkeypatch.setattr(contrasts, "wilcoxon_p", counting)
    draws = np.random.default_rng(1).normal(0.3, 1.0, size=(300, 5))
    got = wilcoxon_p_studies(draws, method="exact")
    monkeypatch.undo()
    _p_at_rank_sum.cache_clear()
    assert 1 <= len(calls) <= 16
    assert list(got) == [wilcoxon_p(row, method="exact") for row in draws]


def test_wilcoxon_p_studies_refuses_a_wrong_shape():
    """A (studies x clusters) array is required."""
    with pytest.raises(ValueError, match="studies x clusters"):
        wilcoxon_p_studies([0.1, 0.2, 0.3])


def test_s04_one_resample_of_the_clusters_is_shared_by_all_models():
    """S04: the resampled cluster ids are identical across models within a replicate."""
    rows = np.arange(6, dtype=float)
    matrix = np.column_stack([rows, 10 * rows, 100 * rows])  # cell (cluster i, model m) names its cluster by i
    boot = cluster_bootstrap(matrix, 200, np.random.default_rng(5))
    assert boot.indices.shape == (200, 6)
    drawn = rows[boot.indices].mean(axis=1)  # mean of the cluster ids a replicate drew
    assert boot.per_model[:, 0] == pytest.approx(drawn)
    assert boot.per_model[:, 1] == pytest.approx(10 * drawn)
    assert boot.per_model[:, 2] == pytest.approx(100 * drawn)
    assert boot.overall == pytest.approx(drawn * (1 + 10 + 100) / 3)


def test_s04_the_same_seed_draws_the_same_clusters():
    """S04: the draw is a function of the generator alone."""
    matrix = np.arange(12, dtype=float).reshape(6, 2)
    first = cluster_bootstrap(matrix, 50, np.random.default_rng([1, 2]))
    again = cluster_bootstrap(matrix, 50, np.random.default_rng([1, 2]))
    assert (first.indices == again.indices).all()
    assert first.point == again.point == pytest.approx(cluster_means(matrix).mean())


def test_s05_a_model_with_no_defined_cell_in_a_resample_is_nan_not_zero():
    """S05: a replicate with an empty arm yields NaN, not 0."""
    matrix = np.array([[1.0, 4.0], [2.0, math.nan], [3.0, math.nan]])  # model 1 has a cell in cluster 0 only
    overall, per_model = resample(matrix, np.array([[1, 2, 2], [0, 1, 2]]))
    assert math.isnan(per_model[0, 1])  # the draw skipped cluster 0: undefined
    assert per_model[0, 0] == pytest.approx(8 / 3)
    assert per_model[1, 1] == 4.0  # a draw that includes it is defined
    assert overall == pytest.approx([8 / 3, 2.5])


def test_s05_undefined_means_are_nan_for_every_empty_input():
    """S05: the mean of nothing is NaN at every stage."""
    empty = np.full((3, 2), math.nan)
    assert np.isnan(cluster_means(empty)).all()
    assert np.isnan(model_means(empty)).all()


def test_s05_an_interval_leaves_out_undefined_replicates_and_counts_them():
    """S05: undefined replicates are excluded from the percentiles, never read as 0."""
    draws = [1.0, 2.0, 3.0, 4.0, 5.0, math.nan, math.nan]
    low, high, defined = percentile_interval(draws, 0.5)
    assert (low, high, defined) == (2.0, 4.0, 5)


def test_s05_an_interval_is_nan_when_fewer_than_half_the_replicates_are_defined():
    """S05: below half the replicates the interval is not reported; exactly half is enough."""
    low, high, defined = percentile_interval([1.0, math.nan, math.nan], 0.95)
    assert math.isnan(low)
    assert math.isnan(high)
    assert defined == 1
    assert percentile_interval([1.0, 3.0, math.nan, math.nan], 0.5) == (1.5, 2.5, 2)
    assert math.isnan(percentile_interval([math.nan, math.nan], 0.95)[0])


def test_s05_an_undefined_resample_never_supports_the_hypothesis():
    """S05: NaN does not support theta > theta0, wherever the boundary is, and a tie does not either."""
    assert not supports([math.nan], theta0=-1.0)[0]
    assert supports([0.0], theta0=-1.0)[0]  # an undefined draw is not a zero
    assert not supports([1e-10, 0.0, -1.0, math.nan]).any()
    assert supports([0.5]).all()


def test_s05_undefined_draws_count_against_the_hypothesis_in_the_p_value():
    """S05: (1 + draws not supporting) / (B + 1), with NaN among the draws not supporting."""
    assert one_sided_bootstrap_p([1.0, 1.0, math.nan, math.nan]) == pytest.approx((1 + 4 - 2) / 5)
    assert one_sided_bootstrap_p([math.nan], theta0=-1.0) == 1.0
    assert one_sided_bootstrap_p([0.0], theta0=-1.0) == 0.5
    assert one_sided_bootstrap_p([2.0] * 9) == pytest.approx(1 / 10)
    assert math.isnan(one_sided_bootstrap_p([]))


def test_bootstrap_refuses_inputs_it_cannot_resample():
    """S03: an empty matrix, no replicate, or a cluster without any cell is a caller's error."""
    rng = np.random.default_rng(0)
    with pytest.raises(ValueError, match="at least one cluster"):
        cluster_bootstrap(np.empty((0, 2)), 10, rng)
    with pytest.raises(ValueError, match="at least one replicate"):
        cluster_bootstrap(np.ones((2, 2)), 0, rng)
    with pytest.raises(ValueError, match="no paired cell"):
        cluster_bootstrap(np.array([[1.0], [math.nan]]), 10, rng)


def test_s02_a_missing_p_value_counts_as_one_and_keeps_the_family_size():
    """S02: a family with a NaN keeps its size m."""
    with_nan = holm([0.03, math.nan], 0.05)
    assert with_nan.reject == (False, False)  # m = 2: 0.03 sits above 0.05 / 2
    assert holm([0.03], 0.05).reject == (True,)  # the same p-value in a family of one
    assert with_nan.levels[0] == level(0.05, 1, 2)
    assert with_nan.adjusted[0] == pytest.approx(0.06)
    assert math.isnan(with_nan.adjusted[1])  # its own adjusted value stays NaN


def test_s02_none_is_missing_like_nan_and_never_rejects_even_at_alpha_near_one():
    """S02: a test that could not run is not significant."""
    result = holm([0.001, None, 0.02], 0.05)
    assert result.reject == (True, False, True)
    assert result.adjusted[0] == pytest.approx(0.003)
    assert result.adjusted[2] == pytest.approx(0.04)
    assert holm([None], 0.999999).reject == (False,)


def test_s02_the_step_down_stops_at_the_first_failure():
    """S02: after one failure no larger p-value is rejected, even one that would pass its own level."""
    result = holm([0.01, 0.04, 0.03], 0.05)
    assert result.reject == (True, False, False)
    assert result.adjusted == pytest.approx((0.03, 0.06, 0.06))
    assert result.levels == pytest.approx((0.05 / 3, 0.05, 0.025))


def test_s02_ties_keep_the_declared_order_and_an_empty_family_is_empty():
    """S02: equal p-values are ranked in the order the family declares them."""
    assert holm([0.02, 0.02, 0.5], 0.05).levels == pytest.approx((0.05 / 3, 0.025, 0.05))
    assert holm([], 0.05).reject == ()


def test_s02_values_outside_the_unit_interval_and_a_bad_alpha_are_refused():
    """S02: bad inputs fail by name instead of producing a decision."""
    with pytest.raises(ValueError, match=r"p-values must lie in \[0, 1\]"):
        holm([1.2], 0.05)
    with pytest.raises(ValueError, match="alpha must be strictly between 0 and 1"):
        holm([0.5], 0.0)
    with pytest.raises(ValueError, match="alpha must be strictly between 0 and 1"):
        holm([0.5], 1.0)


def test_s06_a_p_value_equal_to_its_level_is_rejected():
    """S06: a p-value equal to its level is rejected exactly as declared."""
    assert holm([0.025, 0.9], 0.05).reject == (True, False)
    assert holm([np.nextafter(0.025, 1.0), 0.9], 0.05).reject == (False, False)
    assert holm([np.nextafter(0.025, 0.0), 0.9], 0.05).reject == (True, False)


def test_s06_the_level_is_exact_where_the_adjusted_product_is_one_rounding_off():
    """S06: eleven hypotheses, the smallest p-value exactly on 0.05 / 11; the product alpha would miss it."""
    boundary = level(0.05, 1, 11)
    assert 11 * boundary > 0.05  # the premise: adjusting by multiplication lands above alpha
    result = holm([boundary] + [1.0] * 10, 0.05)
    assert result.reject[0] is True
    assert result.levels[0] == boundary
    assert result.adjusted[0] > 0.05  # reported as computed; the decision does not read it


def test_s06_the_level_is_multiplied_first():
    """S06: alpha * weight / total in that order, the one expression for every level."""
    assert level(0.05, 3, 7) == 0.05 * 3 / 7
    assert level(0.05, 1, 3) == 0.05 / 3
