"""P05: the known-answer test of the inference code, and the proof that it notices a change."""

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
    assert len([p for p in problems if "raised RuntimeError" in p]) == len(kat.WILCOXON_CASES)
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
