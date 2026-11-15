"""One-arm estimates: a rate aggregated to clusters as contrasts are, and a ratio of totals, hand-computed."""

import math

import numpy as np
import pytest

from expcore.stats import contrasts as kernel
from expcore.stats.estimators import EstimatorSettings
from expcore.stats.onearm import RATE, RATIO_OF_TOTALS, DescriptiveSpec, describe
from expcore.stats.rows import AbsentRun, Row

SETTINGS = EstimatorSettings(replicates=300, level=0.95, seed=(7, 11))


def row(task, model, seed, outcomes, *, condition="cond-a", cluster="grp-a", treatment=False):
    """An analysed run with the given outcomes."""
    factors = {"task": task, "model": model, "seed": seed}
    run_key = "|".join(f"{name}={value}" for name, value in [*factors.items(), ("condition", condition)])
    return Row(run_key, condition, factors, dict(outcomes), {"group": cluster}, treatment)


def absent(task, condition, reason):
    """A planned run with no analysed attempt."""
    factors = {"task": task, "model": "model-a", "seed": 1}
    return AbsentRun(f"task={task}|model=model-a|seed=1|condition={condition}", condition, factors, reason)


def rate(name="waited-in-a", conditions=("cond-a",)):
    """The share of runs that waited, in the given conditions."""
    return DescriptiveSpec(name, conditions, "group", outcome="waited")


def per_placement(name="queries-per-placement", conditions=("cond-a",)):
    """Queries over placements, in the given conditions."""
    return DescriptiveSpec(name, conditions, "group", numerator="queries", denominator="placements")


def rate_rows():
    """grp-a: model-a's task-a 1 and 0 (0.5), its task-b 1, so 0.75; model-b 0. grp-b: model-a 1."""
    return [
        row("task-a", "model-a", 1, {"waited": 1.0}),
        row("task-a", "model-a", 2, {"waited": 0.0}),
        row("task-b", "model-a", 1, {"waited": 1.0}),
        row("task-a", "model-b", 1, {"waited": 0.0}),
        row("task-c", "model-a", 1, {"waited": 1.0}, cluster="grp-b"),
    ]


def test_s14_a_rate_is_aggregated_to_clusters_as_contrasts_are():
    """S14: grp-a is the mean of its models, (0.75 + 0) / 2, grp-b is 1, so the share is 0.6875, not 3 / 5 runs."""
    result = describe(rate_rows(), rate(), SETTINGS)
    assert (result.kind, result.outcome, result.conditions) == (RATE, "waited", ("cond-a",))
    assert result.estimate == pytest.approx(0.6875)
    assert (result.n_clusters, result.n_runs) == (2, 5)
    assert [(m.model, m.estimate) for m in result.per_model] == [
        ("model-a", pytest.approx(0.875)),
        ("model-b", pytest.approx(0.0)),
    ]


def test_s14_a_rates_interval_is_the_kernels_cluster_bootstrap_at_its_seed():
    """S14, S04: the percentile interval of the kernel's resampled means, one draw for every model."""
    result = describe(rate_rows(), rate(), SETTINGS)
    boot = kernel.cluster_bootstrap(np.array([[0.75, 0.0], [1.0, math.nan]]), 300, np.random.default_rng([7, 11]))
    low, high, _ = kernel.percentile_interval(boot.overall, 0.95)
    assert (result.ci_low, result.ci_high) == pytest.approx((low, high))
    assert result.n_undefined_replicates == 0


def test_s14_a_declared_set_of_conditions_pools_their_runs_and_ignores_the_others():
    """S14: cond-b's run joins cond-a's in its (task, model); cond-c's does not enter."""
    rows = [
        row("task-a", "model-a", 1, {"waited": 1.0}),
        row("task-a", "model-a", 1, {"waited": 0.0}, condition="cond-b"),
        row("task-a", "model-a", 1, {"waited": 1.0}, condition="cond-c"),
    ]
    result = describe(rows, rate(conditions=("cond-a", "cond-b")), SETTINGS)
    assert result.estimate == pytest.approx(0.5)
    assert result.n_runs == 2


