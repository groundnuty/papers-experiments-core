"""The ratio form of a contrast: log ratios of sums per cluster, and the declared rule for a zero sum, hand-computed."""

import math

import pytest

from expcore.stats.ratio import POOLED, RATIO_CAUSES, ZERO_RULES, RatioForm, aggregate, log_ratio

EXCLUDE = RatioForm("spend", "wins", "exclude")


def continuity(correction):
    """The form with a declared continuity correction."""
    return RatioForm("spend", "wins", "continuity", correction)


def test_s13_a_cluster_is_the_log_of_the_ratio_of_the_arms_ratios_of_sums():
    """S13: arm A spends 300 for 1 win, arm B 100 for 2, so the cluster's value is ln(300 / 1) - ln(100 / 2) = ln 6."""
    cause, value = log_ratio((300.0, 1.0), (100.0, 2.0), EXCLUDE)
    assert cause is None
    assert value == pytest.approx(math.log(6.0))


def test_s13_a_failed_run_counts_its_spend_and_adds_nothing_to_the_wins():
    """S13: the run with 0 wins stays in the sums; leaving its pair out would give ln 2 instead of ln 6."""
    with_failure = aggregate({"grp-a": [((100.0, 1.0), (50.0, 1.0)), ((200.0, 0.0), (50.0, 1.0))]}, EXCLUDE)
    without = aggregate({"grp-a": [((100.0, 1.0), (50.0, 1.0))]}, EXCLUDE)
    assert with_failure[0].matrix[0, 0] == pytest.approx(math.log(6.0))
    assert without[0].matrix[0, 0] == pytest.approx(math.log(2.0))
    assert with_failure[1] == {"zero_denominator": 0, "zero_numerator": 0}


def test_s13_the_input_has_one_column_for_the_models_pooled_in_each_cluster():
    """S13: the sums of a cluster run over every model's pairs, so the estimator's input has one column, POOLED."""
    differences, _ = aggregate({"grp-b": [((10.0, 1.0), (10.0, 1.0))], "grp-a": [((30.0, 1.0), (10.0, 1.0))]}, EXCLUDE)
    assert differences.clusters == ("grp-a", "grp-b")
    assert differences.models == (POOLED,)
    assert list(differences.cluster_differences) == pytest.approx([math.log(3.0), 0.0])


def test_equal_ratios_give_a_log_ratio_of_exactly_zero():
    """100 / 2 against 50 / 1 is one ratio, so the value is 0.0 exactly, never float noise the rank test would rank."""
    assert log_ratio((100.0, 2.0), (50.0, 1.0), EXCLUDE) == (None, 0.0)
    assert log_ratio((7e15, 3.0), (7e15, 3.0), continuity(0.5)) == (None, 0.0)


def test_extreme_totals_give_a_finite_log_ratio():
    """Totals whose ratios overflow a float still give the log ratio: the arithmetic is exact until the logarithm."""
    _, value = log_ratio((1e300, 1e-10), (1e-300, 1e10), EXCLUDE)
    assert math.isfinite(value)
    assert value == pytest.approx(math.log(1e300) - math.log(1e-10) - math.log(1e-300) + math.log(1e10))


@pytest.mark.parametrize(
    ("arm_a", "arm_b", "cause"),
    [
        ((300.0, 1.0), (100.0, 0.0), "zero_denominator"),  # no win in B: the ratio is undefined
        ((300.0, 0.0), (100.0, 2.0), "zero_denominator"),
        ((0.0, 0.0), (0.0, 0.0), "zero_denominator"),  # 0 / 0 in both arms is a zero denominator first
        ((0.0, 2.0), (100.0, 2.0), "zero_numerator"),  # no spend in A: the log ratio would be minus infinity
        ((100.0, 2.0), (0.0, 1.0), "zero_numerator"),
    ],
)
def test_s13_under_exclude_a_zero_total_excludes_the_cluster_with_its_cause_never_a_number(arm_a, arm_b, cause):
    """S13: an undefined or infinite log ratio is never 0, never infinite: the cluster has no value and a cause."""
    found, value = log_ratio(arm_a, arm_b, EXCLUDE)
    assert found == cause
    assert math.isnan(value)


def test_s13_a_continuity_correction_is_added_to_both_totals_of_both_arms():
    """S13: with 0.5 declared, B's 0 wins give ln((300.5 / 1.5) / (100.5 / 0.5)), a finite value and no cause."""
    cause, value = log_ratio((300.0, 1.0), (100.0, 0.0), continuity(0.5))
    assert cause is None
    assert value == pytest.approx(math.log((300.5 / 1.5) / (100.5 / 0.5)))


