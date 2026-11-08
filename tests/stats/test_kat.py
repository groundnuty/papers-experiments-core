"""P05: the known-answer test of the inference code, and the proof that it notices a change."""

import dataclasses
import inspect
import itertools
import math
import types

import numpy as np
import pytest

from expcore.stats import contrasts, estimators, kat


def exact_signed_rank_p(differences):
    """Two-sided exact signed-rank p-value by listing every sign pattern, with no inference code involved."""
    d = np.array([x for x in differences if x != 0.0])
    ranks = np.empty(len(d))
    order = np.argsort(np.abs(d), kind="stable")
    sorted_abs = np.abs(d)[order]
    position = 0
    while position < len(d):
        end = position
        while end + 1 < len(d) and sorted_abs[end + 1] == sorted_abs[position]:
            end += 1
        ranks[order[position : end + 1]] = (position + end) / 2 + 1  # the mid-rank of a tie group
        position = end + 1
    observed = min(ranks[d > 0].sum(), ranks[d < 0].sum())
    total = ranks.sum()
    extreme = sum(
        1
        for signs in itertools.product((0, 1), repeat=len(d))
        if min(np.dot(signs, ranks), total - np.dot(signs, ranks)) <= observed + 1e-12
    )
    return extreme / 2 ** len(d)


def test_p05_the_inference_code_gives_every_known_answer():
    """P05: all estimator cases and all Holm cases hold on the code as it is."""
    assert kat.run() == []
    kat.verify()


def test_p05_every_registered_estimator_has_a_case_and_the_cases_name_no_other():
    """P05: coverage is by registry, so an estimator cannot be added without a known answer."""
    assert set(kat.CASES) == set(estimators.names())
    assert all(kat.CASES[name] for name in estimators.names())


def test_p05_a_new_estimator_without_a_case_fails_the_test(monkeypatch):
    """P05: registering an estimator adds a demand for its known answer."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    estimators.register("untested")(lambda paired, settings: None)
    assert kat.run() == ["estimator 'untested' is registered but has no known-answer case"]
    with pytest.raises(kat.KnownAnswerError, match="estimator 'untested' is registered but has no known-answer case"):
        kat.verify()


REAL_HOLM = contrasts.holm


def strict_holm(p_values, alpha):
    """Holm as it would read with a strict comparison of the adjusted p-value, the drift to catch at the boundary."""
    real = REAL_HOLM(p_values, alpha)
    reject = tuple(a < alpha for a in real.adjusted)
    return contrasts.HolmResult(real.adjusted, real.levels, reject)


@pytest.mark.parametrize(
    ("attribute", "replacement", "fragment"),
    [
        ("wilcoxon_p", lambda differences, decimals=12, method="auto": 0.5, "p-value"),
        ("zeros_dropped", lambda differences, decimals=12: 0, "counts"),
        ("cluster_means", lambda matrix: np.nanmax(matrix, axis=-1), "estimate"),
        ("snap", lambda values, decimals=12: np.round(np.asarray(values, dtype=float), 0) + 0.0, "p-value"),
        ("level", lambda alpha, weight, total: alpha * weight / (total + 1), "level of hypothesis"),
        ("holm", strict_holm, "decision for hypothesis"),
    ],
)
def test_p05_changing_the_inference_code_fails_the_known_answers(monkeypatch, attribute, replacement, fragment):
    """P05: an edit to the kernel that moves a result on the known inputs is reported, with the case that moved."""
    monkeypatch.setattr(contrasts, attribute, replacement)
    problems = kat.run()
    assert problems, f"the known answers did not notice a changed {attribute}"
    assert any(fragment in problem for problem in problems)
    with pytest.raises(kat.KnownAnswerError):
        kat.verify()


def test_p05_a_changed_dependency_fails_the_known_answers(monkeypatch):
    """P05: a different scipy behind the rank test shows up too."""
    monkeypatch.setattr(contrasts.stats, "wilcoxon", lambda *a, **k: types.SimpleNamespace(pvalue=0.5))
    assert any("p-value" in problem for problem in kat.run())


def test_p05_a_strict_comparison_at_the_boundary_is_caught_by_the_boundary_cases_alone(monkeypatch):
    """P05, S06: only the cases whose p-value sits on its level distinguish `<=` on the level from a strict test."""
    monkeypatch.setattr(contrasts, "holm", strict_holm)
    cases = {problem.split(":")[0] for problem in kat.run()}
    assert cases == {
        "holm / a p-value on its level is rejected",
        "holm / a p-value on its level in a family of eleven",
    }


def test_p05_an_estimator_that_raises_is_reported_with_its_case_and_the_others_still_run(monkeypatch):
    """P05: a crash is drift, named by case, and does not hide the cases after it."""

    def broken(paired, settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    monkeypatch.setitem(estimators._REGISTRY, estimators.WILCOXON, estimators._Entry(broken, lambda params: None))
    problems = kat.run()
    assert any("raised RuntimeError: boom" in p for p in problems)
    crashed = [p for p in problems if "raised RuntimeError" in p]
    assert len(crashed) == len(kat.WILCOXON_CASES) + len(kat.INTERVAL_CASES[estimators.WILCOXON])
    assert not any(p.startswith("holm") for p in problems)


def test_p05_the_hand_computed_exact_p_values_agree_with_a_listing_of_every_sign_pattern():
    """P05: the expected values are the combinatorial answers, not what the code returned the first time."""
    exact_cases = [case for case in kat.WILCOXON_CASES if case.params.get("method") == "exact"]
    assert len(exact_cases) >= 5
    for case in exact_cases:
        matrix = np.array(case.matrix, dtype=float)
        defined = ~np.isnan(matrix)
        differences = np.where(defined, matrix, 0.0).sum(axis=1) / defined.sum(axis=1)
        differences = [round(float(x), 12) for x in differences]
        assert exact_signed_rank_p(differences) == pytest.approx(case.p_value, abs=1e-12), case.name


def test_p05_the_normal_approximation_case_is_the_closed_form():
    """P05: z = (10 - 5) / 2.5 = 2, so p = 2 * (1 - Phi(2)) = erfc(2 / sqrt 2)."""
    case = next(c for c in kat.WILCOXON_CASES if c.params.get("method") == "approx")
    assert case.p_value == pytest.approx(math.erfc(2 / math.sqrt(2)), abs=1e-15)


def collapsing(estimator):
    """Wrap an estimator so that its interval is the point estimate: the fault the hand-worked answers cannot see."""

    def wrapped(paired, settings):
        result = estimator(paired, settings)
        return dataclasses.replace(result, ci_low=result.estimate, ci_high=result.estimate)

    return wrapped


def wrapping_the_registered_estimator(monkeypatch, wrapper):
    """Replace the registered Wilcoxon estimator by `wrapper(real estimator)`, in a copy of the registry."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    entry = estimators._REGISTRY[estimators.WILCOXON]
    monkeypatch.setitem(
        estimators._REGISTRY, estimators.WILCOXON, dataclasses.replace(entry, function=wrapper(entry.function))
    )