def test_s14_a_ratio_of_totals_sums_every_run_a_failed_ones_numerator_included():
    """S14: queries 10 + 5 + 6 over placements 2 + 0 + 1 is 7; the run with no placement still adds its queries."""
    rows = [
        row("task-a", "model-a", 1, {"queries": 10.0, "placements": 2.0}),
        row("task-a", "model-a", 2, {"queries": 5.0, "placements": 0.0}),
        row("task-b", "model-a", 1, {"queries": None, "placements": 1.0}),
        row("task-c", "model-a", 1, {"queries": 6.0, "placements": 1.0}, cluster="grp-b"),
    ]
    result = describe(rows, per_placement(), SETTINGS)
    assert (result.kind, result.outcome) == (RATIO_OF_TOTALS, "queries/placements")
    assert result.estimate == pytest.approx(7.0)
    assert (result.n_runs, result.runs.lost["outcome_none"]) == (3, 1)


def test_s14_a_ratio_of_totals_resamples_clusters_and_takes_the_drawn_totals_ratio():
    """S14: grp-a totals 15 / 2, grp-b 6 / 1, so a draw is 30 / 4, 21 / 3 or 12 / 2; the interval, their percentiles."""
    rows = [
        row("task-a", "model-a", 1, {"queries": 15.0, "placements": 2.0}),
        row("task-c", "model-a", 1, {"queries": 6.0, "placements": 1.0}, cluster="grp-b"),
    ]
    result = describe(rows, per_placement(), SETTINGS)
    indices = kernel.draw_clusters(np.random.default_rng([7, 11]), 300, 2)
    queries, placements = np.array([15.0, 6.0]), np.array([2.0, 1.0])
    draws = queries[indices].sum(axis=1) / placements[indices].sum(axis=1)
    low, high, _ = kernel.percentile_interval(draws, 0.95)
    assert (result.ci_low, result.ci_high) == pytest.approx((low, high))
    assert 6.0 <= result.ci_low <= result.ci_high <= 7.5


def test_s14_s05_a_replicate_whose_drawn_denominators_sum_to_zero_is_undefined_and_counted():
    """S14, S05: drawing grp-a twice gives 10 / 0, which is no number: the replicate is NaN, never 0 or infinite."""
    rows = [
        row("task-a", "model-a", 1, {"queries": 5.0, "placements": 0.0}),
        row("task-c", "model-a", 1, {"queries": 6.0, "placements": 2.0}, cluster="grp-b"),
    ]
    result = describe(rows, per_placement(), EstimatorSettings(1000, 0.95, (3,)))
    assert result.estimate == pytest.approx(5.5)
    assert result.n_undefined_replicates / 1000 == pytest.approx(0.25, abs=0.05)
    assert math.isfinite(result.ci_low)
    assert math.isfinite(result.ci_high)


def test_s14_every_denominator_zero_gives_no_estimate():
    """S14: nothing was placed, so queries per placement is undefined, not infinite."""
    rows = [row("task-a", "model-a", 1, {"queries": 5.0, "placements": 0.0})]
    result = describe(rows, per_placement(), SETTINGS)
    assert math.isnan(result.estimate)
    assert math.isnan(result.ci_low)
    assert result.n_undefined_replicates == 300


def test_s14_no_run_with_the_outcome_gives_no_estimate_and_counts_why():
    """S14: every run of the arm is lost, so the estimate is NaN and the arm's runs say why."""
    rows = [row("task-a", "model-a", 1, {"waited": None}, treatment=True)]
    result = describe(rows, rate(), SETTINGS, absent=[absent("task-b", "cond-a", "exhausted")])
    assert math.isnan(result.estimate)
    assert result.n_clusters == 0
    assert (result.runs.planned, result.runs.finished, result.runs.measured) == (2, 1, 0)
    assert result.runs.lost["treatment_outcome_none"] == 1
    assert result.runs.lost["exhausted"] == 1


def test_s14_the_arms_planned_runs_include_its_absent_runs_and_no_other_conditions():
    """S14: the finish rate is over the arm's own planned runs."""
    gone = [absent("task-d", "cond-a", "unfinished"), absent("task-d", "cond-c", "exhausted")]
    result = describe(rate_rows(), rate(), SETTINGS, absent=gone)
    assert (result.runs.planned, result.runs.finished) == (6, 5)
    assert result.runs.finish_rate == pytest.approx(5 / 6)


