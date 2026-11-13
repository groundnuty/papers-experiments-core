"""The estimator registry and the first estimator, with hand-computed results."""

import dataclasses
import math

import numpy as np
import pytest
from scipy import stats

from expcore.stats import contrasts as kernel
from expcore.stats import estimators
from expcore.stats.estimators import (
    RATIO,
    WILCOXON,
    EstimatorResult,
    EstimatorSettings,
    PairedDifferences,
    ParamError,
    UnknownEstimator,
)
from expcore.stats.ratio import POOLED


def paired(matrix, models=None):
    """Wrap a matrix as paired differences with invented cluster and model names."""
    matrix = np.array(matrix, dtype=float)
    models = models or tuple(f"model-{'abcdefgh'[j]}" for j in range(matrix.shape[1]))
    return PairedDifferences(tuple(f"grp-{'abcdefghij'[i]}" for i in range(matrix.shape[0])), tuple(models), matrix)


def settings(**params):
    """Settings with a small bootstrap, a fixed seed and the given estimator parameters."""
    return EstimatorSettings(replicates=300, level=0.95, seed=(7, 11), params=params)


@pytest.fixture
def scratch_registry(monkeypatch):
    """Run a test against a copy of the registry, so an estimator registered in it vanishes afterwards."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))


def test_the_first_registered_estimator_is_the_wilcoxon_cluster_bootstrap():
    """The registry starts with the first estimator under its documented name."""
    assert WILCOXON == "wilcoxon_cluster_bootstrap"
    assert estimators.names()[0] == WILCOXON
    assert estimators.get(WILCOXON) is estimators.wilcoxon_cluster_bootstrap


def test_an_unknown_estimator_is_refused_with_the_registered_names():
    """A name nobody registered raises and lists what exists."""
    with pytest.raises(
        UnknownEstimator, match=r"unknown estimator 'bayes'; registered: wilcoxon_cluster_bootstrap, ratio$"
    ):
        estimators.get("bayes")
    with pytest.raises(UnknownEstimator, match="unknown estimator"):
        estimators.check_params("bayes", {})


def test_registering_twice_or_under_a_bad_name_is_refused(scratch_registry):
    """One name, one estimator, spelt in lower case."""
    with pytest.raises(ValueError, match="already registered"):
        estimators.register(WILCOXON)(lambda paired, settings: None)
    with pytest.raises(ValueError, match="lower-case letters"):
        estimators.register("Bad-Name")(lambda paired, settings: None)


def test_another_estimator_registers_without_touching_the_interface(scratch_registry):
    """The contrast interface assumes no estimator: a second one is added with the same call and result."""

    @estimators.register("mean_only")
    def mean_only(paired, settings):
        return EstimatorResult(
            float(np.mean(paired.cluster_differences)), math.nan, math.nan, math.nan, len(paired.clusters)
        )

    assert estimators.get("mean_only")(paired([[0.2], [0.4]]), settings()).estimate == pytest.approx(0.3)
    assert "mean_only" in estimators.names()
    with pytest.raises(ParamError, match="takes no parameters"):
        estimators.check_params("mean_only", {"depth": 2})


def test_wilcoxon_on_five_positive_differences_has_the_exact_p_one_sixteenth():
    """Hand-computed: ranks 1..5 all positive, so W = 0 and p = 2 * (1 / 2**5) = 0.0625; the estimate is the mean."""
    result = estimators.get(WILCOXON)(paired([[0.1], [0.2], [0.3], [0.4], [0.5]]), settings(method="exact"))
    assert result.estimate == pytest.approx(0.3)
    assert result.p_value == pytest.approx(0.0625)
    assert result.n_clusters == 5
    assert result.counts == {"zero_dropped": 0}
    assert result.ci_low < 0.3 < result.ci_high
    assert result.ci_low >= 0.1
    assert result.ci_high <= 0.5


def test_s03_zero_differences_are_dropped_and_counted():
    """S03: one zero and six positive differences test as six, p = 2 / 2**6, and the zero is reported."""
    result = estimators.get(WILCOXON)(
        paired([[0.0], [0.1], [0.2], [0.3], [0.4], [0.5], [0.6]]), settings(method="exact")
    )
    assert result.p_value == pytest.approx(2 / 64)
    assert result.counts == {"zero_dropped": 1}


def test_s03_all_zero_differences_give_p_one_and_a_point_interval():
    """S03: no evidence of a difference gives p = 1 with the zeros counted, and the interval is the point."""
    result = estimators.get(WILCOXON)(paired([[0.0], [1e-17], [-1e-17]]), settings())
    assert result.p_value == 1.0
    assert result.counts == {"zero_dropped": 3}
    assert (result.ci_low, result.ci_high) == pytest.approx((0.0, 0.0), abs=1e-16)  # the interval keeps the raw noise
    assert result.estimate == pytest.approx(0.0, abs=1e-16)


def test_equal_differences_give_a_point_interval_and_the_hand_computed_normal_p():
    """Hand-computed: four equal differences tie at rank 2.5, W+ = 10, mean 5, sd 2.5, z = 2, p = 0.0455 (approx)."""
    result = estimators.get(WILCOXON)(paired([[0.2]] * 4), settings(method="approx"))
    assert result.p_value == pytest.approx(0.04550026389635839)
    assert result.ci_low == pytest.approx(0.2)
    assert result.ci_high == pytest.approx(0.2)
    exact = estimators.get(WILCOXON)(paired([[0.2]] * 4), settings(method="exact"))
    assert exact.p_value == pytest.approx(2 / 16)  # every sign flip gives W = 0 or 10


def test_the_estimate_is_the_mean_cluster_difference_with_equal_weight_per_model():
    """Cluster differences are the mean over the models with a cell, so a cluster with one model counts once."""
    result = estimators.get(WILCOXON)(paired([[0.1, 0.3], [0.2, math.nan], [0.3, 0.5]]), settings())
    assert result.estimate == pytest.approx((0.2 + 0.2 + 0.4) / 3)
    assert [(m.model, m.estimate) for m in result.per_model] == [
        ("model-a", pytest.approx(0.2)),
        ("model-b", pytest.approx(0.4)),
    ]


def test_s04_each_model_interval_comes_from_the_same_resamples_as_the_overall_one():
    """S04: with identical columns every model's interval is the overall interval."""
    column = [[0.1], [0.2], [0.35], [0.4], [0.8], [0.15]]
    result = estimators.get(WILCOXON)(paired(np.hstack([column, column, column])), settings())
    for model in result.per_model:
        assert (model.ci_low, model.ci_high) == pytest.approx((result.ci_low, result.ci_high))
        assert model.n_undefined_replicates == 0


