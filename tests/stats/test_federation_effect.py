"""C45: a sensitivity rerun that fits site as a fixed effect and federation as a random effect, then runs the primary
analysis unchanged on the adjusted outcomes. Every dataset here is simulated with a fixed seed and invented names."""

import copy
import dataclasses
import re
import tomllib

import numpy as np
import pytest

from expcore.campaign.factors import FactorSchema
from expcore.stats.analysis import analyse
from expcore.stats.federation_effect import adjust, federation_analysis
from expcore.stats.prereg import parse
from expcore.stats.rows import Row

SCHEMA = FactorSchema(
    {"arm": ["a", "b", "c"]}, ["task", "arm", "model", "seed"], {f"cond-{arm}": {"arm": arm} for arm in "abc"}
)
SCORE_TEXT = """\
alpha = 0.05
holm_family = ["a-vs-b"]

[bootstrap]
replicates = 200
seed = 5

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""
RATIO_TEXT = """\
alpha = 0.05
holm_family = ["a-vs-b", "spend-diff", "spend-per-win"]

[bootstrap]
replicates = 200
seed = 5

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.spend-diff]
conditions = ["cond-a", "cond-b"]
outcome = "spend"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.spend-per-win]
conditions = ["cond-a", "cond-b"]
estimator = "ratio"
numerator = "spend"
denominator = "wins"
zero_denominator = "exclude"
cluster = "group"
"""
RATIO_ONLY_TEXT = RATIO_TEXT.replace('"a-vs-b", ', "").replace(
    """[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

""",
    "",
)
READ_BY_RATIO = "read by a ratio contrast: an additive adjustment of its sums is not defined"

SITE_OF = {
    "fed-1": "site-a",
    "fed-2": "site-a",
    "fed-3": "site-a",
    "fed-4": "site-b",
    "fed-5": "site-b",
    "fed-6": "site-b",
}
FEDERATIONS = tuple(SITE_OF)
FEDERATION_EFFECT = {
    "fed-1": 0.4,
    "fed-2": -0.1,
    "fed-3": -0.3,
    "fed-4": -0.4,
    "fed-5": 0.1,
    "fed-6": 0.3,
}  # zero per site
SITE_SHIFT = 2.0  # site-b runs score this much above site-a runs
CONDITION_EFFECT = {"cond-a": 0.5, "cond-b": 0.0}
MODEL_EFFECT = {"m1": 0.0, "m2": 0.3}


def prereg(text=SCORE_TEXT):
    """A preregistration over SCHEMA, parsed from inline text."""
    return parse(tomllib.loads(text), "inline", "f" * 64, SCHEMA)


def make_run(task, model, seed, condition, **outcomes):
    """One row as the export writes it; the task is its own cluster."""
    factors = {"task": task, "model": model, "seed": seed}
    run_key = "|".join(f"{name}={value}" for name, value in [*factors.items(), ("condition", condition)])
    return Row(run_key, condition, factors, outcomes, {"group": task})


def simulate(*, tasks=10, seed=2024, site_shift=SITE_SHIFT, federation_scale=1.0, noise=0.3, task_sd=0.5):
    """Rows and placement of a campaign on two sites and six federations.

    Every (condition, model, task, seed) run is placed by walking a seeded shuffle of the runs and giving the k-th the
    federation k modulo six, so the federations are near equal in size and interleaved across the cells. The planted
    effects sum to zero within each site, so the site's mean federation effect is zero and the site shift is what a
    site estimate should recover.
    """
    rng = np.random.default_rng(seed)
    task_effect = rng.normal(0.0, task_sd, tasks)
    plan = [
        (task, model, run_seed, condition)
        for task in range(tasks)
        for model in MODEL_EFFECT
        for run_seed in (1, 2)
        for condition in CONDITION_EFFECT
    ]
    rows, placement = [], {}
    for rank, index in enumerate(rng.permutation(len(plan))):
        task, model, run_seed, condition = plan[index]
        federation = FEDERATIONS[rank % len(FEDERATIONS)]
        site = SITE_OF[federation]
        score = (
            5.0
            + task_effect[task]
            + CONDITION_EFFECT[condition]
            + MODEL_EFFECT[model]
            + (site_shift if site == "site-b" else 0.0)
            + federation_scale * FEDERATION_EFFECT[federation]
            + rng.normal(0.0, noise)
        )
        spend = 100.0 + 20.0 * rng.normal()
        wins = int(rng.integers(1, 4))
        row = make_run(f"task-{task}", model, run_seed, condition, score=score, spend=spend, wins=wins)
        rows.append(row)
        placement[row.run_key] = (site, federation)
    return rows, placement


def scores(rows):
    """The score column of the rows, in row order."""
    return [row.outcomes["score"] for row in rows]


def cell_gaps(rows, placement, values):
    """For each (condition, model) cell, the mean of its site-b values minus the mean of its site-a values."""
    gaps = []
    for condition in CONDITION_EFFECT:
        for model in MODEL_EFFECT:
            at = {"site-a": [], "site-b": []}
            for row, value in zip(rows, values, strict=True):
                if (row.condition, row.model) == (condition, model):
                    at[placement[row.run_key][0]].append(value)
            assert at["site-a"], "every site must run every cell"
            assert at["site-b"], "every site must run every cell"
            gaps.append(float(np.mean(at["site-b"]) - np.mean(at["site-a"])))
    return gaps


def with_scores(rows, values):
    """The rows with their score replaced, None allowed."""
    return [
        dataclasses.replace(row, outcomes={**row.outcomes, "score": value})
        for row, value in zip(rows, values, strict=True)
    ]


def test_c45_the_estimated_site_effect_recovers_the_planted_shift():
    """C45: the fitted site-b minus site-a is the planted shift of 2.0. The planted federation effects sum to zero in
    each site, so what is left is noise: 0.3 over about 40 runs a site gives a standard error near 0.07, and the
    tolerance of 0.3 is four times that."""
    rows, placement = simulate()
    _, fit = adjust(rows, placement, "score")
    assert fit.refused is None
    assert fit.site_effects["site-b"] - fit.site_effects["site-a"] == pytest.approx(SITE_SHIFT, abs=0.3)


def test_c45_adjusted_outcomes_agree_across_sites_within_each_cell():
    """C45: within every (condition, model) cell the two sites' adjusted means are far closer than the raw ones: the
    raw gaps are about the shift, and every adjusted gap is under a quarter of the smallest raw gap. The planted task
    effects are small here (sd 0.1): which tasks a site happens to run shifts a cell's site gap by a task-composition
    term that no site adjustment can remove, and the test is about the site shift."""
    rows, placement = simulate(task_sd=0.1)
    adjusted, _ = adjust(rows, placement, "score")
    raw = cell_gaps(rows, placement, scores(rows))
    after = cell_gaps(rows, placement, adjusted)
    assert min(raw) > 1.5, raw
    assert max(abs(gap) for gap in after) < 0.25 * min(raw), (raw, after)


def test_c45_site_effects_are_centred_over_the_fitted_rows():
    """C45: the run-weighted mean of the site effects is zero, so the adjustment does not move the overall level."""
    rows, placement = simulate()
    _, fit = adjust(rows, placement, "score")
    sites = [placement[row.run_key][0] for row in rows]
    weighted = sum(fit.site_effects[site] for site in sites) / len(sites)
    assert weighted == pytest.approx(0.0, abs=1e-12)
    assert fit.sites == ("site-a", "site-b")
    assert fit.federations == FEDERATIONS


def dense_pieces(rows, placement, lam):
    """The fit by dense algebra at the variance ratio `lam`: coefficients, residuals, the restricted objective and H.

    The design is one column per (condition, model) cell, the tasks after the first, and site-b."""
    y = np.array(scores(rows))
    cells = sorted({(row.condition, row.model) for row in rows})
    tasks = sorted({row.task for row in rows})
    columns = [[float((row.condition, row.model) == cell) for row in rows] for cell in cells]
    columns += [[float(row.task == task) for row in rows] for task in tasks[1:]]
    columns += [[float(placement[row.run_key][0] == "site-b") for row in rows]]
    x = np.array(columns).T
    z = np.array([[float(placement[row.run_key][1] == fed) for fed in FEDERATIONS] for row in rows])
    n, rank = x.shape
    assert np.linalg.matrix_rank(x) == rank
    h = np.eye(n) + lam * z @ z.T
    h_inv = np.linalg.inv(h)
    a = x.T @ h_inv @ x
    beta = np.linalg.solve(a, x.T @ h_inv @ y)
    residual = y - x @ beta
    rss = float(residual @ h_inv @ residual)
    objective = (n - rank) * np.log(rss / (n - rank)) + np.linalg.slogdet(h)[1] + np.linalg.slogdet(a)[1]
    return beta, residual, rss / (n - rank), float(objective), h, z


def test_c45_the_fit_matches_dense_reml_gls_and_blup():
    """C45: on 24 runs, the variances, the site effect and the federation effects equal a dense computation at the
    fitted variance ratio (the GLS coefficients and the BLUP lambda Z' H^-1 r), and the restricted objective is no
    better a fifth either side of that ratio, so it is REML's optimum and not ML's or another shrinkage's."""
    rows, placement = simulate(tasks=3, federation_scale=3.0)
    _, fit = adjust(rows, placement, "score")
    assert fit.tau2 > 0
    lam = fit.tau2 / fit.sigma2
    beta, residual, sigma2, objective, h, z = dense_pieces(rows, placement, lam)
    assert fit.sigma2 == pytest.approx(sigma2, rel=1e-6)
    blup = lam * z.T @ np.linalg.solve(h, residual)
    for federation, value in zip(FEDERATIONS, blup, strict=True):
        assert fit.federation_effects[federation] == pytest.approx(value, abs=1e-6)
    assert fit.site_effects["site-b"] - fit.site_effects["site-a"] == pytest.approx(beta[-1], abs=1e-6)
    assert objective <= dense_pieces(rows, placement, lam * 1.2)[3]
    assert objective <= dense_pieces(rows, placement, lam / 1.2)[3]


def test_c45_without_site_or_federation_effects_the_contrast_barely_moves():
    """C45: with pure noise beside the condition effect, the federation variance is small beside the residual one and
    the rerun's contrast estimate stays within 0.05 of the primary's. The site-effect error (about 0.07) times the
    difference in site share between two conditions (about 0.1) is under 0.01, so 0.05 is generous."""
    rows, placement = simulate(site_shift=0.0, federation_scale=0.0)
    _, fit = adjust(rows, placement, "score")
    assert fit.tau2 <= 0.25 * fit.sigma2
    primary = analyse(rows, prereg(), []).contrast("a-vs-b")
    rerun = federation_analysis(rows, [], prereg(), placement).sensitivity.contrast("a-vs-b")
    assert rerun.estimate == pytest.approx(primary.estimate, abs=0.05)


def test_c45_none_outcomes_stay_none_and_are_not_counted():
    """C45: a run whose outcome is None keeps None and is not a fitted run; the others are adjusted."""
    rows, placement = simulate()
    values = scores(rows)
    for index in (3, 17, 40):
        values[index] = None
    adjusted, fit = adjust(with_scores(rows, values), placement, "score")
    assert [index for index, value in enumerate(adjusted) if value is None] == [3, 17, 40]
    assert fit.runs == len(rows) - 3
    assert all(isinstance(value, float) for value in adjusted if value is not None)
    assert adjusted[0] != values[0]


def test_c45_a_run_without_the_outcome_is_not_fitted_and_does_not_stop_the_rerun():
    """C45: a run of a condition outside the contrasts that has no such outcome at all is not counted in the fit, and
    the rerun runs as it does for the primary."""
    rows, placement = simulate()
    bare = make_run("task-0", "m1", 1, "cond-c")
    placement[bare.run_key] = ("site-a", "fed-1")
    analysis = federation_analysis([*rows, bare], [], prereg(), placement)
    assert analysis.fits[0].runs == len(rows)
    assert analysis.sensitivity is not None
    assert adjust([*rows, bare], placement, "score")[0][-1] is None


def test_c45_a_single_task_has_no_task_columns_and_is_fitted():
    """C45: with one task the design is the cells and the site alone, and eight runs are enough to fit it."""
    rows, placement = simulate(tasks=1)
    adjusted, fit = adjust(rows, placement, "score")
    assert (fit.refused, fit.runs) == (None, 8)
    assert all(isinstance(value, float) for value in adjusted)


def test_c45_one_site_is_not_a_refusal():
    """C45: with one site the site effect is 0 and the federation effects are still fitted."""
    rows, placement = simulate()
    one_site = {key: ("site-a", federation) for key, (_, federation) in placement.items()}
    _, fit = adjust(rows, one_site, "score")
    assert fit.refused is None
    assert fit.site_effects == {"site-a": 0.0}
    assert fit.sites == ("site-a",)


def test_c45_a_site_that_coincides_with_one_cell_is_aliased_to_it():
    """C45: when site-b holds exactly the runs of one cell, its effect is not estimable beside the cell mean: the
    site effects are 0 and the cell keeps what the site shifted, so no value is shifted by an arbitrary split."""
    rows, _ = simulate()
    moved, counts = {}, {"site-a": 0, "site-b": 0}
    for row in rows:
        site = "site-b" if (row.condition, row.model) == ("cond-a", "m1") else "site-a"
        federation = FEDERATIONS[counts[site] % 3 + (3 if site == "site-b" else 0)]
        counts[site] += 1
        moved[row.run_key] = (site, federation)
    _, fit = adjust(rows, moved, "score")
    assert fit.refused is None
    assert fit.site_effects == {"site-a": pytest.approx(0.0, abs=1e-12), "site-b": pytest.approx(0.0, abs=1e-12)}


def test_c45_every_federation_holding_one_run_refuses_the_outcome_and_the_rerun():
    """C45: with a federation per run the federation variance is not identifiable: the values come back unchanged with
    the reason, and a rerun with that one outcome has no sensitivity result and says why."""
    rows, _ = simulate()
    alone = {row.run_key: ("site-a" if index % 2 else "site-b", f"solo-{index}") for index, row in enumerate(rows)}
    values, fit = adjust(rows, alone, "score")
    assert "two or more" in fit.refused
    assert values == scores(rows)
    assert (fit.sigma2, fit.tau2, fit.site_effects, fit.federation_effects) == (None, None, {}, {})
    analysis = federation_analysis(rows, [], prereg(), alone)
    assert analysis.sensitivity is None
    assert "score" in analysis.refused
    assert [(f.outcome, f.refused is not None) for f in analysis.fits] == [("score", True)]


def test_c45_one_federation_refuses_the_outcome():
    """C45: all runs in one federation leave its variance confounded with the cell means."""
    rows, _ = simulate()
    single = {row.run_key: ("site-a", "fed-1") for row in rows}
    values, fit = adjust(rows, single, "score")
    assert "confounded" in fit.refused
    assert values == scores(rows)


def test_c45_fewer_than_three_runs_refuse_the_outcome():
    """C45: two fitted runs are too few."""
    rows, placement = simulate()
    values = [value if index < 2 else None for index, value in enumerate(scores(rows))]
    adjusted, fit = adjust(with_scores(rows, values), placement, "score")
    assert (fit.runs, "fewer than 3" in fit.refused) == (2, True)
    assert adjusted == values


def test_c45_a_constant_outcome_is_refused_for_having_no_variation():
    """C45: nothing can be fitted to a column that does not vary, and nothing needs adjusting."""
    rows, placement = simulate()
    adjusted, fit = adjust(with_scores(rows, [1.0] * len(rows)), placement, "score")
    assert "no variation" in fit.refused
    assert adjusted == [1.0] * len(rows)


def test_c45_an_outcome_the_fixed_effects_explain_exactly_is_refused():
    """C45: with no noise and no site or federation effect the fixed part fits every run, no residual variation is
    left to estimate variances from, and the values come back unchanged."""
    rows, placement = simulate(noise=0.0, site_shift=0.0, federation_scale=0.0)
    adjusted, fit = adjust(rows, placement, "score")
    assert "no residual variation" in fit.refused
    assert adjusted == scores(rows)


def test_c45_a_design_with_no_residual_degrees_of_freedom_is_refused():
    """C45: four runs in four cells with two federations fit exactly, so the variances cannot be estimated."""
    rows = [make_run("task-0", model, 1, "cond-a", score=float(index)) for index, model in enumerate("wxyz")]
    placement = {row.run_key: ("site-a", "fed-1" if index < 2 else "fed-2") for index, row in enumerate(rows)}
    _, fit = adjust(rows, placement, "score")
    assert "no residual degrees of freedom" in fit.refused


def test_c45_an_outcome_read_by_a_ratio_contrast_is_not_adjusted_and_is_listed():
    """C45: spend is the outcome of a contrast of one outcome and the numerator of a ratio contrast: it stays as it
    is, with the reason, and only the score is fitted."""
    rows, placement = simulate()
    analysis = federation_analysis(rows, [], prereg(RATIO_TEXT), placement)
    assert analysis.unadjusted == (("spend", READ_BY_RATIO),)
    assert [fit.outcome for fit in analysis.fits] == ["score"]


def test_c45_ratio_contrasts_and_their_columns_keep_their_primary_results_in_the_rerun():
    """C45: the rerun of the ratio contrast, and of the one-outcome contrast on its numerator, equals the primary's."""
    rows, placement = simulate()
    rerun = federation_analysis(rows, [], prereg(RATIO_TEXT), placement).sensitivity
    primary = analyse(rows, prereg(RATIO_TEXT), [])
    for name in ("spend-per-win", "spend-diff"):
        a, b = rerun.contrast(name), primary.contrast(name)
        assert (a.estimate, a.ci_low, a.ci_high, a.p_value, a.n_pairs) == (
            b.estimate,
            b.ci_low,
            b.ci_high,
            b.p_value,
            b.n_pairs,
        )


def test_c45_with_only_ratio_columns_there_is_no_rerun():
    """C45: no outcome is left to adjust, so there is no sensitivity result, and the reason is given."""
    rows, placement = simulate()
    analysis = federation_analysis(rows, [], prereg(RATIO_ONLY_TEXT), placement)
    assert analysis.fits == ()
    assert analysis.unadjusted == (("spend", READ_BY_RATIO),)
    assert analysis.sensitivity is None
    assert "no contrast of one outcome" in analysis.refused


def test_c45_the_rerun_has_the_primary_contrasts_and_holm_members():
    """C45: the rerun's contrasts, in the primary's order, and the members of its Holm decisions are the primary's."""
    rows, placement = simulate()
    rerun = federation_analysis(rows, [], prereg(RATIO_TEXT), placement).sensitivity
    primary = analyse(rows, prereg(RATIO_TEXT), [])
    assert [c.name for c in rerun.contrasts] == [c.name for c in primary.contrasts]
    assert [d.name for d in rerun.holm] == [d.name for d in primary.holm]


def test_c45_the_inputs_are_not_changed():
    """C45: the rows, the placement and the preregistration are read only."""
    rows, placement = simulate()
    before = copy.deepcopy((rows, placement))
    federation_analysis(rows, [], prereg(), placement)
    adjust(rows, placement, "score")
    assert (rows, placement) == before


def test_c45_the_same_input_gives_the_same_adjustment():
    """C45: the fit has no random part: two calls give identical values and an identical fit."""
    rows, placement = simulate()
    assert adjust(rows, placement, "score") == adjust(rows, placement, "score")


def test_c45_a_federation_at_two_sites_is_a_caller_error():
    """C45: a federation is nested in one site; one placed at two raises, naming it, from both entry points."""
    rows, placement = simulate()
    placement[rows[0].run_key] = (
        "site-b" if placement[rows[0].run_key][0] == "site-a" else "site-a",
        placement[rows[0].run_key][1],
    )
    federation = placement[rows[0].run_key][1]
    with pytest.raises(ValueError, match=re.escape(f"federation {federation!r} is placed at two sites")):
        adjust(rows, placement, "score")
    with pytest.raises(ValueError, match=re.escape(f"federation {federation!r} is placed at two sites")):
        federation_analysis(rows, [], prereg(), placement)


def test_c45_a_run_missing_from_placement_is_a_caller_error():
    """C45: a run with no placement raises, naming its run key, from both entry points."""
    rows, placement = simulate()
    missing = rows[5].run_key
    del placement[missing]
    with pytest.raises(ValueError, match=re.escape(missing)):
        adjust(rows, placement, "score")
    with pytest.raises(ValueError, match=re.escape(missing)):
        federation_analysis(rows, [], prereg(), placement)