def test_s14_totals_far_apart_in_size_give_every_defined_replicate_its_exact_ratio():
    """S14: grp-b's totals are 1e-200 over 1e-200, a ratio of 1; no scaling may underflow its denominator to 0 and
    leave the draw of grp-b twice undefined."""
    rows = [
        row("task-a", "model-a", 1, {"queries": 1e-200, "placements": 1e200}),
        row("task-c", "model-a", 1, {"queries": 1e-200, "placements": 1e-200}, cluster="grp-b"),
    ]
    result = describe(rows, per_placement(), EstimatorSettings(1000, 0.95, (3,)))
    assert result.n_undefined_replicates == 0
    assert result.ci_high == pytest.approx(1.0)
    assert result.ci_low == pytest.approx(0.0, abs=1e-300)
    assert result.per_model[0].n_undefined_replicates == 0


def test_s14_a_rate_of_huge_values_is_refused_rather_than_overflowing_to_infinity():
    """S14: a mean of values past 1e300 can overflow a float sum; it is refused naming the run, never infinite."""
    rows = [row("task-a", "model-a", 1, {"waited": 1e308}), row("task-c", "model-a", 1, {"waited": 1e308}, cluster="b")]
    with pytest.raises(ValueError, match=r"outcome 'waited' of run .* is beyond"):
        describe(rows, rate(), SETTINGS)
    assert describe([row("task-a", "model-a", 1, {"waited": 1e300})], rate(), SETTINGS).estimate == 1e300


def test_s14_an_absent_run_that_repeats_or_is_analysed_is_refused():
    """S14: a run key absent twice, or absent and analysed, would count one run twice in the finish rate."""
    analysed = [row("task-a", "model-a", 1, {"waited": 1.0})]
    with pytest.raises(ValueError, match="two runs of the arm share one run key"):
        describe(analysed, rate(), SETTINGS, absent=[absent("task-a", "cond-a", "unfinished")])
    twice = [absent("task-b", "cond-a", "unfinished"), absent("task-b", "cond-a", "exhausted")]
    with pytest.raises(ValueError, match="two runs of the arm share one run key"):
        describe(analysed, rate(), SETTINGS, absent=twice)


def test_a_single_cluster_gives_a_point_interval():
    """One cluster has nothing to resample but itself."""
    rows = [row("task-a", "model-a", 1, {"waited": 1.0}), row("task-a", "model-a", 2, {"waited": 0.0})]
    result = describe(rows, rate(), SETTINGS)
    assert (result.estimate, result.ci_low, result.ci_high) == pytest.approx((0.5, 0.5, 0.5))


def test_the_result_is_a_function_of_the_set_of_rows_and_the_settings():
    """Row order does not matter, and equal settings give equal results."""
    rows = rate_rows()
    assert describe(rows, rate(), SETTINGS) == describe(list(reversed(rows)), rate(), SETTINGS)


@pytest.mark.parametrize(
    ("outcomes", "message"),
    [
        ({"queries": -1.0, "placements": 1.0}, "is negative"),
        ({"queries": 1.0}, "has no outcome 'placements'"),
    ],
)
def test_a_bad_value_is_refused_naming_the_run_and_the_outcome(outcomes, message):
    """A total of counts cannot hold a negative value, and an outcome a row lacks is not a None."""
    with pytest.raises(ValueError, match=message):
        describe([row("task-a", "model-a", 1, outcomes)], per_placement(), SETTINGS)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"conditions": (), "outcome": "waited"}, "one condition or more"),
        ({"conditions": ("cond-a", "cond-a"), "outcome": "waited"}, "each condition once"),
        ({"conditions": ("cond-a",)}, "an outcome, or two different"),
        ({"conditions": ("cond-a",), "outcome": "w", "numerator": "q", "denominator": "p"}, "not both"),
        ({"conditions": ("cond-a",), "numerator": "q", "denominator": "q"}, "two different"),
    ],
)
def test_a_spec_that_is_not_one_form_is_refused(fields, message):
    """A rate names one outcome; a ratio of totals names two different ones; never both."""
    with pytest.raises(ValueError, match=message):
        DescriptiveSpec("d", cluster="group", **fields)
