"""A preregistration run end to end: simulated rows with planted effects, estimators by name, Holm over the family."""

import dataclasses
import math
import tomllib

import numpy as np
import pytest
from stats_support import both_arms, make_row

from expcore.campaign.factors import FactorSchema
from expcore.stats import contrasts as kernel
from expcore.stats import estimators, power
from expcore.stats.analysis import (
    CONCLUSIONS,
    TreatmentOutcomesRefused,
    analyse,
    conclude,
    pilot_power,
    power_study,
    run_contrast,
)
from expcore.stats.estimators import EstimatorResult
from expcore.stats.pairing import ContrastSpec, pair_rows
from expcore.stats.prereg import name_digest, parse
from expcore.stats.rows import AbsentRun, Row
from expcore.stats.sim import OutcomeSpec, SimSpec, simulate

TEXT = """\
alpha = 0.05
holm_family = ["gate-effect", "open-vs-shut-at-x", "mode-effect"]

[bootstrap]
replicates = 400
seed = 17

[power]
seed = 2
n_simulations = 30
target_clusters = 20
target_difference = 0.1
sd_grid = [0.1]

[contrasts.gate-effect]
factor = "gate"
levels = ["open", "shut"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.open-vs-shut-at-x]
conditions = ["open-x", "shut-x"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.mode-effect]
factor = "mode"
levels = ["x", "y"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""

CONDITIONS = {
    "open-x": {"gate": "open", "mode": "x"},
    "shut-x": {"gate": "shut", "mode": "x"},
    "open-y": {"gate": "open", "mode": "y"},
    "shut-y": {"gate": "shut", "mode": "y"},
}
SCHEMA = FactorSchema(
    {"gate": ["open", "shut"], "mode": ["x", "y"]}, ["task", "gate", "mode", "model", "seed"], CONDITIONS
)
RATE = {"open-x": 0.7, "shut-x": 0.5, "open-y": 0.7, "shut-y": 0.5}  # the gate adds 0.2; the mode adds nothing


ARMS = FactorSchema(
    {"arm": ["a", "b", "x", "y"]},
    ["task", "arm", "model", "seed"],
    {f"cond-{arm}": {"arm": arm} for arm in "abxy"},
)


def manifest(sha):
    """The manifest entries of a preregistration with this hash, checked against the SCHEMA above."""
    return {"preregistration_sha256": sha, "factor_schema_sha256": SCHEMA.digest}


@pytest.fixture
def prereg():
    """The preregistration above, parsed from a mapping with a fixed hash."""
    return parse(tomllib.loads(TEXT), "inline", "a" * 64, SCHEMA)


@pytest.fixture(scope="module")
def planted():
    """Rows from the simulator with the planted gate effect, and the spec that made them."""
    spec = SimSpec(
        CONDITIONS, {"rate": OutcomeSpec("proportion", RATE)}, n_clusters=30, tasks_per_cluster=2, seeds=(1, 2)
    )
    return spec, simulate(spec, np.random.default_rng(2026))


def test_the_pipeline_recovers_a_planted_effect_within_its_interval(prereg, planted):
    """Both contrast forms estimate the planted 0.2 and their intervals cover it; the null mode effect is near zero."""
    spec, rows = planted
    result = analyse(rows, prereg)
    truth = spec.true_contrast("rate", "open-x", "shut-x")
    for name in ("gate-effect", "open-vs-shut-at-x"):
        contrast = result.contrast(name)
        assert contrast.estimate == pytest.approx(truth, abs=0.04)
        assert contrast.ci_low < truth < contrast.ci_high
        assert contrast.p_value < 1e-4
        assert contrast.n_clusters == 30
    null = result.contrast("mode-effect")
    assert abs(null.estimate) < 0.05
    assert null.p_value > 0.05


def test_holm_runs_over_the_declared_family_with_the_exact_levels(prereg, planted):
    """The decisions follow the family order, the levels are alpha over the remaining hypotheses, and the null stays."""
    _, rows = planted
    result = analyse(rows, prereg)
    assert [d.name for d in result.holm] == ["gate-effect", "open-vs-shut-at-x", "mode-effect"]
    assert [d.reject for d in result.holm] == [True, True, False]
    assert sorted(d.level for d in result.holm) == pytest.approx([0.05 / 3, 0.05 / 2, 0.05])
    for decision in result.holm:
        assert decision.p_value == result.contrast(decision.name).p_value
        assert decision.p_holm >= decision.p_value


def test_p04_the_result_carries_the_hash_of_the_preregistration_it_was_computed_under(prereg, planted):
    """P04: the manifest entry is the preregistration hash, and an override shows in it."""
    _, rows = planted
    assert analyse(rows, prereg).manifest == manifest("a" * 64)
    small = prereg.with_override(alpha=0.01)
    assert analyse(rows, small).manifest == manifest("a" * 64 + "+override")
    assert analyse(rows, small).alpha == 0.01


def test_a_contrast_gives_the_same_result_whichever_contrasts_run_with_it(prereg, planted):
    """Its seed comes from its own name, so adding or removing other contrasts leaves its interval alone."""
    _, rows = planted
    alone = prereg.with_override(
        contrasts={"gate-effect": prereg.contrasts["gate-effect"]}, holm_family=("gate-effect",)
    )
    one, many = analyse(rows, alone).contrast("gate-effect"), analyse(rows, prereg).contrast("gate-effect")
    # the minimum detectable effect is judged at alpha over the family size, so it is the one field that may differ
    assert dataclasses.replace(one, minimum_detectable_effect=None) == dataclasses.replace(
        many, minimum_detectable_effect=None
    )
    assert one.minimum_detectable_effect < many.minimum_detectable_effect


def test_the_same_rows_and_preregistration_give_the_same_result_in_any_row_order(prereg, planted):
    """The analysis is a function of the set of rows."""
    _, rows = planted
    assert analyse(list(reversed(rows)), prereg) == analyse(rows, prereg)


def test_s02_an_untestable_contrast_keeps_its_place_in_the_family():
    """S02: a family with a NaN keeps its size m; the other contrast is judged at alpha / 2, not alpha."""
    text = TEXT.replace('["gate-effect", "open-vs-shut-at-x", "mode-effect"]', '["with-data", "no-data"]')
    text = text[: text.index("[contrasts.gate-effect]")]
    text += '[contrasts.with-data]\nconditions = ["cond-a", "cond-b"]\noutcome = "score"\n'
    text += 'estimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n\n'
    text += '[contrasts.no-data]\nconditions = ["cond-x", "cond-y"]\noutcome = "score"\n'
    text += 'estimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n'
    family = parse(tomllib.loads(text), "inline", "c" * 64, ARMS)
    rows = []
    for i in range(6):  # six clusters, all positive and distinct: two-sided exact p = 2 / 2**6 = 0.03125
        rows += both_arms(f"t{i}", "model-a", 1, 0.9 - 0.01 * i, 0.5, cluster=f"c{i}")
    result = analyse(rows, family)
    assert result.contrast("with-data").p_value == pytest.approx(0.03125)
    assert math.isnan(result.contrast("no-data").p_value)
    decisions = {d.name: d for d in result.holm}
    assert decisions["with-data"].level == 0.05 / 2
    assert decisions["with-data"].reject is False  # in a family of one it would be rejected at 0.05
    assert decisions["with-data"].p_holm == pytest.approx(0.0625)
    assert math.isnan(decisions["no-data"].p_holm)
    assert decisions["no-data"].reject is False


def test_s03_the_counts_of_what_pairing_left_out_reach_the_result(prereg):
    """S03, R10: dropped clusters, unpaired runs and missing outcomes are reported with the estimate."""
    rows = []
    for i in range(6):
        rows += both_arms(f"t{i}", "model-a", 1, 0.8, 0.5, cluster=f"c{i}")
    rows.append(make_row("lonely", "model-a", 1, 0.9, condition="open-x", cluster="grp-i"))
    rows += [
        make_row("gone", "model-a", 1, None, condition="open-x", cluster="grp-j"),
        make_row("gone", "model-a", 1, 0.3, condition="shut-x", cluster="grp-j"),
    ]
    spec = ContrastSpec("x", "score", "wilcoxon_cluster_bootstrap", "group", conditions=("cond-a", "cond-b"))
    only_named = ContrastSpec("n", "score", "wilcoxon_cluster_bootstrap", "group", conditions=("open-x", "shut-x"))
    result = run_contrast(rows, spec, prereg)
    assert (result.n_clusters, result.n_clusters_dropped, result.n_pairs, result.n_unpaired) == (6, 0, 6, 0)
    assert result.counts == {"zero_dropped": 0}
    named = run_contrast(rows, only_named, prereg)
    assert (named.n_clusters, named.n_clusters_dropped, named.n_pairs_missing, named.n_unpaired) == (0, 2, 1, 1)
    assert math.isnan(named.estimate)
    assert math.isnan(named.p_value)


def test_the_contrast_interface_and_the_result_assume_no_estimator(prereg, planted, monkeypatch):
    """A preregistration can name any registered estimator; the same call and result type carry it."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))

    @estimators.register("sign_share")
    def sign_share(paired, settings):
        differences = paired.cluster_differences
        positive = float(np.mean(differences > 0))
        return EstimatorResult(float(np.mean(differences)), 1.0 - positive, math.nan, math.nan, len(differences))

    _, rows = planted
    text = TEXT.replace('estimator = "wilcoxon_cluster_bootstrap"', 'estimator = "sign_share"', 1)
    other = parse(tomllib.loads(text), "inline", "b" * 64, SCHEMA)
    result = analyse(rows, other)
    assert result.contrast("gate-effect").estimator == "sign_share"
    assert result.contrast("gate-effect").p_value == 0.0
    assert math.isnan(result.contrast("gate-effect").ci_low)
    assert result.contrast("open-vs-shut-at-x").estimator == "wilcoxon_cluster_bootstrap"