def test_s05_a_model_with_one_defined_cell_reports_its_undefined_replicates():
    """S05: a model seen in a single cluster is undefined whenever that cluster is not drawn."""
    matrix = [[0.1, 0.4], [0.2, math.nan], [0.3, math.nan], [0.2, math.nan], [0.1, math.nan], [0.3, math.nan]]
    result = estimators.get(WILCOXON)(paired(matrix), EstimatorSettings(1000, 0.95, (3,), {}))
    thin = result.per_model[1]
    assert 0 < thin.n_undefined_replicates < 1000  # (5/6)**6 of the draws miss the cluster, about a third
    assert thin.n_undefined_replicates / 1000 == pytest.approx((5 / 6) ** 6, abs=0.06)
    assert thin.estimate == pytest.approx(0.4)
    assert (thin.ci_low, thin.ci_high) == pytest.approx((0.4, 0.4))  # the defined replicates all equal the cell
    assert result.per_model[0].n_undefined_replicates == 0


def test_the_result_is_a_function_of_the_settings_alone():
    """Equal settings give equal results; another seed gives another interval."""
    data = paired([[0.1], [0.2], [0.35], [0.4], [0.8], [0.15]])
    run = estimators.get(WILCOXON)
    assert run(data, settings()) == run(data, settings())
    other = run(data, EstimatorSettings(300, 0.95, (8, 11), {}))
    assert (other.ci_low, other.ci_high) != (run(data, settings()).ci_low, run(data, settings()).ci_high)


