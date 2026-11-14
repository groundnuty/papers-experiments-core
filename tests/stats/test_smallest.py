"""S16: a preregistered smallest effect of interest per contrast, judged on the scale of the contrast's estimate (#81).

The known answers below are derived by hand in each test's docstring, never read back from the code under test.
"""

import math
import tomllib

import numpy as np
import pytest
from scipy import stats

from expcore.campaign.factors import FactorSchema
from expcore.stats import power
from expcore.stats.analysis import analyse, run_contrast
from expcore.stats.pairing import ContrastSpec, pair_rows
from expcore.stats.prereg import name_digest, parse
from expcore.stats.ratio import RatioForm
from expcore.stats.rows import Row
from expcore.stats.smallest import (
    DECISIONS,
    Rule,
    RuleError,
    decide,
    detectable,
    interval_relation,
    rule,
)

ARMS = FactorSchema(
    {"arm": ["a", "b", "c"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "abc"}
)
# Six clusters at alpha 0.05: the exact two-sided signed-rank test rejects only when all six differences share a sign
# (p = 2/64 = 0.03125; one rank against them gives 4/64 = 0.0625). A study of six N(delta, sd^2) draws then rejects
# with probability Phi(delta/sd)^6 + Phi(-delta/sd)^6, the second term below 1e-8 here, so power 0.8 needs
# Phi(delta/sd) = 0.8^(1/6) = 0.963492, delta = 1.7927 sd: the minimum detectable effect of six clusters.
MDE_PER_SD = 1.7927
MDE_TOLERANCE = 0.08  # in SDs: about five Monte Carlo standard errors of 4000 simulated studies
SD_OF_ONE_TO_SIX = math.sqrt(3.5)  # 1..6 deviate from 3.5 by 0.5, 1.5, 2.5 twice each: 17.5 / 5 = 3.5


def test_s16_the_hand_derived_constant_is_the_normal_quantile_it_names():
    """S16: 0.8^(1/6) = exp(ln 0.8 / 6) = exp(-0.0371905) = 0.963492, whose normal quantile is 1.79274."""
    assert stats.norm.ppf(0.8 ** (1 / 6)) == pytest.approx(MDE_PER_SD, abs=5e-5)


# --- the rule ------------------------------------------------------------------------------------------------------


def test_s16_a_difference_is_compared_on_the_outcomes_scale_and_a_ratio_as_its_natural_log():
    """S16: 2 and its reciprocal 0.5 are one size on the log scale, ln 2 = 0.693147; a difference is itself."""
    assert Rule("difference", 0.1, "higher").threshold == 0.1
    assert Rule("ratio", 2.0, "either").threshold == pytest.approx(0.693147, abs=1e-6)
    assert Rule("ratio", 0.5, "lower").threshold == Rule("ratio", 2.0, "lower").threshold


@pytest.mark.parametrize(
    ("declared", "ratio", "key", "message"),
    [
        ({"smallest_ratio": 2.0}, False, "smallest_ratio", "belongs to a ratio contrast"),
        ({"smallest_effect": 0.1}, True, "smallest_effect", "belongs to a contrast of one outcome"),
        ({"smallest_ratio": 1.0}, True, "smallest_ratio", "a factor of one"),
        ({"smallest_ratio": 1}, True, "smallest_ratio", "a factor of one"),
        ({"smallest_ratio": 0.0}, True, "smallest_ratio", "positive ratio"),
        ({"smallest_ratio": -2.0}, True, "smallest_ratio", "positive ratio"),
        ({"smallest_effect": 0.0}, False, "smallest_effect", "must be positive"),
        ({"smallest_effect": -0.1}, False, "smallest_effect", "must be positive"),
        ({"smallest_effect": "0.1"}, False, "smallest_effect", "finite number"),
        ({"smallest_effect": True}, False, "smallest_effect", "finite number"),
        ({"smallest_effect": math.nan}, False, "smallest_effect", "finite number"),
        ({"smallest_ratio": math.inf}, True, "smallest_ratio", "finite number"),
        ({"smallest_effect": 0.1, "direction": "up"}, False, "direction", "'higher' or 'lower'"),
        ({"direction": "higher"}, False, "direction", "without smallest_effect or smallest_ratio"),
        ({"smallest_ratio": 0.5, "direction": "higher"}, True, "smallest_ratio", "below 1"),
    ],
)
def test_s16_a_smallest_effect_of_the_wrong_form_or_value_is_refused_naming_the_key(declared, ratio, key, message):
    """S16: the wrong key for the contrast's form, a ratio that is not positive or is 1, a size that is not positive,
    a direction alone or unknown, and a decrease declared as "higher"."""
    with pytest.raises(RuleError, match=message) as refused:
        rule(**declared, ratio=ratio)
    assert refused.value.key == key


def test_s16_no_declaration_is_no_rule_and_a_rule_without_a_direction_holds_in_either():
    """S16: nothing declared is no rule; a declared number is held as a float, with "either" for no direction."""
    assert rule(ratio=False) is None
    assert rule(smallest_effect=0.1, ratio=False) == Rule("difference", 0.1, "either")
    assert rule(smallest_ratio=2, direction="higher", ratio=True) == Rule("ratio", 2.0, "higher")
    assert rule(smallest_ratio=0.5, direction="lower", ratio=True) == Rule("ratio", 0.5, "lower")


def test_s16_a_spec_built_past_the_loader_is_refused_the_same_way():
    """S16: a difference contrast cannot carry a ratio, nor a ratio contrast a difference."""
    with pytest.raises(ValueError, match="contrast 'a-vs-b': smallest_ratio belongs to a ratio contrast"):
        ContrastSpec("a-vs-b", "score", "wilcoxon_cluster_bootstrap", "group", conditions=("x", "y"), smallest_ratio=2)
    form = RatioForm("spend", "wins", "exclude")
    with pytest.raises(ValueError, match="smallest_effect belongs to a contrast of one outcome"):
        ContrastSpec("r", form.label, "ratio", "group", conditions=("x", "y"), ratio=form, smallest_effect=0.5)


# --- the decision, the interval and the minimum detectable effect --------------------------------------------------


@pytest.mark.parametrize(
    ("significant", "estimate", "direction", "decision"),
    [
        (True, 0.12, "higher", "meets"),
        (True, 0.1, "higher", "meets"),  # at the threshold
        (True, 0.6 - 0.5, "higher", "meets"),  # 0.09999999999999998 in floats: on the snapping grid it is 0.1
        (True, 0.08, "higher", "falls_short"),
        (True, 0.0, "higher", "falls_short"),
        (True, -0.12, "higher", "opposite_direction"),  # the defect the issue names: a drop never confirms a rise
        (True, -0.01, "higher", "opposite_direction"),  # whatever its size
        (True, -0.12, "lower", "meets"),
        (True, 0.12, "lower", "opposite_direction"),
        (True, -0.12, "either", "meets_either_direction"),  # never a bare "meets": the sign says which way
        (True, 0.12, "either", "meets_either_direction"),
        (True, 0.05, "either", "falls_short"),
        (False, 0.5, "higher", "not_significant"),
        (False, -0.5, "either", "not_significant"),
    ],
)
def test_s16_meets_needs_significance_and_the_estimate_at_or_beyond_the_threshold_on_the_claimed_side(
    significant, estimate, direction, decision
):
    """S16: a threshold of 0.1; the decision is one of DECISIONS."""
    assert decide(significant, estimate, 0.1, direction) == decision
    assert decision in DECISIONS


def test_s16_an_untestable_contrast_is_judged_untestable_whatever_its_numbers():
    """S16, S11: no pair is nothing to judge, whatever significance or estimate it carries."""
    assert decide(True, 0.5, 0.1, "higher", untestable=True) == "untestable"


@pytest.mark.parametrize(
    ("interval", "direction", "relation"),
    [
        ((0.12, 0.3), "higher", "beyond"),
        ((0.1, 0.3), "higher", "beyond"),  # every value at or beyond it
        ((0.05, 0.3), "higher", "includes"),
        ((0.05, 0.1), "higher", "includes"),
        ((-0.2, 0.08), "higher", "short_of"),
        ((-0.3, -0.12), "higher", "short_of"),  # on the other side of zero, so short of a rise
        ((-0.3, -0.12), "lower", "beyond"),
        ((-0.3, -0.05), "lower", "includes"),
        ((-0.05, 0.3), "lower", "short_of"),
        ((0.12, 0.3), "either", "beyond"),
        ((-0.3, -0.12), "either", "beyond"),
        ((-0.05, 0.08), "either", "short_of"),
        ((-0.3, 0.3), "either", "includes"),
        ((0.05, 0.3), "either", "includes"),
        ((math.nan, math.nan), "either", None),
    ],
)
def test_s16_the_interval_excludes_the_smallest_effect_beyond_or_short_of_it_or_includes_it(
    interval, direction, relation
):
    """S16: a threshold of 0.1; an undefined interval has no relation."""
    assert interval_relation(*interval, 0.1, direction) == relation


@pytest.mark.parametrize(
    ("mde", "threshold", "verdict"),
    [(0.3354, 0.5, True), (0.3354, 0.25, False), (0.5, 0.5, True), (None, 0.5, None), (math.nan, 0.5, None)],
)
def test_s16_the_design_detects_the_smallest_effect_when_its_minimum_detectable_effect_is_at_most_it(
    mde, threshold, verdict
):
    """S16: unknown, never no, without power settings or where the minimum detectable effect cannot be found."""
    assert detectable(mde, threshold) is verdict


# --- known answers through the analysis -----------------------------------------------------------------------------

SIX_TEXT = """\
alpha = 0.05
holm_family = FAMILY

[bootstrap]
replicates = 200
seed = 7

[power]
seed = 3
n_simulations = 4000
target_clusters = 6
target_difference = 0.1
sd_grid = [0.1]

[contrasts.lift]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
LIFT

[contrasts.cost]
conditions = ["cond-a", "cond-b"]
estimator = "ratio"
numerator = "spend"
denominator = "wins"
zero_denominator = "exclude"
cluster = "group"
COST

[contrasts.empty]
conditions = ["cond-a", "cond-c"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
smallest_effect = 0.25
"""


def six_prereg(lift="", cost="", family='["lift", "cost"]'):
    """The preregistration above with the declarations of `lift` and `cost`; `empty` has no pair to use."""
    text = SIX_TEXT.replace("FAMILY", family).replace("LIFT", lift).replace("COST", cost)
    return parse(tomllib.loads(text), "inline", "c" * 64, ARMS)


def six_clusters(step=0.1, factor=1.5):
    """Six clusters, one run of each arm in each: cluster i (1..6) has a score difference A - B of i x `step` and a
    log ratio of A's spend per win over B's of i x ln(`factor`)."""
    rows = []
    for i in range(1, 7):
        for arm, score, spend in (("cond-a", 0.5 + i * step, 30.0 * factor**i), ("cond-b", 0.5, 30.0)):
            run_key = f"task=t{i}|model=model-a|seed=1|condition={arm}"
            factors = {"task": f"t{i}", "model": "model-a", "seed": 1}
            rows.append(Row(run_key, arm, factors, {"score": score, "spend": spend, "wins": 2.0}, {"group": f"c{i}"}))
    return rows


@pytest.mark.parametrize(
    ("smallest", "can", "decision"),
    [(0.5, True, "falls_short"), (0.25, False, "meets")],
)
def test_s16_known_answer_a_difference_contrasts_minimum_detectable_effect_above_and_below_its_smallest_effect(
    smallest, can, decision
):
    """S16: differences 0.1 to 0.6 have mean 0.35 and SD 0.1 x sqrt(3.5) = 0.18708, so six clusters detect
    1.7927 x 0.18708 = 0.3354. That is below a smallest effect of 0.5 (detectable) and above 0.25 (not). All six are
    positive, p = 0.03125, significant in a family of one at alpha 0.05, so the estimate 0.35 falls short of 0.5 and
    meets 0.25."""
    prereg = six_prereg(f'smallest_effect = {smallest}\ndirection = "higher"', family='["lift"]')
    result = analyse(six_clusters(), prereg).contrast("lift")
    sd = 0.1 * SD_OF_ONE_TO_SIX
    assert result.estimate == pytest.approx(0.35)
    assert result.p_value == pytest.approx(2 / 64)
    assert result.minimum_detectable_effect == pytest.approx(MDE_PER_SD * sd, abs=MDE_TOLERANCE * sd)
    rule_ = result.smallest_effect
    assert (rule_.form, rule_.declared, rule_.threshold, rule_.direction) == (
        "difference",
        smallest,
        smallest,
        "higher",
    )
    assert rule_.detectable is can
    assert rule_.decision == decision
    assert rule_.minimum_detectable_ratio is None  # a difference has no ratio


def test_s16_the_minimum_detectable_effect_compared_is_the_power_modules_at_the_contrasts_own_seed():
    """S16: the figure the verdict rests on is the contrast's own, simulated from its SD at its cluster count."""
    prereg = six_prereg("smallest_effect = 0.5", family='["lift"]')
    result = analyse(six_clusters(), prereg).contrast("lift")
    differences = pair_rows(six_clusters(), prereg.contrasts["lift"]).differences.cluster_differences
    expected = power.minimum_detectable_effect(
        power.pilot_cluster_sd(differences), 6, 0.05, prereg.power, np.random.default_rng([3, name_digest("lift")])
    )
    assert result.minimum_detectable_effect == expected
    assert result.smallest_effect.detectable is (expected <= 0.5)


@pytest.mark.parametrize(
    ("factor", "can", "decision"),
    [(1.5, False, "meets"), (1.125, True, "falls_short")],
)
def test_s16_known_answer_a_ratio_contrast_with_smallest_ratio_two_is_judged_on_the_log_scale(factor, can, decision):
    """S16: smallest_ratio = 2 is ln 2 = 0.6931 on the scale of the ratio contrast's log ratios i x ln(factor).

    factor 1.5: SD ln 1.5 x sqrt(3.5) = 0.405465 x 1.870829 = 0.75856, MDE 1.7927 x 0.75856 = 1.3599 > 0.6931, so
    not detectable (as a ratio exp(1.3599) = 3.896 > 2); the estimate 3.5 x ln 1.5 = 1.4191 >= 0.6931 meets.
    factor 1.125: SD 0.117783 x 1.870829 = 0.22035, MDE 0.39503 <= 0.6931, detectable (ratio 1.4844 <= 2); the
    estimate 3.5 x 0.117783 = 0.41224 falls short of ln 2 (ratio 1.510 < 2).
    On the wrong scale each verdict would flip: the log MDE 1.36 is below 2, the ratio MDE 1.48 above ln 2, and the
    log estimate 1.42 below 2.
    """
    prereg = six_prereg(cost='smallest_ratio = 2\ndirection = "higher"', family='["cost"]')
    result = analyse(six_clusters(factor=factor), prereg).contrast("cost")
    sd = math.log(factor) * SD_OF_ONE_TO_SIX
    assert result.estimate == pytest.approx(3.5 * math.log(factor))
    assert result.minimum_detectable_effect == pytest.approx(MDE_PER_SD * sd, abs=MDE_TOLERANCE * sd)
    rule_ = result.smallest_effect
    assert (rule_.form, rule_.declared, rule_.direction) == ("ratio", 2.0, "higher")
    assert rule_.threshold == pytest.approx(0.693147, abs=1e-6)
    assert rule_.minimum_detectable_ratio == pytest.approx(math.exp(result.minimum_detectable_effect))
    assert rule_.detectable is can
    assert rule_.decision == decision


def test_s16_a_ratio_below_one_with_direction_lower_is_the_same_size_on_the_other_side():
    """S16: smallest_ratio = 0.5, direction "lower": A's cost per win at most half of B's. Log ratios -i x ln 1.5
    (factor 1/1.5) have the estimate -1.4191 <= -ln 2, so it meets; the MDE, a size, is as for 1.5, not detectable."""
    prereg = six_prereg(cost='smallest_ratio = 0.5\ndirection = "lower"', family='["cost"]')
    result = analyse(six_clusters(factor=1 / 1.5), prereg).contrast("cost")
    assert result.estimate == pytest.approx(-3.5 * math.log(1.5))
    assert result.smallest_effect.threshold == pytest.approx(math.log(2))
    assert (result.smallest_effect.decision, result.smallest_effect.detectable) == ("meets", False)


@pytest.mark.parametrize(
    ("direction", "decision"),
    [('direction = "higher"', "opposite_direction"), ("", "meets_either_direction"), ('direction = "lower"', "meets")],
)
def test_s16_a_significant_effect_of_the_opposite_sign_never_meets_a_directional_claim(direction, decision):
    """S16 (the issue's defect): differences -0.1 to -0.6, all negative, p = 0.03125, estimate -0.35. Against a claim
    that A raises the score by 0.25 it is opposite_direction; with no direction it meets either way, and says so."""
    prereg = six_prereg(f"smallest_effect = 0.25\n{direction}", family='["lift"]')
    result = analyse(six_clusters(step=-0.1), prereg).contrast("lift")
    assert (result.estimate, result.significant) == (pytest.approx(-0.35), True)
    assert result.smallest_effect.decision == decision


def test_s16_a_family_member_is_judged_by_holms_decision_not_its_raw_p_value():
    """S16: with the untestable `empty` in the family (p = 1, kept), Holm's first level is 0.05 / 2 = 0.025, and
    `lift`'s p = 0.03125 is above it: not significant, so it cannot meet, though alone its raw p would."""
    prereg = six_prereg("smallest_effect = 0.25", family='["lift", "empty"]')
    rows = six_clusters()
    alone = run_contrast(rows, prereg.contrasts["lift"], prereg)
    assert (alone.significant, alone.smallest_effect.decision) == (True, "meets_either_direction")
    judged = analyse(rows, prereg)
    assert judged.contrast("lift").significant is False
    assert judged.contrast("lift").smallest_effect.decision == "not_significant"
    empty = judged.contrast("empty").smallest_effect
    assert (empty.decision, empty.interval, empty.detectable) == ("untestable", None, None)


def test_s16_an_exploratory_contrast_is_judged_by_its_raw_p_value_and_stays_exploratory():
    """S16: outside the family `lift`'s raw p = 0.03125 at alpha 0.05 is its significance, as for its conclusion."""
    prereg = six_prereg("smallest_effect = 0.25", family='["cost"]')
    lift = analyse(six_clusters(), prereg).contrast("lift")
    assert (lift.exploratory, lift.significant, lift.smallest_effect.decision) == (True, True, "meets_either_direction")


def test_s16_without_power_settings_whether_the_design_detects_it_is_unknown():
    """S16: no minimum detectable effect was asked for, so the answer is unknown, never no; the decision stands."""
    prereg = six_prereg("smallest_effect = 0.25", family='["lift"]').with_override(power=None)
    rule_ = analyse(six_clusters(), prereg).contrast("lift").smallest_effect
    assert (rule_.detectable, rule_.minimum_detectable_ratio, rule_.decision) == (None, None, "meets_either_direction")


def test_s16_a_contrast_that_declares_none_has_no_smallest_effect_in_its_result():
    """S16: None, which analysis.json leaves out, for a contrast of either form that declares nothing."""
    result = analyse(six_clusters(), six_prereg(family='["lift"]'))
    assert result.contrast("lift").smallest_effect is None
    assert result.contrast("cost").smallest_effect is None