def test_a_contrast_naming_an_estimator_that_is_not_registered_fails_at_run_time_too(prereg):
    """A spec built in code, past the loader, still cannot reach an unknown estimator."""
    spec = ContrastSpec("x", "score", "bayes", "group", conditions=("cond-a", "cond-b"))
    with pytest.raises(estimators.UnknownEstimator, match="unknown estimator 'bayes'"):
        run_contrast(both_arms("task-a", "model-a", 1, 0.9, 0.5), spec, prereg)


def test_the_result_finds_a_contrast_by_name(prereg, planted):
    """Lookup by name, and a clear error for a name that was not declared."""
    _, rows = planted
    result = analyse(rows, prereg)
    assert result.contrast("mode-effect").name == "mode-effect"
    with pytest.raises(KeyError, match="no contrast named 'nope'; declared: gate-effect"):
        result.contrast("nope")


def test_pilot_power_pairs_the_pilot_rows_and_uses_the_preregistered_settings(prereg, planted):
    """Each contrast's SD comes from its own paired cluster differences; the grid and targets come from the file."""
    _, rows = planted
    result = pilot_power(rows, prereg, names=["gate-effect"])
    table = result.rows
    assert {row["contrast"] for row in table} == {"gate-effect"}
    assert {row["pilot_clusters"] for row in table} == {30}
    assert {(row["n_clusters"], row["delta"]) for row in table} == {(20, 0.1)}
    assert all(0.0 <= row["power"] <= 1.0 for row in table)
    assert len(pilot_power(rows, prereg).rows) == 3 * 2
    assert result.manifest == manifest("a" * 64)  # P04


def test_pilot_power_refuses_what_it_cannot_run(prereg, planted):
    """No power section, or an unknown contrast name, is an error that names the preregistration."""
    _, rows = planted
    with pytest.raises(ValueError, match=r"inline: has no \[power\] section"):
        pilot_power(rows, prereg.with_override(power=None))
    with pytest.raises(ValueError, match="inline: no contrast named 'nope'"):
        pilot_power(rows, prereg, names=["nope"])