def test_no_cluster_gives_nan_and_zero_counts():
    """S03: a contrast with nothing paired has undefined values, which Holm treats as p = 1."""
    result = estimators.get(WILCOXON)(PairedDifferences((), (), np.empty((0, 0))), settings())
    assert math.isnan(result.estimate)
    assert math.isnan(result.p_value)
    assert (result.n_clusters, result.per_model, result.counts) == (0, (), {"zero_dropped": 0})


@pytest.mark.parametrize(
    ("params", "key"),
    [
        ({"depth": 1}, "depth"),
        ({"snap_decimals": 0}, "snap_decimals"),
        ({"snap_decimals": 16}, "snap_decimals"),
        ({"snap_decimals": True}, "snap_decimals"),
        ({"snap_decimals": 3.0}, "snap_decimals"),
        ({"method": "pratt"}, "method"),
    ],
)
def test_wilcoxon_parameters_are_checked_by_name(params, key):
    """A parameter the estimator does not know, or a value out of range, is refused naming the key."""
    with pytest.raises(ParamError) as caught:
        estimators.check_params(WILCOXON, params)
    assert caught.value.key == key
    with pytest.raises(ParamError):
        estimators.get(WILCOXON)(paired([[0.1]]), settings(**params))


def test_wilcoxon_accepts_its_documented_parameters():
    """The snap grid and the method are the two parameters."""
    estimators.check_params(WILCOXON, {"snap_decimals": 9, "method": "exact"})
    estimators.check_params(WILCOXON, {})


def test_paired_differences_are_checked_copied_and_read_only():
    """The input type refuses a matrix whose shape or empty rows break the estimators' assumptions."""
    source = np.array([[0.1, 0.2], [0.3, math.nan]])
    data = PairedDifferences(("grp-a", "grp-b"), ("model-a", "model-b"), source)
    source[0, 0] = 99.0
    assert data.matrix[0, 0] == 0.1
    with pytest.raises(ValueError, match="read-only"):
        data.matrix[0, 0] = 5.0
    with pytest.raises(ValueError, match="does not match 2 clusters by 1 models"):
        PairedDifferences(("grp-a", "grp-b"), ("model-a",), source)
    with pytest.raises(ValueError, match="at least one paired cell"):
        PairedDifferences(("grp-a", "grp-b"), ("model-a", "model-b"), np.array([[0.1, math.nan], [math.nan, math.nan]]))
    with pytest.raises(ValueError, match="at least one paired cell"):
        PairedDifferences(("grp-a", "grp-b"), ("model-a", "model-b"), np.array([[0.1, math.nan], [0.2, math.nan]]))


@pytest.mark.parametrize(
    "params",
    [{}, {"method": "exact"}, {"method": "approx"}, {"snap_decimals": 3}, {"method": "approx", "snap_decimals": 3}],
)
def test_s06_s09_the_registered_p_values_are_the_estimators_own_under_its_parameters(params):
    """S06, S09: the p-value the power study simulates with is the one the estimator reports, parameters included."""
    studies = np.round(np.random.default_rng(4).normal(0.3, 0.2, size=(40, 6)), 3)
    studies[5, :] = 0.0  # all zero
    studies[6, 0] = 0.0  # a zero
    function = estimators.p_values_function(WILCOXON, params)
    expected = [estimators.get(WILCOXON)(paired(study.reshape(-1, 1)), settings(**params)).p_value for study in studies]
    assert np.array_equal(function(studies), expected, equal_nan=True)