def case_names(problems):
    """The case labels of a list of problems."""
    return {problem.split(":")[0] for problem in problems}


VARYING_CASES = {
    f"wilcoxon_cluster_bootstrap / {name}"
    for name in (
        "five positive differences",
        "zero dropped",
        "one negative difference",
        "ties",
        "two models, one cell missing",
        "twelve clusters, one negative",
        "nine clusters, two models, gaps",
    )
}


def test_p05_where_the_differences_vary_the_interval_has_width_and_encloses_the_estimate():
    """P05: ci_low < ci_high and ci_low <= estimate <= ci_high where differences vary, a point where they are equal."""
    for case in kat.WILCOXON_CASES:
        matrix = np.array(case.matrix, dtype=float)
        paired = estimators.PairedDifferences(
            tuple(f"grp-{i}" for i in range(len(matrix))), tuple(f"model-{j}" for j in range(matrix.shape[1])), matrix
        )
        result = estimators.get(estimators.WILCOXON)(
            paired, estimators.EstimatorSettings(200, 0.95, (1, 2), case.params)
        )
        spread = contrasts.cluster_means(matrix)
        if np.nanmax(spread) > np.nanmin(spread):
            assert result.ci_low < result.ci_high, case.name
            assert result.ci_low <= result.estimate <= result.ci_high, case.name
        else:
            assert result.ci_low == result.estimate == result.ci_high, case.name


def test_p05_an_estimator_whose_interval_collapses_onto_its_estimate_fails_the_known_answers(monkeypatch):
    """P05: a wrapper that sets ci_low = ci_high = estimate is reported, case by case, and verify() raises."""
    assert kat.run() == []
    wrapping_the_registered_estimator(monkeypatch, collapsing)
    problems = kat.run()
    assert problems
    assert case_names(problems) >= VARYING_CASES
    assert any("has no width" in problem for problem in problems)
    assert any("frozen interval" in problem for problem in problems)
    with pytest.raises(kat.KnownAnswerError, match="has no width"):
        kat.verify()


NINE_ZEROS_AND_A_ONE = kat.IntervalCase(
    "nine zeros and a one",
    ((0.0,),) * 9 + ((1.0,),),
    {},
    replicates=400,
    level=0.4,
    seed=(7, 11),
    interval=(0.0, 0.1),
    model_intervals=((0.0, 0.1),),
)