def test_the_power_study_runs_on_the_preregistered_settings_and_the_family_size(prereg):
    """The grid, the targets, the seed and the number of simulations come from the file; alpha is divided by m = 3."""
    result = power_study(prereg)
    table = result.rows
    assert result.manifest == manifest("a" * 64)  # P04: the power table carries the hash too
    assert [(row["sd"], row["alpha_level"]) for row in table] == [(0.1, "alpha"), (0.1, "alpha_over_contrasts")]
    assert table[1]["alpha"] == pytest.approx(0.05 / 3)
    assert {(row["n_clusters"], row["delta"]) for row in table} == {(20, 0.1)}
    assert power_study(prereg) == result  # seeded by the file
    assert all(math.isnan(row["power"]) for row in power_study(prereg, simulate=False).rows)
    changed = prereg.with_override(power=dataclasses.replace(prereg.power, target_difference=0.3))
    assert power_study(changed).rows[0]["power"] > table[0]["power"]
    assert power_study(changed).manifest == manifest("a" * 64 + "+override")
    with pytest.raises(ValueError, match=r"inline: has no \[power\] section"):
        power_study(prereg.with_override(power=None))


# --- a chain design: the third condition runs on some of the tasks only ---------------------------------------------

CHAIN_TASKS = ["task-a", "task-b", "task-c", "task-d", "task-e", "task-f"]
CHAIN_CLUSTERS = dict(zip(CHAIN_TASKS, ["grp-a", "grp-a", "grp-b", "grp-b", "grp-c", "grp-c"], strict=True))
CHAIN = FactorSchema(
    {"stage": ["one", "two", "three"]},
    ["task", "stage", "model", "seed"],
    {"chain-one": {"stage": "one"}, "chain-two": {"stage": "two"}, "chain-three": {"stage": "three"}},
    tasks={"chain-three": ["task-a", "task-b", "task-c", "task-d"]},
)
CHAIN_TEXT = """\
alpha = 0.05
holm_family = ["one-vs-two", "two-vs-three"]

[bootstrap]
replicates = 200
seed = 5

[contrasts.one-vs-two]
conditions = ["chain-one", "chain-two"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.two-vs-three]
conditions = ["chain-two", "chain-three"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.one-vs-three]
conditions = ["chain-one", "chain-three"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def chain_score(stage, index, model):
    """The score of a run: a base per task, a step per stage that differs by task, and a shift per model."""
    base = 0.2 + 0.01 * index + (0.05 if model == "model-b" else 0.0)
    return base + {"one": 0.0, "two": 0.1 + 0.005 * index, "three": 0.15 + 0.01 * index}[stage]


def chain_rows(schema=CHAIN, stages=("one", "two", "three")):
    """Rows for the planned runs of the chain schema, as the export and the scorers would give them."""
    rows = []
    for key in schema.expand(CHAIN_TASKS, ["model-a", "model-b"], [1, 2]):
        factors = key.factors
        if factors["stage"] not in stages:
            continue
        index = CHAIN_TASKS.index(factors["task"])
        score = chain_score(factors["stage"], index, factors["model"])
        rows.append(
            Row(
                key.text,
                schema.condition_of(key),
                factors,
                {"score": score},
                {"group": CHAIN_CLUSTERS[factors["task"]]},
            )
        )
    return rows


@pytest.fixture
def chain():
    """The chain preregistration, checked against the chain schema."""
    return parse(tomllib.loads(CHAIN_TEXT), "inline", "d" * 64, CHAIN)


def test_a_condition_on_some_tasks_drops_the_others_from_its_contrasts_and_counts_them(chain):
    """Reviewer request: tasks missing in either arm leave that contrast, and the output says which and how many."""
    result = analyse(chain_rows(), chain)
    two_vs_three = result.contrast("two-vs-three")
    assert two_vs_three.tasks_dropped == ("task-e", "task-f")
    assert two_vs_three.n_tasks_dropped == 2
    assert two_vs_three.n_unpaired == 2 * 2 * 2  # two tasks, two models, two seeds
    assert (two_vs_three.n_clusters, two_vs_three.n_clusters_dropped) == (2, 1)  # grp-c had only the dropped tasks
    one_vs_three = result.contrast("one-vs-three")
    assert one_vs_three.tasks_dropped == ("task-e", "task-f")


def test_the_estimate_of_a_contrast_with_dropped_tasks_is_taken_over_the_tasks_both_arms_ran(chain):
    """Hand-computed: stage two against three on tasks a to d, per cluster the mean over its two tasks."""
    result = analyse(chain_rows(), chain).contrast("two-vs-three")
    step = [(0.15 + 0.01 * i) - (0.1 + 0.005 * i) for i in range(4)]  # three minus two, so the contrast is negated
    expected = -((step[0] + step[1]) / 2 + (step[2] + step[3]) / 2) / 2
    assert result.estimate == pytest.approx(expected)


def test_other_contrasts_are_unaffected_by_a_condition_that_runs_on_some_tasks_only(chain):
    """The all-task contrast gives exactly the result it gives when the third condition does not exist."""
    with_chain = analyse(chain_rows(), chain)
    without = parse(tomllib.loads(CHAIN_TEXT), "inline", "d" * 64, CHAIN).with_override(
        contrasts={"one-vs-two": chain.contrasts["one-vs-two"]}, holm_family=("one-vs-two",)
    )
    alone = analyse(chain_rows(stages=("one", "two")), without)
    assert with_chain.contrast("one-vs-two") == alone.contrast("one-vs-two")
    assert with_chain.contrast("one-vs-two").tasks_dropped == ()
    assert with_chain.contrast("one-vs-two").n_clusters == 3


def test_the_family_keeps_every_contrast_when_one_of_them_drops_tasks(chain):
    """Holm runs over the declared family whatever each contrast dropped; the dropped tasks do not shrink m."""
    result = analyse(chain_rows(), chain)
    assert [d.name for d in result.holm] == ["one-vs-two", "two-vs-three"]
    assert sorted(d.level for d in result.holm) == pytest.approx([0.05 / 2, 0.05])


# --- conclusions: difference, trivial_difference, equivalent, not_detected; the minimum detectable effect -----------

ARM_CONDITIONS = {f"cond-{arm}": {"arm": arm} for arm in "pqr"}
ARM_SCHEMA = FactorSchema({"arm": ["p", "q", "r"]}, ["task", "arm", "model", "seed"], ARM_CONDITIONS)
ARM_MEANS = {"cond-p": 0.6, "cond-q": 0.4, "cond-r": 0.4}  # p differs from q by 0.2; q and r are the same
CONCLUSION_TEXT = """\
alpha = 0.05
holm_family = ["p-vs-q", "q-vs-r"]