def test_s06_the_registered_p_values_follow_the_method_parameter():
    """S06: five distinct positive differences give p = 0.0625 exactly and 0.0431 by the normal approximation."""
    study = np.array([[0.1, 0.2, 0.3, 0.4, 0.5]])
    assert estimators.p_values_function(WILCOXON, {"method": "exact"})(study) == pytest.approx([0.0625])
    assert estimators.p_values_function(WILCOXON, {"method": "approx"})(study) == pytest.approx([0.04311444678])


def test_an_estimator_without_a_p_values_function_has_none_and_a_bad_parameter_is_refused(scratch_registry):
    """The hook is optional; asking for it checks the parameter table as the analysis does."""
    estimators.register("mean_only_hookless")(lambda paired, settings: None)
    assert estimators.p_values_function("mean_only_hookless", {}) is None
    with pytest.raises(ParamError, match="method"):
        estimators.p_values_function(WILCOXON, {"method": "pratt"})
    with pytest.raises(UnknownEstimator, match="unknown estimator 'bayes'"):
        estimators.p_values_function("bayes", {})


# --- the ratio estimator: the kernel on cluster log ratios --------------------------------------------------------

LOG_RATIOS = [math.log(r) for r in (6.0, 2.0, 3.0, 1.5, 4.0, 5.0)]  # six clusters, every ratio of ratios above 1


def pooled(values):
    """A ratio contrast's input: one log ratio per cluster, in the one column of the pooled models, marked pooled."""
    column = paired(np.array(values, dtype=float).reshape(-1, 1), models=(POOLED,))
    return dataclasses.replace(column, pooled=True)


def test_s13_the_ratio_estimator_is_registered_for_the_ratio_form_and_the_first_for_the_difference_form():
    """S13: each estimator declares the input it reads, so neither can be handed the other's cells."""
    assert RATIO == "ratio"
    assert estimators.names()[:2] == (WILCOXON, RATIO)
    assert estimators.form(RATIO) == "ratio"
    assert estimators.form(WILCOXON) == "difference"
    with pytest.raises(UnknownEstimator, match="unknown estimator 'bayes'"):
        estimators.form("bayes")


def test_an_estimator_registers_for_the_difference_form_unless_it_says_otherwise(scratch_registry):
    """A study's estimator keeps the form it always had; a form outside the two is refused."""
    estimators.register("plain_mean")(lambda paired, settings: None)
    assert estimators.form("plain_mean") == "difference"
    with pytest.raises(ValueError, match="form must be one of 'difference', 'ratio'"):
        estimators.register("odd_form", form="share")


def test_s13_the_ratio_estimate_is_the_mean_cluster_log_ratio_with_the_exact_signed_rank_p():
    """S13, hand-computed: six positive log ratios have W- = 0, so the two-sided exact p is 2 / 2**6; scipy agrees."""
    result = estimators.get(RATIO)(pooled(LOG_RATIOS), settings(method="exact"))
    assert result.estimate == pytest.approx(sum(LOG_RATIOS) / 6)
    assert result.p_value == pytest.approx(2 / 64)
    assert result.p_value == pytest.approx(stats.wilcoxon(LOG_RATIOS, method="exact").pvalue)
    assert result.counts == {"zero_dropped": 0}
    assert result.per_model == ()  # the models are pooled in each cluster: there is no estimate by model


def test_s13_the_signed_rank_p_of_mixed_signs_matches_scipy():
    """S13: with signs mixed and n = 7, the p-value is scipy's own on the same log ratios."""
    values = [math.log(r) for r in (6.0, 0.5, 3.0, 1.5, 0.8, 5.0, 2.5)]
    result = estimators.get(RATIO)(pooled(values), settings())
    assert result.p_value == pytest.approx(stats.wilcoxon(values).pvalue)


