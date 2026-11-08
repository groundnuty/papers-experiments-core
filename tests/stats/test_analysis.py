"""A preregistration run end to end: simulated rows with planted effects, estimators by name, Holm over the family."""

import dataclasses
import math
import tomllib

import numpy as np
import pytest
from stats_support import both_arms, make_row

from expcore.campaign.factors import FactorSchema
from expcore.stats import estimators
from expcore.stats.analysis import analyse, pilot_power, power_study, run_contrast
from expcore.stats.estimators import EstimatorResult
from expcore.stats.pairing import ContrastSpec
from expcore.stats.prereg import parse
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
    assert analyse(rows, alone).contrast("gate-effect") == analyse(rows, prereg).contrast("gate-effect")


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