[bootstrap]
replicates = 400
seed = 3

[power]
seed = 4
n_simulations = 300
target_clusters = 20
target_difference = 0.1
sd_grid = [0.1]

[contrasts.p-vs-q]
conditions = ["cond-p", "cond-q"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.q-vs-r]
conditions = ["cond-q", "cond-r"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.q-vs-r-margin]
conditions = ["cond-q", "cond-r"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.15

[contrasts.q-vs-r-tiny-margin]
conditions = ["cond-q", "cond-r"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.002

[contrasts.p-vs-q-wide-margin]
conditions = ["cond-p", "cond-q"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.5
"""


@pytest.fixture(scope="module")
def concluded():
    """The preregistration above run on simulated rows, with the preregistration itself."""
    prereg = parse(tomllib.loads(CONCLUSION_TEXT), "inline", "e" * 64, ARM_SCHEMA)
    spec = SimSpec(
        ARM_CONDITIONS,
        {"rate": OutcomeSpec("proportion", ARM_MEANS)},
        n_clusters=30,
        tasks_per_cluster=2,
        seeds=(1, 2),
    )
    return prereg, analyse(simulate(spec, np.random.default_rng(77)), prereg)


def test_a_significant_result_is_a_difference(concluded):
    """The planted 0.2 is rejected by Holm, and without a margin nothing makes it equivalent."""
    _, result = concluded
    found = result.contrast("p-vs-q")
    assert (found.significant, found.in_family) == (True, True)
    assert (found.equivalence_margin, found.equivalence_interval, found.equivalent) == (None, None, False)
    assert found.conclusion == "difference"


def test_a_non_significant_result_without_a_margin_is_not_detected_never_no_difference(concluded):
    """The same two arms with no planted effect: the test does not reject, and the result says only that."""
    _, result = concluded
    null = result.contrast("q-vs-r")
    assert null.significant is False
    assert null.p_value > 0.05
    assert (null.equivalence_margin, null.equivalence_interval, null.equivalent) == (None, None, False)
    assert null.conclusion == "not_detected"


def test_with_a_margin_an_interval_inside_it_is_equivalent(concluded):
    """The 1 - 2 alpha interval of the null contrast lies inside plus or minus 0.15, so it is equivalent."""
    _, result = concluded
    equivalent = result.contrast("q-vs-r-margin")
    low, high = equivalent.equivalence_interval
    assert -0.15 <= low <= high <= 0.15
    assert equivalent.equivalent is True
    assert equivalent.conclusion == "equivalent"
    assert equivalent.equivalence_margin == 0.15


def test_the_equivalence_interval_is_at_one_minus_two_alpha_not_at_the_reporting_level(concluded):
    """With alpha 0.05 the interval is the 90 per cent one, narrower than the 95 per cent interval of the result."""
    _, result = concluded
    contrast = result.contrast("q-vs-r-margin")
    low, high = contrast.equivalence_interval
    assert high - low < contrast.ci_high - contrast.ci_low
    assert contrast.ci_low < low
    assert high < contrast.ci_high


def test_a_margin_the_interval_is_too_wide_for_is_not_detected(concluded):
    """A margin was declared, but the interval does not fit in it, so equivalence is not shown."""
    _, result = concluded
    wide = result.contrast("q-vs-r-tiny-margin")
    low, high = wide.equivalence_interval
    assert not (low >= -0.002 and high <= 0.002)
    assert wide.equivalent is False
    assert wide.significant is False
    assert wide.conclusion == "not_detected"


def test_a_significant_effect_inside_the_margin_is_a_trivial_difference_not_equivalent(concluded):
    """A detected effect shown to be inside the preregistered margin keeps its own word; the test is unchanged."""
    _, result = concluded
    both = result.contrast("p-vs-q-wide-margin")
    assert both.significant is True
    assert both.equivalent is True  # the interval is inside the margin, which says nothing of the test
    assert both.conclusion == "trivial_difference"


def test_the_four_conclusions_follow_the_test_and_the_margin():
    """Equivalent needs the test not to reject, and a significant effect is never reported as equivalent."""
    assert conclude(significant=True, equivalent=False) == "difference"
    assert conclude(significant=True, equivalent=True) == "trivial_difference"
    assert conclude(significant=False, equivalent=True) == "equivalent"
    assert conclude(significant=False, equivalent=False) == "not_detected"


def test_a_table_of_conclusions_alone_cannot_hide_a_detected_effect(concluded):
    """A conclusion names a detected effect (difference or trivial_difference) exactly when the test is significant."""
    _, result = concluded
    for contrast in result.contrasts:
        assert (contrast.conclusion in ("difference", "trivial_difference")) is contrast.significant, contrast.name
    tested = set(CONCLUSIONS) - {"untestable"}  # every contrast of the fixture has pairs; S11 tests the fifth word
    assert {c.conclusion for c in result.contrasts} == tested  # the fixture shows the four words of a tested contrast


def test_a_contrast_outside_the_family_is_judged_by_its_raw_p_value(concluded):
    """Nothing adjusts for a contrast outside the Holm family, and the result says so."""
    _, result = concluded
    outside = result.contrast("p-vs-q-wide-margin")
    assert outside.in_family is False
    assert outside.significant is (outside.p_value <= result.alpha)
    assert result.contrast("p-vs-q").in_family is True


def test_a_contrast_outside_the_family_is_labelled_exploratory(concluded):
    """Outside the Holm family nothing controls multiplicity, so the result carries the label; a member does not."""
    _, result = concluded
    assert result.contrast("p-vs-q-wide-margin").exploratory is True
    assert result.contrast("q-vs-r-margin").exploratory is True
    assert result.contrast("p-vs-q").exploratory is False
    assert result.contrast("q-vs-r").exploratory is False
    assert all(c.exploratory is (not c.in_family) for c in result.contrasts)


def test_the_exploratory_label_is_a_field_of_the_result_not_something_a_report_must_work_out(concluded):
    """It is part of the result's data, so a table built from the fields cannot leave it out."""
    _, result = concluded
    assert "exploratory" in {field.name for field in dataclasses.fields(result.contrasts[0])}
    assert dataclasses.asdict(result.contrast("q-vs-r-margin"))["exploratory"] is True


def test_every_conclusion_is_one_of_the_five_words(concluded):
    """The vocabulary is closed."""
    _, result = concluded
    assert {c.conclusion for c in result.contrasts} <= set(CONCLUSIONS)
    assert set(CONCLUSIONS) == {"difference", "trivial_difference", "equivalent", "not_detected", "untestable"}


def test_a_family_member_is_significant_by_holm_not_by_its_raw_p_value():
    """p = 0.03125 is below alpha, but Holm in a family of two asks 0.025, so the conclusion is not_detected."""
    text = CONCLUSION_TEXT.replace('["p-vs-q", "q-vs-r"]', '["with-data", "no-data"]')
    text = text[: text.index("[contrasts.p-vs-q]")]
    for name, pair in (("with-data", '"cond-p", "cond-q"'), ("no-data", '"cond-q", "cond-r"')):
        text += f'[contrasts.{name}]\nconditions = [{pair}]\noutcome = "score"\n'
        text += 'estimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n\n'
    prereg = parse(tomllib.loads(text), "inline", "f" * 64, ARM_SCHEMA)
    rows = []
    for i in range(6):  # six clusters, all positive and distinct: two-sided exact p = 2 / 2**6 = 0.03125
        rows += [
            make_row(f"t{i}", "model-a", 1, 0.9 - 0.01 * i, condition="cond-p", cluster=f"c{i}", arm="p"),
            make_row(f"t{i}", "model-a", 1, 0.5, condition="cond-q", cluster=f"c{i}", arm="q"),
        ]
    result = analyse(rows, prereg)
    found = result.contrast("with-data")
    assert found.p_value == pytest.approx(0.03125)
    assert found.p_value < 0.05
    assert (found.significant, found.conclusion) == (False, "not_detected")
    assert result.contrast("no-data").conclusion == "untestable"  # no pair at all: nothing was tested (S11)


def test_the_equivalence_interval_comes_from_the_estimator_at_the_level_it_is_asked_for(prereg, planted, monkeypatch):
    """No estimator is assumed: each is asked once at the reporting level and once at 1 - 2 alpha."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    levels = []

    @estimators.register("fixed_width")
    def fixed_width(paired, settings):
        levels.append(settings.level)
        half = 1 - settings.level  # a wider interval at a higher level, whatever the data
        return EstimatorResult(0.0, 0.5, -half, half, len(paired.clusters))

    text = TEXT.replace('estimator = "wilcoxon_cluster_bootstrap"', 'estimator = "fixed_width"', 1)
    text = text.replace('cluster = "group"', 'cluster = "group"\nequivalence_margin = 0.06', 1)
    other = parse(tomllib.loads(text), "inline", "b" * 64, SCHEMA)
    _, rows = planted
    result = analyse(rows, other).contrast("gate-effect")
    assert levels == [0.95, 0.9]  # the reporting level, then 1 - 2 alpha with alpha = 0.05
    assert result.equivalence_interval == pytest.approx((-0.1, 0.1))
    assert result.conclusion == "not_detected"  # the interval at 0.9 is +-0.1, outside the margin 0.06
    tighter = dataclasses.replace(other.contrasts["gate-effect"], equivalence_margin=0.11)
    assert run_contrast(rows, tighter, other).conclusion == "equivalent"


def test_the_minimum_detectable_effect_is_computed_at_the_runs_cluster_count_from_the_power_settings(concluded):
    """A family member is judged at alpha over the family size, any other contrast at alpha."""
    _, result = concluded
    member = result.contrast("p-vs-q")
    outside = result.contrast("q-vs-r-margin")
    for contrast in (member, outside):
        assert contrast.n_clusters == 30
        assert contrast.minimum_detectable_effect > 0
    assert member.minimum_detectable_effect < 0.2  # the planted 0.2 was detected


def test_the_minimum_detectable_effect_is_the_power_modules_for_the_contrasts_own_sd_and_cluster_count(concluded):
    """The value is the one the power module gives for the contrast's cluster differences, seeded by its name.

    At the default method and 30 clusters the analysis's Wilcoxon test is the exact one, so the power module's own
    vectorised exact test and the contrast's p-value function agree and the two routes give the same figure.
    """
    prereg, result = concluded
    spec = prereg.contrasts["p-vs-q"]
    rows = simulate(
        SimSpec(
            ARM_CONDITIONS,
            {"rate": OutcomeSpec("proportion", ARM_MEANS)},
            n_clusters=30,
            tasks_per_cluster=2,
            seeds=(1, 2),
        ),
        np.random.default_rng(77),
    )
    differences = pair_rows(rows, spec).differences.cluster_differences
    expected = power.minimum_detectable_effect(
        power.pilot_cluster_sd(differences),
        30,
        0.05 / 2,
        prereg.power,
        np.random.default_rng([prereg.power.seed, name_digest("p-vs-q")]),
    )
    assert result.contrast("p-vs-q").minimum_detectable_effect == expected


def test_without_power_settings_there_is_no_minimum_detectable_effect(concluded):
    """None, not NaN and not 0: nothing was asked for."""
    prereg, _ = concluded
    rows = simulate(
        SimSpec(ARM_CONDITIONS, {"rate": OutcomeSpec("proportion", ARM_MEANS)}, n_clusters=8, seeds=(1,)),
        np.random.default_rng(1),
    )
    result = analyse(rows, prereg.with_override(power=None))
    assert all(c.minimum_detectable_effect is None for c in result.contrasts)


def test_the_minimum_detectable_effect_is_nan_when_the_clusters_are_too_few_or_do_not_vary(concluded):
    """Five clusters cannot reach p below 0.0625, and identical cluster differences have no SD to scale by."""
    prereg, _ = concluded
    few = simulate(
        SimSpec(ARM_CONDITIONS, {"rate": OutcomeSpec("proportion", ARM_MEANS)}, n_clusters=5, seeds=(1,)),
        np.random.default_rng(2),
    )
    assert math.isnan(analyse(few, prereg).contrast("p-vs-q").minimum_detectable_effect)
    flat = [
        make_row(f"t{i}", "model-a", 1, score, condition=f"cond-{arm}", cluster=f"c{i}", outcome="rate", arm=arm)
        for i in range(8)
        for arm, score in (("p", 0.7), ("q", 0.5))
    ]
    assert math.isnan(analyse(flat, prereg).contrast("p-vs-q").minimum_detectable_effect)


FIVE_CLUSTERS_TEXT = """\
alpha = 0.05
holm_family = ["p-vs-q"]

[bootstrap]
replicates = 100
seed = 3

[power]
seed = 4
n_simulations = 200
target_clusters = 20
target_difference = 0.1
sd_grid = [0.1]

[estimators.wilcoxon_cluster_bootstrap]
method = "METHOD"

[contrasts.p-vs-q]
conditions = ["cond-p", "cond-q"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def five_distinct_positive_clusters():
    """Five clusters whose paired differences are 0.40, 0.39, 0.38, 0.37 and 0.36: all positive and distinct."""
    rows = []
    for i in range(5):
        rows += [
            make_row(
                f"t{i}", "model-a", 1, 0.9 - 0.01 * i, condition="cond-p", cluster=f"c{i}", outcome="rate", arm="p"
            ),
            make_row(f"t{i}", "model-a", 1, 0.5, condition="cond-q", cluster=f"c{i}", outcome="rate", arm="q"),
        ]
    return rows


def test_s06_s09_the_minimum_detectable_effect_uses_the_preregistered_wilcoxon_method():
    """S06, S09: five clusters at alpha 0.05 reject by the normal approximation (p = 0.043), never by the exact test."""
    rows = five_distinct_positive_clusters()
    found = {}
    for method in ("approx", "exact"):
        prereg = parse(tomllib.loads(FIVE_CLUSTERS_TEXT.replace("METHOD", method)), "inline", "f" * 64, ARM_SCHEMA)
        found[method] = analyse(rows, prereg).contrast("p-vs-q")
    assert found["approx"].p_value == pytest.approx(0.043114446783)
    assert found["exact"].p_value == pytest.approx(0.0625)
    assert math.isfinite(found["approx"].minimum_detectable_effect)
    assert found["approx"].minimum_detectable_effect > 0
    assert math.isnan(found["exact"].minimum_detectable_effect)
    assert found["approx"].significant is True  # the analysis itself rejects at this p-value, as the simulation does
    assert found["exact"].significant is False


def test_s06_s09_the_minimum_detectable_effect_follows_the_override_of_the_estimator_parameters():
    """S06, S09: the same rows, a different preregistered method, a different figure; the default is exact at n = 5."""
    rows = five_distinct_positive_clusters()
    text = FIVE_CLUSTERS_TEXT.replace('[estimators.wilcoxon_cluster_bootstrap]\nmethod = "METHOD"\n\n', "")
    prereg = parse(tomllib.loads(text), "inline", "f" * 64, ARM_SCHEMA)
    assert math.isnan(analyse(rows, prereg).contrast("p-vs-q").minimum_detectable_effect)  # auto is exact at n = 5
    approximate = prereg.with_override(estimator_params={"wilcoxon_cluster_bootstrap": {"method": "approx"}})
    assert math.isfinite(analyse(rows, approximate).contrast("p-vs-q").minimum_detectable_effect)


def test_s09_an_estimator_without_a_p_values_function_has_no_minimum_detectable_effect(prereg, planted, monkeypatch):
    """S09: no stand-in test is simulated for an estimator that cannot say how it computes its p-value."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    estimators.register("sign_share")(
        lambda paired, settings: EstimatorResult(0.1, 0.5, math.nan, math.nan, len(paired.clusters))
    )
    _, rows = planted
    text = TEXT.replace('estimator = "wilcoxon_cluster_bootstrap"', 'estimator = "sign_share"', 1)
    other = parse(tomllib.loads(text), "inline", "b" * 64, SCHEMA)
    result = analyse(rows, other)
    assert math.isnan(result.contrast("gate-effect").minimum_detectable_effect)
    assert result.contrast("open-vs-shut-at-x").minimum_detectable_effect > 0


# --- treatment attempts in a contrast (S10) -----------------------------------------------------------------------

TREATED_TEXT = """\
alpha = 0.05
holm_family = ["FAMILY"]

[bootstrap]
replicates = 50
seed = 11

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
DECLARATION
[contrasts.x-vs-y]
conditions = ["cond-x", "cond-y"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def treated_prereg(family: str = "a-vs-b", declaration: str | None = None, tolerance: int | None = None):
    """A preregistration over ARMS with a-vs-b and x-vs-y; `family` is the one Holm member, a-vs-b may declare."""
    line = "" if declaration is None else f'treatment_outcomes = "{declaration}"\n'
    line += "" if tolerance is None else f"max_unscored_treatment = {tolerance}\n"
    text = TREATED_TEXT.replace("FAMILY", family).replace("DECLARATION\n", line)
    return parse(tomllib.loads(text), "inline", "e" * 64, ARMS)


def treated_rows() -> list[Row]:
    """Six clusters of ordinary pairs, then two pairs whose cond-a run is a treatment attempt the scorer left None."""
    rows = []
    for i in range(6):
        rows += both_arms(f"t{i}", "model-a", 1, 0.9 - 0.01 * i, 0.5, cluster=f"c{i}")
    for i in range(2):
        first, second = both_arms(f"outage-{i}", "model-a", 1, None, 0.5, cluster=f"c{i}")
        rows += [dataclasses.replace(first, treatment=True), second]
    return rows


def test_s10_a_confirmatory_contrast_that_loses_a_pair_to_a_treatment_attempts_none_is_refused():
    """S10: the condition's manipulation would silently leave its own contrast; the refusal names the contrast, the
    count and both ways to fix it."""
    with pytest.raises(TreatmentOutcomesRefused) as caught:
        analyse(treated_rows(), treated_prereg())
    message = str(caught.value)
    assert "confirmatory contrast 'a-vs-b'" in message
    assert "2 pair(s)" in message
    assert "outcome 'score'" in message
    assert 'treatment_outcomes = "scored"' in message
    assert 'treatment_outcomes = "excluded"' in message
    assert "[contrasts.a-vs-b]" in message


def test_s10_declaring_scored_does_not_excuse_a_scorer_that_leaves_every_treatment_attempt_none():
    """S10: "scored" is a claim the analysis checks. With every outage left None, the declaration is false and the
    contrast is refused, naming the claim, the count and the ways out, or issue #39 would survive behind one line."""
    with pytest.raises(TreatmentOutcomesRefused) as caught:
        analyse(treated_rows(), treated_prereg(declaration="scored"))
    message = str(caught.value)
    assert "confirmatory contrast 'a-vs-b'" in message
    assert 'declares treatment_outcomes = "scored"' in message
    assert "2 pair(s)" in message
    assert "max_unscored_treatment is 0" in message
    assert "[contrasts.a-vs-b]" in message


def measured_treatment_pair() -> list[Row]:
    """One pair whose cond-a run is a treatment attempt the scorer did measure."""
    first, second = both_arms("outage-measured", "model-a", 1, 0.2, 0.5, cluster="c0")
    return [dataclasses.replace(first, treatment=True), second]


def test_s10_a_preregistered_tolerance_allows_exactly_that_many_unscored_treatment_pairs():
    """S10: for a rare genuine "could not be measured", a member may lose as many pairs as its preregistration set
    beforehand, and the result says so; one more than that is refused."""
    rows = treated_rows() + measured_treatment_pair()
    result = analyse(rows, treated_prereg(declaration="scored", tolerance=2)).contrast("a-vs-b")
    assert (result.in_family, result.treatment_outcomes, result.max_unscored_treatment) == (True, "scored", 2)
    assert (result.n_pairs, result.pairs_missing["treatment_outcome_none"]) == (7, 2)
    with pytest.raises(TreatmentOutcomesRefused, match="max_unscored_treatment is 1"):
        analyse(rows, treated_prereg(declaration="scored", tolerance=1))


@pytest.mark.parametrize("tolerance", [2, 100])
def test_s10_no_tolerance_excuses_a_contrast_in_which_no_treatment_attempt_was_measured(tolerance):
    """S10: a tolerance is for the rare attempt, so a large one cannot bring back the original bug of a scorer that
    leaves every outage None."""
    with pytest.raises(TreatmentOutcomesRefused, match="no treatment attempt of the contrast was measured"):
        analyse(treated_rows(), treated_prereg(declaration="scored", tolerance=tolerance))


def test_s10_a_treatment_attempt_left_none_beside_an_exhausted_partner_is_refused_too():
    """S10: the partner's exhaustion must not hide that the scorer left the manipulation None."""
    rows = treated_rows()[:12]
    first, _ = both_arms("outage-0", "model-a", 1, None, 0.5, cluster="c0")
    factors = {"task": "outage-0", "model": "model-a", "seed": 1}
    partner = AbsentRun("task=outage-0|model=model-a|seed=1|condition=cond-b", "cond-b", factors, "exhausted")
    with pytest.raises(TreatmentOutcomesRefused, match="1 pair"):
        analyse([*rows, dataclasses.replace(first, treatment=True)], treated_prereg(declaration="scored"), [partner])


def unfinished_partner(task: str) -> AbsentRun:
    """The cond-b run of `task`, planned, with no analysed attempt."""
    factors = {"task": task, "model": "model-a", "seed": 1}
    return AbsentRun(f"task={task}|model=model-a|seed=1|condition=cond-b", "cond-b", factors, "unfinished")


def test_s10_a_measured_treatment_attempt_whose_partner_is_unfinished_still_counts_as_measured():
    """S10: the tolerance refuses a contrast that measured none of its treatment attempts, not one whose measured
    attempt lost its partner to something else."""
    first, _ = both_arms("outage-measured", "model-a", 1, 0.2, 0.5, cluster="c0")
    rows = [*treated_rows(), dataclasses.replace(first, treatment=True)]
    prereg = treated_prereg(declaration="scored", tolerance=2)
    result = analyse(rows, prereg, [unfinished_partner("outage-measured")]).contrast("a-vs-b")
    assert (result.n_pairs, result.pairs_missing["treatment_outcome_none"]) == (6, 2)


def test_s10_a_treatment_attempt_on_a_task_one_arm_never_runs_does_not_refuse_the_contrast():
    """S10: in a chain design the extra task is outside the contrast's pairs, so a None there costs it nothing."""
    first, _ = both_arms("extra-task", "model-a", 1, None, 0.5, cluster="c0")
    rows = [*treated_rows()[:12], dataclasses.replace(first, treatment=True)]
    result = analyse(rows, treated_prereg()).contrast("a-vs-b")
    assert (result.n_pairs, result.pairs_missing["treatment_outcome_none"]) == (6, 0)
    assert result.pairs_missing["unpaired_or_left_out"] == 1


def test_s10_excluded_is_refused_when_only_treatment_attempts_without_analysed_partners_remain():
    """S10: an excluded attempt with an unfinished partner still counts as excluded, so the empty contrast is seen."""
    first, _ = both_arms("outage-0", "model-a", 1, None, 0.5, cluster="c0")
    rows = [dataclasses.replace(first, treatment=True)]
    with pytest.raises(TreatmentOutcomesRefused, match="leaves it no pair: all 1 are treatment attempts"):
        analyse(rows, treated_prereg(declaration="excluded"), [unfinished_partner("outage-0")])


def test_s10_excluded_may_not_leave_a_confirmatory_contrast_with_no_pair():
    """S10: with every pair a treatment attempt, "excluded" leaves nothing to estimate, and a conclusion of "not
    detected" would read as no outage effect."""
    with pytest.raises(TreatmentOutcomesRefused, match="leaves it no pair: all 2 are treatment attempts"):
        analyse(treated_rows()[-4:], treated_prereg(declaration="excluded"))


def test_s10_excluded_lets_the_confirmatory_contrast_through_and_counts_what_it_left_out():
    """S10: "excluded" leaves out every pair with a treatment attempt, counted apart, and the result says so: the
    design fixes the population, whatever the scorer returned."""
    result = analyse(treated_rows(), treated_prereg(declaration="excluded")).contrast("a-vs-b")
    assert (result.in_family, result.treatment_outcomes, result.max_unscored_treatment) == (True, "excluded", 0)
    assert result.n_pairs == 6
    assert (result.pairs_missing["treatment_outcome_none"], result.pairs_missing["treatment_excluded"]) == (0, 2)


def test_s10_an_exploratory_contrast_reports_the_pairs_it_loses_to_a_treatment_attempts_none_and_is_not_refused():
    """S10: outside the family nothing is claimed confirmatory, so the drop is reported and the analysis goes on."""
    result = analyse(treated_rows(), treated_prereg(family="x-vs-y")).contrast("a-vs-b")
    assert (result.exploratory, result.treatment_outcomes) == (True, None)
    assert result.pairs_missing["treatment_outcome_none"] == 2
    assert result.n_pairs == 6


# --- a contrast with no usable pair (S11) -------------------------------------------------------------------------

UNTESTABLE_TEXT = """\
alpha = 0.05
holm_family = FAMILY

[bootstrap]
replicates = 100
seed = 9

[power]
seed = 4
n_simulations = 30
target_clusters = 20
target_difference = 0.1
sd_grid = [0.1]

[contrasts.p-vs-q]
conditions = ["cond-p", "cond-q"]
outcome = "rate"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.q-vs-r]
conditions = ["cond-q", "cond-r"]
outcome = "rate"
estimator = "ESTIMATOR"
cluster = "group"
"""


def untestable_prereg(family=("p-vs-q", "q-vs-r"), estimator="wilcoxon_cluster_bootstrap"):
    """p-vs-q has data; q-vs-r is the contrast whose partners are all missing. Power settings are present."""
    text = UNTESTABLE_TEXT.replace("FAMILY", str(list(family)).replace("'", '"')).replace("ESTIMATOR", estimator)
    return parse(tomllib.loads(text), "inline", "d" * 64, ARM_SCHEMA)


def untestable_inputs(cause: str) -> tuple[list[Row], list[AbsentRun]]:
    """Six clusters of p-vs-q pairs; every cond-r partner of q-vs-r is missing for `cause`."""
    rows, absent = [], []
    for i in range(6):
        cluster = f"c{i}"
        rows.append(
            make_row(
                f"t{i}", "model-a", 1, 0.9 - 0.01 * i, condition="cond-p", cluster=cluster, outcome="rate", arm="p"
            )
        )
        rows.append(make_row(f"t{i}", "model-a", 1, 0.5, condition="cond-q", cluster=cluster, outcome="rate", arm="q"))
        if cause == "outcome_none":  # an ordinary run the scorer could not measure
            rows.append(
                make_row(f"t{i}", "model-a", 1, None, condition="cond-r", cluster=cluster, outcome="rate", arm="r")
            )
        else:
            factors = {"task": f"t{i}", "model": "model-a", "seed": 1, "arm": "r"}
            absent.append(AbsentRun(f"task=t{i}|arm=r|model=model-a|seed=1", "cond-r", factors, cause))
    return rows, absent


CAUSE_OF = {"exhausted": "exhausted", "unfinished": "unpaired_or_left_out", "outcome_none": "outcome_none"}


@pytest.mark.parametrize("cause", sorted(CAUSE_OF))
def test_s11_a_contrast_with_no_usable_pair_is_untestable_whatever_the_cause(cause):
    """S11: no evidence is never "not_detected": every partner exhausted, unfinished or scored None on an ordinary run
    makes the contrast untestable, never significant, with no p-value, no estimate and no minimum detectable effect
    although the preregistration has power settings."""
    rows, absent = untestable_inputs(cause)
    result = analyse(rows, untestable_prereg(), absent)
    empty = result.contrast("q-vs-r")
    assert (empty.n_pairs, empty.pairs_missing[CAUSE_OF[cause]]) == (0, 6)
    assert (empty.conclusion, empty.significant, empty.equivalent) == ("untestable", False, False)
    assert math.isnan(empty.p_value)
    assert math.isnan(empty.estimate)
    assert empty.minimum_detectable_effect is None
    tested = result.contrast("p-vs-q")
    assert tested.conclusion != "untestable"
    assert tested.minimum_detectable_effect is not None


def test_s11_an_untestable_member_stays_in_the_family_as_p_one_whatever_its_estimator_returned(monkeypatch):
    """S11, S02: the family keeps its size, and the other member's adjusted p-value and level are what Holm gives with
    p = 1 for the untestable one, which is also what it gives for a NaN. An estimator that returns a number for no
    data is not believed: the contrast is still untestable and cannot reject."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    estimators.register("eager")(lambda paired, settings: EstimatorResult(0.3, 0.0001, 0.1, 0.5, len(paired.clusters)))
    rows, absent = untestable_inputs("exhausted")
    result = analyse(rows, untestable_prereg(estimator="eager"), absent)
    empty = result.contrast("q-vs-r")
    assert (empty.conclusion, empty.significant) == ("untestable", False)
    assert math.isnan(empty.p_value)
    decisions = {d.name: d for d in result.holm}
    assert len(decisions) == 2
    assert decisions["q-vs-r"].reject is False
    assert math.isnan(decisions["q-vs-r"].p_holm)
    p = result.contrast("p-vs-q").p_value
    with_one, with_nan = kernel.holm([p, 1.0], 0.05), kernel.holm([p, math.nan], 0.05)
    assert with_one.adjusted[0] == with_nan.adjusted[0] == decisions["p-vs-q"].p_holm
    assert with_one.levels[0] == with_nan.levels[0] == decisions["p-vs-q"].level == 0.05 / 2


def test_s11_an_exploratory_contrast_with_no_usable_pair_is_untestable_too():
    """S11: outside the family the word is the same, and it is labelled exploratory wherever it is shown."""
    rows, absent = untestable_inputs("unfinished")
    empty = analyse(rows, untestable_prereg(family=("p-vs-q",)), absent).contrast("q-vs-r")
    assert (empty.exploratory, empty.conclusion, empty.minimum_detectable_effect) == (True, "untestable", None)


def test_s11_untestable_overrides_whatever_the_test_and_the_margin_say():
    """S11: with no pair, neither a significant test nor an interval inside a margin can reach any other word."""
    for significant in (True, False):
        for equivalent in (True, False):
            assert conclude(significant=significant, equivalent=equivalent, untestable=True) == "untestable"
    assert conclude(significant=False, equivalent=False) == "not_detected"