def test_p05_a_percentile_interval_that_ends_on_the_estimate_is_legitimate_and_passes(monkeypatch):
    """P05: the estimate is 0.1 and the 40% interval is 0 to 0.1; enclosure includes its ends, width is positive."""
    monkeypatch.setattr(kat, "INTERVAL_CASES", {estimators.WILCOXON: (NINE_ZEROS_AND_A_ONE,)})
    assert kat.run() == []
    case = NINE_ZEROS_AND_A_ONE
    paired = estimators.PairedDifferences(
        tuple(f"grp-{i}" for i in range(10)), ("model-a",), np.array(case.matrix, dtype=float)
    )
    result = estimators.get(estimators.WILCOXON)(paired, estimators.EstimatorSettings(400, 0.4, (7, 11), {}))
    assert (result.ci_low, result.estimate, result.ci_high) == pytest.approx((0.0, 0.1, 0.1))
    assert result.ci_high == pytest.approx(result.estimate)  # the premise: the interval ends on the estimate


def test_p05_the_collapse_is_still_caught_on_the_input_where_the_interval_ends_on_the_estimate(monkeypatch):
    """P05: relaxing the enclosure to closed does not let ci_low = ci_high = estimate through."""
    monkeypatch.setattr(kat, "INTERVAL_CASES", {estimators.WILCOXON: (NINE_ZEROS_AND_A_ONE,)})
    wrapping_the_registered_estimator(monkeypatch, collapsing)
    problems = [p for p in kat.run() if p.startswith("wilcoxon_cluster_bootstrap / nine zeros and a one")]
    assert any("has no width" in problem for problem in problems)


def test_p05_an_interval_that_misses_the_estimate_is_reported_even_with_width(monkeypatch):
    """P05: closed enclosure still excludes an interval that lies to one side of the estimate."""

    def shifted(estimator):
        def wrapped(paired, settings):
            result = estimator(paired, settings)
            return dataclasses.replace(result, ci_low=result.estimate + 1.0, ci_high=result.estimate + 2.0)

        return wrapped

    monkeypatch.setattr(kat, "INTERVAL_CASES", {})
    wrapping_the_registered_estimator(monkeypatch, shifted)
    assert any("does not enclose the estimate" in problem for problem in kat.run())


def test_p05_the_width_rule_alone_catches_the_collapse_in_every_case_with_varying_differences(monkeypatch):
    """P05: without the frozen endpoints the property still fails the wrapper, and only where differences vary."""
    monkeypatch.setattr(kat, "INTERVAL_CASES", {})
    wrapping_the_registered_estimator(monkeypatch, collapsing)
    problems = kat.run()
    assert case_names(problems) == {
        "wilcoxon_cluster_bootstrap / five positive differences",
        "wilcoxon_cluster_bootstrap / zero dropped",
        "wilcoxon_cluster_bootstrap / one negative difference",
        "wilcoxon_cluster_bootstrap / ties",
        "wilcoxon_cluster_bootstrap / two models, one cell missing",
    }
    assert all("has no width" in problem for problem in problems)


def test_p05_a_change_of_the_seeded_bootstrap_fails_the_frozen_endpoints_and_nothing_else(monkeypatch):
    """P05: a bootstrap drawing other clusters keeps its width and encloses the estimate; the frozen values see it."""
    monkeypatch.setattr(
        contrasts,
        "draw_clusters",
        lambda rng, replicates, n: np.random.default_rng(99).integers(0, n, size=(replicates, n)),
    )
    problems = kat.run()
    assert case_names(problems) == {
        "wilcoxon_cluster_bootstrap / twelve clusters, one negative",
        "wilcoxon_cluster_bootstrap / nine clusters, two models, gaps",
    }
    assert all("frozen interval" in problem for problem in problems)
    with pytest.raises(kat.KnownAnswerError, match="frozen interval"):
        kat.verify()


def narrowing(estimator):
    """Wrap an estimator so that its interval keeps its centre and loses a hundredth of its width on each side."""

    def wrapped(paired, settings):
        result = estimator(paired, settings)
        shrink = 0.01 * (result.ci_high - result.ci_low)
        return dataclasses.replace(result, ci_low=result.ci_low + shrink, ci_high=result.ci_high - shrink)

    return wrapped


def test_p05_a_slightly_narrower_interval_is_caught_by_the_frozen_endpoints(monkeypatch):
    """P05: the endpoints are compared to 1e-12, so a one percent change of the interval is a failure."""
    wrapping_the_registered_estimator(monkeypatch, narrowing)
    assert case_names(kat.run()) == {
        "wilcoxon_cluster_bootstrap / twelve clusters, one negative",
        "wilcoxon_cluster_bootstrap / nine clusters, two models, gaps",
    }


def test_p05_the_frozen_intervals_are_two_inputs_with_a_comment_on_how_they_were_made():
    """P05: the frozen cases are the two named inputs, and the source says they came from the reviewed code."""
    assert [case.name for case in kat.INTERVAL_CASES[estimators.WILCOXON]] == [
        "twelve clusters, one negative",
        "nine clusters, two models, gaps",
    ]
    source = inspect.getsource(kat)
    assert "Frozen from the reviewed implementation, not derived by hand" in source