def test_a_continuity_correction_applies_to_every_cluster_not_only_to_one_with_a_zero():
    """The declared correction is one transformation of the data, not a choice made after seeing a zero."""
    _, value = log_ratio((300.0, 1.0), (100.0, 2.0), continuity(0.5))
    assert value == pytest.approx(math.log((300.5 / 1.5) / (100.5 / 2.5)))


def test_s13_a_continuity_correction_keeps_a_zero_numerator_finite():
    """S13: the correction covers a zero spend as it covers zero wins."""
    cause, value = log_ratio((0.0, 2.0), (100.0, 2.0), continuity(0.5))
    assert cause is None
    assert value == pytest.approx(math.log((0.5 / 2.5) / (100.5 / 2.5)))


def test_s13_an_excluded_cluster_counts_its_pairs_and_leaves_the_input():
    """S13: grp-b has no win in B, so its two pairs are counted under zero_denominator and grp-b is not a row."""
    cells = {
        "grp-a": [((300.0, 1.0), (100.0, 2.0))],
        "grp-b": [((10.0, 1.0), (10.0, 0.0)), ((20.0, 1.0), (10.0, 0.0))],
        "grp-c": [((0.0, 1.0), (10.0, 1.0))],
    }
    differences, excluded = aggregate(cells, EXCLUDE)
    assert differences.clusters == ("grp-a",)
    assert differences.matrix[0, 0] == pytest.approx(math.log(6.0))
    assert excluded == {"zero_denominator": 2, "zero_numerator": 1}


def test_no_cluster_left_gives_an_input_with_no_cluster():
    """Every cluster excluded is an empty input, which the analysis reports as untestable."""
    differences, excluded = aggregate({"grp-a": [((1.0, 0.0), (1.0, 1.0))]}, EXCLUDE)
    assert differences.clusters == ()
    assert excluded["zero_denominator"] == 1
    assert aggregate({}, EXCLUDE)[0].clusters == ()


def test_s13_totals_past_the_float_range_are_summed_exactly():
    """S13: two runs of 1e308 in each arm sum past the largest float, and the log ratio of equal arms is still 0."""
    pairs = [((1e308, 1.0), (1e308, 1.0)), ((1e308, 1.0), (1e308, 1.0))]
    differences, excluded = aggregate({"grp-a": pairs}, EXCLUDE)
    assert differences.matrix[0, 0] == 0.0
    assert excluded == {"zero_denominator": 0, "zero_numerator": 0}
    _, value = log_ratio((1e308, 1.0), (1e308, 4.0), EXCLUDE)
    assert value == pytest.approx(math.log(4.0))


def test_the_sums_do_not_depend_on_the_order_of_the_pairs():
    """Each total is an exact sum, so a cluster's value is a function of its set of pairs."""
    pairs = [((0.1, 1.0), (0.3, 1.0)), ((0.2, 0.0), (0.7, 2.0)), ((1e16, 1.0), (1.0, 1.0)), ((-0.0, 0.0), (1.0, 0.0))]
    first, _ = aggregate({"grp-a": pairs}, EXCLUDE)
    second, _ = aggregate({"grp-a": list(reversed(pairs))}, EXCLUDE)
    assert first.matrix[0, 0] == second.matrix[0, 0]


def test_the_form_names_its_causes_and_rules():
    """The causes a ratio contrast adds to the missing pairs, and the two rules a preregistration chooses from."""
    assert RATIO_CAUSES == ("numerator_none_at_zero", "zero_denominator", "zero_numerator")
    assert ZERO_RULES == ("exclude", "continuity")
    assert EXCLUDE.label == "spend/wins"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("spend", "spend", "exclude"), "two different outcomes"),
        (("", "wins", "exclude"), "numerator"),
        (("spend", " ", "exclude"), "denominator"),
        (("spend", "wins", "drop"), "zero_denominator must be one of 'exclude', 'continuity'"),
        (("spend", "wins", "continuity"), "continuity_correction"),
        (("spend", "wins", "continuity", 0.0), "positive finite number"),
        (("spend", "wins", "continuity", math.inf), "positive finite number"),
        (("spend", "wins", "continuity", True), "positive finite number"),
        (("spend", "wins", "exclude", 0.5), 'only with zero_denominator = "continuity"'),
    ],
)
def test_a_form_that_is_not_a_declared_rule_is_refused(args, message):
    """The rule is declared, never defaulted: a missing or a misspelt one, or a correction without its rule, fails."""
    with pytest.raises(ValueError, match=message):
        RatioForm(*args)
