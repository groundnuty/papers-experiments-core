"""S13, S16 and S17 together: a ratio contrast whose arms read columns of their own, judged against a smallest ratio,
inside a named Holm family.

Each key was pinned by its own PR's known answers; these tests declare them on one contrast and derive the answers by
hand from those cases (`tests/stats/test_analysis.py` for the per-arm columns, `tests/stats/test_smallest.py` for the
smallest effect, `tests/stats/test_holm_families.py` for the families), never reading them back from the code under
test.
"""

import json
import math
import tomllib

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.stats.analysis import analyse
from expcore.stats.prereg import parse
from expcore.stats.rows import Row
from expcore.study.pipeline import _finite, _json_text

ARMS = FactorSchema(
    {"arm": ["a", "b", "c"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "abc"}
)
# As in test_smallest: six clusters, all of one sign, give the exact two-sided p = 2/64 = 0.03125, and power 0.8 at
# alpha 0.05 needs a shift of 1.7927 SD (the minimum detectable effect of six clusters), within 0.035 SD of simulation
MDE_PER_SD, MDE_TOLERANCE = 1.7927, 0.035
SD_OF_ONE_TO_SIX = math.sqrt(3.5)  # the SD of 1..6

ADVERSE_TEXT = """\
alpha = 0.05
holm_family = ["adverse"]

[bootstrap]
replicates = 200
seed = 7

[power]
seed = 3
n_simulations = 20000
target_clusters = 6
target_difference = 0.1
sd_grid = [0.1]

[contrasts.adverse]
conditions = ["cond-a", "cond-b"]
estimator = "ratio"
numerator_a = "spend_high"
numerator_b = "spend_low"
denominator = "wins"
zero_denominator = "exclude"
cluster = "group"
SMALLEST
"""


def adverse_prereg(smallest='smallest_ratio = 2\ndirection = "higher"'):
    return parse(tomllib.loads(ADVERSE_TEXT.replace("SMALLEST", smallest)), "inline", "e" * 64, ARMS)


def adverse_clusters(factor):
    """Six clusters, one run of each arm in each, two wins each. In cluster i (1..6) arm A's upper bound is
    30 x factor**i and its lower bound a quarter of that; arm B's lower bound is 30 and its upper bound 240.

    The adverse contrast reads arm A's spend_high and arm B's spend_low, so cluster i's log ratio is
    ln((30 f^i / 2) / (30 / 2)) = i ln f, the six_clusters case of test_smallest. Had arm A read its own spend_low the
    log ratios would be i ln f - ln 4; had arm B read its own spend_high, i ln f - ln 8: every answer below moves."""
    rows = []
    for i in range(1, 7):
        upper_a = 30.0 * factor**i
        for arm, high, low in (("cond-a", upper_a, upper_a / 4), ("cond-b", 240.0, 30.0)):
            run_key = f"task=t{i}|model=model-a|seed=1|condition={arm}"
            factors = {"task": f"t{i}", "model": "model-a", "seed": 1}
            outcomes = {"spend_high": high, "spend_low": low, "wins": 2.0}
            rows.append(Row(run_key, arm, factors, outcomes, {"group": f"c{i}"}))
    return rows


@pytest.mark.parametrize(
    ("factor", "can", "decision"),
    [(1.5, False, "meets"), (1.125, True, "falls_short")],
)
def test_s16_a_per_arm_ratio_contrast_is_judged_against_its_smallest_ratio_on_the_log_scale(factor, can, decision):
    """S16 with S13's per-arm columns, by hand. Log ratios i ln f, i = 1..6: the estimate is 3.5 ln f, all six share
    a sign so p = 0.03125 < 0.05 (a family of one), SD ln f x sqrt 3.5, MDE 1.7927 SD.

    f = 1.5: estimate 1.4191 >= ln 2 = 0.6931, meets; MDE 1.3599 > ln 2, not detectable.
    f = 1.125: estimate 0.41224 < ln 2, falls short; MDE 0.39503 <= ln 2, detectable.
    The threshold is ln 2, the log of the declared ratio 2, and the minimum detectable ratio exp(MDE)."""
    result = analyse(adverse_clusters(factor), adverse_prereg()).contrast("adverse")
    log_f = math.log(factor)
    assert result.estimate == pytest.approx(3.5 * log_f)
    assert (result.p_value, result.significant) == (pytest.approx(0.03125), True)
    assert result.ratio.columns(0) == ("spend_high", "wins")
    assert result.ratio.columns(1) == ("spend_low", "wins")
    sd = log_f * SD_OF_ONE_TO_SIX
    assert result.minimum_detectable_effect == pytest.approx(MDE_PER_SD * sd, abs=MDE_TOLERANCE * sd)
    judged = result.smallest_effect
    assert (judged.form, judged.declared, judged.direction) == ("ratio", 2.0, "higher")
    assert judged.threshold == pytest.approx(math.log(2.0))
    assert judged.minimum_detectable_ratio == pytest.approx(math.exp(result.minimum_detectable_effect))
    assert (judged.detectable, judged.decision) == (can, decision)


def test_s16_a_per_arm_ratio_contrast_that_declares_no_smallest_ratio_has_none():
    """S16: the per-arm contrast without a smallest ratio keeps its S13 answer (3.5 ln 1.5) and has no judgement."""
    result = analyse(adverse_clusters(1.5), adverse_prereg(smallest="")).contrast("adverse")
    assert result.estimate == pytest.approx(3.5 * math.log(1.5))
    assert result.smallest_effect is None


# --- the three keys together: a per-arm ratio contrast with a smallest ratio, in a named Holm family (S17) -----------

FAMILIES_TEXT = """\
alpha = 0.05

[holm_families]
FAMILIES

[bootstrap]
replicates = 200
seed = 7

[power]
seed = 3
n_simulations = 20000
target_clusters = 6
target_difference = 0.1
sd_grid = [0.1]

[contrasts.adverse]
conditions = ["cond-a", "cond-b"]
estimator = "ratio"
numerator_a = "spend_high"
numerator_b = "spend_low"
denominator = "wins"
zero_denominator = "exclude"
cluster = "group"
smallest_ratio = 2
direction = "higher"

[contrasts.lift]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.empty]
conditions = ["cond-a", "cond-c"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def families_prereg(families):
    return parse(tomllib.loads(FAMILIES_TEXT.replace("FAMILIES", families)), "inline", "f" * 64, ARMS)


def scored_clusters():
    """adverse_clusters(1.5), each run also scored: arm A 0.5 + 0.1 i in cluster i, arm B 0.5, so `lift`'s six
    differences 0.1 i share a sign (p = 0.03125, estimate 0.35); cond-c has no run, so `empty` is untestable."""
    rows = adverse_clusters(1.5)
    for row in rows:
        i = int(row.factors["task"][1:])
        row.outcomes["score"] = 0.5 + 0.1 * i if row.condition == "cond-a" else 0.5
    return rows


@pytest.mark.parametrize(
    ("families", "adverse_holm", "lift_holm", "decision", "can"),
    [
        # adverse alone: m = 1, 0.03125 < 0.05 rejects (adjusted 0.03125), so the smallest ratio is met; lift with the
        # untestable empty (p = 1): 0.03125 > 0.05 / 2 misses, adjusted 2 x 0.03125 = 0.0625. adverse's MDE, at
        # alpha / 1, is 1.3599 > ln 2: not detectable
        ('cost = ["adverse"]\nquality = ["lift", "empty"]', (0.03125, True), (0.0625, False), "meets", False),
        # adverse with empty: it misses at 0.05 / 2 (adjusted 0.0625), so not significant, whatever its estimate; lift
        # alone rejects at 0.05 (adjusted 0.03125). adverse's MDE is at alpha / 2 = 0.025, which six clusters never
        # reach (their smallest p is 0.03125): NaN, so whether the design detects ln 2 is unknown, None
        ('cost = ["adverse", "empty"]\nquality = ["lift"]', (0.0625, False), (0.03125, True), "not_significant", None),
    ],
)
def test_s17_s16_s13_a_per_arm_ratio_contrast_with_a_smallest_ratio_is_judged_by_its_own_familys_holm_decision(
    families, adverse_holm, lift_holm, decision, can
):
    """S17 with S16 and S13, by hand. Each named family is adjusted on its own: the same p-values give adverse a
    different Holm decision in each case, and its smallest ratio is judged by that decision, on the log scale (the
    threshold ln 2, the estimate 3.5 ln 1.5 = 1.4191 from arm A's spend_high over arm B's spend_low), and its minimum
    detectable effect is at alpha over the size of its own family. Merged into one family of three, neither would
    reject (0.03125 > 0.05 / 3)."""
    result = analyse(scored_clusters(), families_prereg(families))
    adverse, lift = result.contrast("adverse"), result.contrast("lift")
    assert adverse.estimate == pytest.approx(3.5 * math.log(1.5))
    assert lift.estimate == pytest.approx(0.35)
    assert (adverse.p_value, lift.p_value) == (pytest.approx(0.03125), pytest.approx(0.03125))
    assert result.contrast("empty").n_pairs == 0
    holm = {d.name: (d.p_holm, d.reject) for d in result.holm}
    assert holm["adverse"] == (pytest.approx(adverse_holm[0]), adverse_holm[1])
    assert holm["lift"] == (pytest.approx(lift_holm[0]), lift_holm[1])
    assert (adverse.significant, lift.significant) == (adverse_holm[1], lift_holm[1])
    assert [family.members for family in result.holm_families] == [
        tuple(name.strip(' "') for name in line.split("=")[1].strip(" []").split(",")) for line in families.splitlines()
    ]
    judged = adverse.smallest_effect
    assert (judged.form, judged.declared, judged.direction) == ("ratio", 2.0, "higher")
    assert judged.threshold == pytest.approx(math.log(2.0))
    assert (judged.decision, judged.detectable) == (decision, can)
    assert math.isfinite(adverse.minimum_detectable_effect) is (can is not None)
    assert lift.smallest_effect is None


def test_s17_s16_s13_analysis_json_carries_all_three_keys_and_leaves_out_what_was_not_declared():
    """S17, S16, S13: analysis.json holds adverse's per-arm numerators (and no per-arm denominator, which it does not
    declare), its smallest effect with the threshold ln 2, and each named family; lift, which declares no smallest
    effect, has no such key."""
    result = analyse(scored_clusters(), families_prereg('cost = ["adverse"]\nquality = ["lift", "empty"]'))
    data = json.loads(_json_text(_finite(result)))
    contrasts = {c["name"]: c for c in data["contrasts"]}
    form = contrasts["adverse"]["ratio"]
    assert (form["numerator_a"], form["numerator_b"]) == ("spend_high", "spend_low")
    assert "denominator_a" not in form
    assert "denominator_b" not in form
    assert contrasts["adverse"]["smallest_effect"]["threshold"] == pytest.approx(math.log(2.0))
    assert contrasts["adverse"]["smallest_effect"]["decision"] == "meets"
    assert "smallest_effect" not in contrasts["lift"]
    assert [family["name"] for family in data["holm_families"]] == ["cost", "quality"]