def test_s13_the_ratio_interval_is_the_kernels_shared_cluster_bootstrap_of_the_log_ratios():
    """S13: the same draws as the kernel's, so the interval is the percentile interval of the resampled means."""
    result = estimators.get(RATIO)(pooled(LOG_RATIOS), settings())
    boot = kernel.cluster_bootstrap(np.array(LOG_RATIOS).reshape(-1, 1), 300, np.random.default_rng([7, 11]))
    low, high, _ = kernel.percentile_interval(boot.overall, 0.95)
    assert (result.ci_low, result.ci_high) == pytest.approx((low, high))
    wilcoxon = estimators.get(WILCOXON)(pooled(LOG_RATIOS), settings())
    assert result == dataclasses.replace(wilcoxon, per_model=())  # the kernel, read on the log scale


def test_s13_equal_arms_give_log_ratios_of_zero_with_p_one_and_the_zeros_counted():
    """S13, S03: a cluster whose arms have one ratio is a zero, dropped and counted; all of them give p = 1."""
    result = estimators.get(RATIO)(pooled([0.0, 0.0, 0.0]), settings())
    assert result.p_value == 1.0
    assert result.counts == {"zero_dropped": 3}
    assert (result.estimate, result.ci_low, result.ci_high) == (0.0, 0.0, 0.0)


def test_s13_a_single_cluster_has_an_estimate_and_no_evidence():
    """S13: one cluster's log ratio is the estimate; one signed rank cannot reject, so p = 1."""
    result = estimators.get(RATIO)(pooled([math.log(2.0)]), settings(method="exact"))
    assert result.estimate == pytest.approx(math.log(2.0))
    assert result.p_value == 1.0
    assert result.n_clusters == 1


def test_s13_a_ratio_contrast_with_no_cluster_has_no_number():
    """S13, S03: every cluster excluded leaves no input, and nothing is estimated."""
    result = estimators.get(RATIO)(PairedDifferences((), (), np.empty((0, 0))), settings())
    assert all(math.isnan(value) for value in (result.estimate, result.p_value, result.ci_low, result.ci_high))


def test_s06_s09_the_ratio_estimators_registered_p_values_are_its_own():
    """S06, S09: the minimum detectable effect of a ratio contrast is simulated with the test it reports."""
    studies = np.round(np.random.default_rng(5).normal(0.4, 0.3, size=(30, 6)), 3)
    studies[3, :] = 0.0
    function = estimators.p_values_function(RATIO, {"method": "exact"})
    expected = [estimators.get(RATIO)(pooled(s), settings(method="exact")).p_value for s in studies]
    assert np.array_equal(function(studies), expected, equal_nan=True)


def test_the_ratio_estimators_parameters_are_checked_under_its_own_name():
    """It takes the kernel's two parameters, and a refusal names the ratio estimator, not another."""
    estimators.check_params(RATIO, {"snap_decimals": 9, "method": "exact"})
    with pytest.raises(ParamError, match="is not a parameter of ratio"):
        estimators.check_params(RATIO, {"depth": 1})


# --- the ratio estimator by (model, group) cell: estimated per cell, tested and resampled over groups -----------------

CELLS = [  # one row per group, one column per model: each (model, group) cell's log ratio, NaN for a lost cell
    [math.log(6.0), math.log(1.25)],
    [math.log(2.0), math.log(4.0)],
    [math.log(3.0), math.nan],
    [math.log(0.8), math.log(0.25)],
]  # no two of the groups' values, nor of model-a's cells, share a magnitude, so the exact test has no tie


def by_cell(cells=CELLS, models=("model-a", "model-b")):
    """A ratio contrast's input by cell: one row per group, each model's cell log ratio in its column."""
    return paired(cells, models=models)


