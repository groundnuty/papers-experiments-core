# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/analysis/kat.py
"""Known answers of the inference code (P05): fixed inputs whose outputs were worked out by hand.

A change to the Wilcoxon kernel, the bootstrap, Holm or a dependency that moves what they compute on these inputs
fails `verify()`. A study calls it before it reads any data, and the test suite calls it on every run. It detects drift
in what the code does on known inputs; it does not show the code right on other inputs, which the unit tests and the
code stamp cover.

Every expected value below has its derivation beside it. The intervals of a seeded bootstrap are not part of the
answers, because they depend on the generator's stream and not on the inference: where an interval is known by hand
(equal differences give a point interval) it is checked, and otherwise it is only required to lie between the smallest
and the largest cluster difference.

Every registered estimator needs a case; `run()` reports one that has none, so a new estimator cannot be added without
a known answer.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from expcore.stats import contrasts, estimators
from expcore.stats.estimators import EstimatorSettings, PairedDifferences

NAN = math.nan
TOLERANCE = 1e-12


@dataclass(frozen=True)
class EstimatorCase:
    """One estimator input with the answers worked out by hand."""

    name: str
    matrix: tuple[tuple[float, ...], ...]
    params: Mapping[str, Any]
    estimate: float
    p_value: float
    n_clusters: int
    counts: Mapping[str, int]
    model_estimates: tuple[float, ...]
    interval: tuple[float, float] | None = None  # exact when known by hand, else only required to enclose the estimate
    why: str = field(default="", compare=False)


@dataclass(frozen=True)
class HolmCase:
    """One Holm family with its adjusted p-values, levels and decisions worked out by hand."""

    name: str
    p_values: tuple[float, ...]
    alpha: float
    adjusted: tuple[float, ...]
    levels: tuple[float, ...]
    reject: tuple[bool, ...]
    why: str = field(default="", compare=False)


WILCOXON_CASES = (
    EstimatorCase(
        "five positive differences",
        ((0.1,), (0.2,), (0.3,), (0.4,), (0.5,)),
        {"method": "exact"},
        estimate=0.3,
        p_value=0.0625,
        n_clusters=5,
        counts={"zero_dropped": 0},
        model_estimates=(0.3,),
        why="mean (0.1+...+0.5)/5; ranks 1..5 all positive, W = 0, p = 2 * (1 / 2**5)",
    ),
    EstimatorCase(
        "zero dropped",
        ((0.0,), (0.1,), (0.2,), (0.3,), (0.4,), (0.5,), (0.6,)),
        {"method": "exact"},
        estimate=0.3,
        p_value=2 / 64,
        n_clusters=7,
        counts={"zero_dropped": 1},
        model_estimates=(0.3,),
        why="mean 2.1/7; the zero is dropped, six positive ranks give p = 2 * (1 / 2**6)",
    ),
    EstimatorCase(
        "one negative difference",
        ((0.1,), (0.2,), (0.3,), (0.4,), (0.5,), (-0.6,)),
        {"method": "exact"},
        estimate=0.15,
        p_value=0.4375,
        n_clusters=6,
        counts={"zero_dropped": 0},
        model_estimates=(0.15,),
        why="mean 0.9/6; W- = 6, 14 of the 64 sign patterns have a rank sum of at most 6, p = 2 * 14 / 64",
    ),
    EstimatorCase(
        "ties",
        ((0.2,), (0.2,), (0.4,)),
        {"method": "exact"},
        estimate=0.8 / 3,
        p_value=0.25,
        n_clusters=3,
        counts={"zero_dropped": 0},
        model_estimates=(0.8 / 3,),
        why="ranks 1.5, 1.5, 3; only the all-positive and all-negative patterns reach W = 0, p = 2 * (1 / 2**3)",
    ),
    EstimatorCase(
        "equal differences, exact",
        ((0.2,),) * 4,
        {"method": "exact"},
        estimate=0.2,
        p_value=0.125,
        n_clusters=4,
        counts={"zero_dropped": 0},
        model_estimates=(0.2,),
        interval=(0.2, 0.2),
        why="every resample mean is 0.2; all sign patterns give W = 0 or 10, p = 2 * (1 / 2**4)",
    ),
    EstimatorCase(
        "equal differences, normal approximation",
        ((0.2,),) * 4,
        {"method": "approx"},
        estimate=0.2,
        p_value=0.04550026389635842,
        n_clusters=4,
        counts={"zero_dropped": 0},
        model_estimates=(0.2,),
        interval=(0.2, 0.2),
        why="one tie group of 4: mean 5, sd sqrt(7.5 - 1.25) = 2.5, W = 10, z = 2, p = 2 * (1 - Phi(2))",
    ),
    EstimatorCase(
        "all zero",
        ((0.0,), (0.0,), (0.0,)),
        {},
        estimate=0.0,
        p_value=1.0,
        n_clusters=3,
        counts={"zero_dropped": 3},
        model_estimates=(0.0,),
        interval=(0.0, 0.0),
        why="no evidence of a difference: p = 1 by definition, every zero counted",
    ),
    EstimatorCase(
        "two models, one cell missing",
        ((0.1, 0.3), (0.2, NAN), (0.3, 0.5)),
        {"method": "exact"},
        estimate=0.8 / 3,
        p_value=0.25,
        n_clusters=3,
        counts={"zero_dropped": 0},
        model_estimates=(0.2, 0.4),
        why="cluster differences 0.2, 0.2, 0.4 (equal weight over the models with a cell); models 0.2 and 0.4",
    ),
)
CASES: Mapping[str, tuple[EstimatorCase, ...]] = {estimators.WILCOXON: WILCOXON_CASES}

HOLM_CASES = (
    HolmCase(
        "step-down stops at the first failure",
        (0.01, 0.04, 0.03),
        0.05,
        adjusted=(0.03, 0.06, 0.06),
        levels=(0.05 / 3, 0.05 / 1, 0.05 / 2),
        reject=(True, False, False),
        why=(
            "sorted 0.01, 0.03, 0.04 at levels alpha/3, alpha/2, alpha/1: 0.03 > 0.025 stops; "
            "adjusted 3 * 0.01, 2 * 0.03, then the running maximum"
        ),
    ),
    HolmCase(
        "a missing p-value stays in the family",
        (0.001, NAN, 0.02),
        0.05,
        adjusted=(0.003, NAN, 0.04),
        levels=(0.05 / 3, 0.05 / 1, 0.05 / 2),
        reject=(True, False, True),
        why=(
            "the NaN counts as 1 and is ranked last, so m = 3: 0.001 <= alpha/3 and 0.02 <= alpha/2; "
            "adjusted 3 * 0.001, 2 * 0.02"
        ),
    ),
    HolmCase(
        "a p-value on its level is rejected",
        (0.025, 0.9),
        0.05,
        adjusted=(0.05, 0.9),
        levels=(0.025, 0.05),
        reject=(True, False),
        why="0.025 = alpha/2 exactly, rejected as declared (<=)",
    ),
    HolmCase(
        "a p-value on its level in a family of eleven",
        (0.004545454545454546, *([1.0] * 10)),
        0.05,
        adjusted=(0.05000000000000001, *([1.0] * 10)),
        levels=(0.004545454545454546, *(0.05 / k for k in range(10, 0, -1))),
        reject=(True, *([False] * 10)),
        why=(
            "0.05 * 1 / 11 compared with itself is rejected; 11 * it lands one rounding above 0.05, "
            "which is why the adjusted p-value does not decide"
        ),
    ),
)


def _same(expected: float, got: float) -> bool:
    """Equal within the tolerance; two NaNs are equal here."""
    if math.isnan(expected) or math.isnan(got):
        return math.isnan(expected) and math.isnan(got)
    return abs(expected - got) <= TOLERANCE * max(1.0, abs(expected))


def _compare(case: str, label: str, expected: Any, got: Any, problems: list[str]) -> None:
    """Append a problem when a value differs from the known answer."""
    if isinstance(expected, int | float) and not isinstance(expected, bool) and isinstance(got, int | float):
        ok = _same(float(expected), float(got))
    else:
        ok = expected == got
    if not ok:
        problems.append(f"{case}: {label} is {got!r}, the known answer is {expected!r}")


def _check_estimator(name: str, case: EstimatorCase, problems: list[str]) -> None:
    """Run one estimator case and compare every output with its known answer."""
    label = f"{name} / {case.name}"
    matrix = np.array(case.matrix, dtype=float)
    paired = PairedDifferences(
        tuple(f"c{i + 1}" for i in range(matrix.shape[0])), tuple(f"m{j + 1}" for j in range(matrix.shape[1])), matrix
    )
    settings = EstimatorSettings(replicates=200, level=0.95, seed=(1, 2), params=case.params)
    result = estimators.get(name)(paired, settings)
    _compare(label, "estimate", case.estimate, result.estimate, problems)
    _compare(label, "p-value", case.p_value, result.p_value, problems)
    _compare(label, "cluster count", case.n_clusters, result.n_clusters, problems)
    _compare(label, "counts", dict(case.counts), dict(result.counts), problems)
    _compare(label, "per-model estimates", len(case.model_estimates), len(result.per_model), problems)
    for expected, model in zip(case.model_estimates, result.per_model, strict=False):
        _compare(label, f"estimate of {model.model}", expected, model.estimate, problems)
    if case.interval is not None:
        _compare(label, "interval low", case.interval[0], result.ci_low, problems)
        _compare(label, "interval high", case.interval[1], result.ci_high, problems)
    else:
        spread = contrasts.cluster_means(matrix)
        if not (
            np.nanmin(spread) - TOLERANCE
            <= result.ci_low
            <= result.estimate
            <= result.ci_high
            <= np.nanmax(spread) + TOLERANCE
        ):
            problems.append(
                f"{label}: the interval {result.ci_low!r}..{result.ci_high!r} does not enclose the estimate"
            )


def _check_holm(case: HolmCase, problems: list[str]) -> None:
    """Run one Holm case and compare the adjusted p-values, the levels and the decisions."""
    label = f"holm / {case.name}"
    result = contrasts.holm(case.p_values, case.alpha)
    for index, (adjusted, level, reject) in enumerate(zip(case.adjusted, case.levels, case.reject, strict=True)):
        _compare(label, f"adjusted p of hypothesis {index}", adjusted, result.adjusted[index], problems)
        _compare(label, f"level of hypothesis {index}", level, result.levels[index], problems)
        _compare(label, f"decision for hypothesis {index}", reject, result.reject[index], problems)


def run() -> list[str]:
    """Run every known answer; return the problems found, empty when the inference code gives all of them."""
    problems: list[str] = []
    for name in estimators.names():
        if name not in CASES:
            problems.append(f"estimator {name!r} is registered but has no known-answer case")
            continue
        for case in CASES[name]:
            _guard(
                lambda case=case, name=name: _check_estimator(name, case, problems), f"{name} / {case.name}", problems
            )
    for holm_case in HOLM_CASES:
        _guard(lambda holm_case=holm_case: _check_holm(holm_case, problems), f"holm / {holm_case.name}", problems)
    return problems


def _guard(check: Callable[[], None], label: str, problems: list[str]) -> None:
    """Run one check; a raised error is a problem of that case, not a reason to skip the others."""
    try:
        check()
    except Exception as error:  # any failure of the inference code is drift to report, with its type
        problems.append(f"{label}: raised {type(error).__name__}: {error}")


class KnownAnswerError(RuntimeError):
    """The inference code no longer gives a known answer."""


def verify() -> None:
    """Raise KnownAnswerError listing every difference, or return when the code gives all the known answers."""
    problems = run()
    if problems:
        raise KnownAnswerError("known-answer test failed:\n  " + "\n  ".join(problems))