def test_s13_by_cell_the_test_runs_on_each_groups_equal_weight_mean_of_its_cells():
    """S13, hand-computed: a group's value is the mean of its testable cells, each model weighing the same (grp-c has
    model-a's cell alone), the estimate the mean of the four groups' values, the p-value scipy's signed-rank test of
    those four values, and the clusters counted are the groups; each model's estimate is the mean of its own cells."""
    result = estimators.get(RATIO)(by_cell(), settings(method="exact"))
    groups = [(CELLS[0][0] + CELLS[0][1]) / 2, (CELLS[1][0] + CELLS[1][1]) / 2, CELLS[2][0], sum(CELLS[3]) / 2]
    assert result.estimate == pytest.approx(sum(groups) / 4)
    assert result.p_value == pytest.approx(stats.wilcoxon(groups, method="exact").pvalue)
    assert result.n_clusters == 4
    assert [m.model for m in result.per_model] == ["model-a", "model-b"]
    model_b = [row[1] for row in CELLS if not math.isnan(row[1])]
    assert [m.estimate for m in result.per_model] == pytest.approx([sum(r[0] for r in CELLS) / 4, sum(model_b) / 3])


def test_s13_by_cell_the_bootstrap_draws_groups_with_all_their_cells_and_each_models_interval_from_the_same_draws():
    """S13, S04: one resample of the groups, each with every cell it holds, serves the overall interval and every
    model's."""
    paired_cells = by_cell()
    result = estimators.get(RATIO)(paired_cells, settings())
    boot = kernel.cluster_bootstrap(paired_cells.matrix, 300, np.random.default_rng([7, 11]))
    assert boot.indices.shape == (300, 4)  # a draw is of the four groups, not of the seven cells
    assert (result.ci_low, result.ci_high) == pytest.approx(kernel.percentile_interval(boot.overall, 0.95)[:2])
    for column, model in enumerate(result.per_model):
        low, high, _ = kernel.percentile_interval(boot.per_model[:, column], 0.95)
        assert (model.ci_low, model.ci_high) == pytest.approx((low, high))


def test_s13_by_cell_perfectly_correlated_cells_of_a_group_give_the_inference_of_one_cell_per_group():
    """S13: two models whose cells are equal in every group carry no more evidence than one, so the estimate, the
    p-value, the interval and the count of clusters are those of the one model: the group, not the cell, is the unit.
    Counting the eight cells as independent would give n = 8 and a smaller p-value."""
    column = [row[0] for row in CELLS]
    twice = estimators.get(RATIO)(by_cell([[v, v] for v in column]), settings(method="exact"))
    once = estimators.get(RATIO)(by_cell([[v] for v in column], models=("model-a",)), settings(method="exact"))
    fields = ("estimate", "p_value", "ci_low", "ci_high", "n_clusters")
    assert [getattr(twice, f) for f in fields] == [getattr(once, f) for f in fields]
    flat = estimators.get(RATIO)(by_cell([[v] for v in column * 2], models=("model-a",)), settings())
    assert (twice.n_clusters, flat.n_clusters) == (4, 8)
    assert flat.p_value < twice.p_value


def test_s13_a_model_named_pooled_keeps_its_estimate_by_cell():
    """S13: whether the models are pooled is said by the input, not read from a column's name, so the one model left
    of an input by cell keeps its estimate whatever it is called."""
    column = [[row[0]] for row in CELLS]
    result = estimators.get(RATIO)(by_cell(column, models=(POOLED,)), settings())
    assert [(m.model, m.estimate) for m in result.per_model] == [(POOLED, pytest.approx(result.estimate))]
    assert estimators.get(RATIO)(pooled([row[0] for row in column]), settings()).per_model == ()


def test_a_pooled_input_has_the_one_pooled_column():
    """An input marked pooled holds one column of the pooled models, or no cluster at all."""
    with pytest.raises(ValueError, match="pooled input has the one column 'pooled'"):
        PairedDifferences(("grp-a",), ("model-a",), np.array([[0.1]]), pooled=True)
    assert PairedDifferences((), (), np.empty((0, 0)), pooled=True).pooled
