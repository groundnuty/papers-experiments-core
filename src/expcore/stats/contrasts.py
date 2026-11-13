# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/contrasts.py, blob 075305b68276374c1f62e3439eb94b4979491f68
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: www-2027-experiments @ 46a5828d, harness/analysis/stats.py:7-13, 43
#   merged: www-2027-experiments @ 46a5828d, harness/analysis/multiplicity.py:86-88
#   changes: see git history of this file from its import commit
"""Inference kernel for paired cluster contrasts: the snapped Wilcoxon test, the cluster bootstrap and Holm.

The kernel works on a matrix of paired differences, one row per cluster and one column per model, NaN where a cell
has no pair. The cluster is the unit of inference. Pairing and aggregation happen before the kernel (see pairing);
the registered estimators and the declared contrasts sit above it (see estimators and analysis). Nothing here knows
an outcome, a factor or a design.

Rules kept here, each with a regression test:
- S01: float noise is snapped before the rank test, so differences equal in arithmetic tie.
- S02, S06: Holm keeps the declared family, and its decisions compare the raw p-value with an exact level.
- S03: an empty or all-zero set of differences has defined values, and dropped zeros are counted.
- S04: one cluster resample is shared by all models.
- S05: an undefined resample is NaN, never 0, and never supports a hypothesis.
- S15: an equivalence claim is Schuirmann's two one-sided t-tests of the cluster differences, dual to a 1 - 2L
  interval; fewer than two clusters, or no spread between them, gives NaN, never evidence of equivalence.
"""

from __future__ import annotations

import dataclasses
import functools
import math
from collections.abc import Sequence

import numpy as np
from scipy import stats

from expcore.stats.rejection import rejects

DEFAULT_SNAP_DECIMALS = 12
# A draw within this distance of the boundary is a tie and does not support the alternative; it only absorbs
# floating-point rounding (0.53 - 0.50 is 0.0300...03).
TIE_TOLERANCE = 1e-9
# An interval needs at least this share of its replicates defined; the same rule serves every bootstrap interval.
MIN_DEFINED_SHARE = 0.5
WILCOXON_METHODS = ("auto", "exact", "approx")


def snap(values, decimals: int = DEFAULT_SNAP_DECIMALS) -> np.ndarray:
    """Round to `decimals` places so that float noise cannot split values that are equal in arithmetic (S01).

    The grid is far below the data's own grid, so it merges the noise a chain of means leaves (about 1e-16
    relative: 0.1 + 0.2 against 0.3) and never two values that really differ. A dry run on real data moved a
    p-value from 1.12e-5 to 6.09e-6 when equal differences ranked apart. -0.0 becomes 0.0.
    """
    return np.round(np.asarray(values, dtype=float), decimals) + 0.0


def _masked_mean(matrix: np.ndarray, axis: int) -> np.ndarray:
    """Mean over the defined cells along an axis; NaN, never 0, where no cell is defined (S05)."""
    defined = ~np.isnan(matrix)
    total = np.where(defined, matrix, 0.0).sum(axis=axis)
    count = defined.sum(axis=axis)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / count, np.nan)


def cluster_means(matrix: np.ndarray) -> np.ndarray:
    """Cluster-level differences: the equal-weight mean over the models with a paired cell. Shape (..., R, M) to (..., R)."""
    return _masked_mean(np.asarray(matrix, dtype=float), axis=-1)


def model_means(matrix: np.ndarray) -> np.ndarray:
    """Per-model differences: the mean over the clusters with a paired cell. Shape (..., R, M) to (..., M)."""
    return _masked_mean(np.asarray(matrix, dtype=float), axis=-2)


@dataclasses.dataclass(frozen=True)
class Bootstrap:
    """The point values and every replicate of a cluster bootstrap, with the cluster draws that produced them."""

    point: float
    point_per_model: np.ndarray  # (M,)
    overall: np.ndarray  # (B,) mean over the drawn clusters of their cluster-level difference
    per_model: np.ndarray  # (B, M) NaN where a model has no defined cell among the drawn clusters
    indices: np.ndarray  # (B, R) the drawn cluster rows, one draw per replicate shared by all models


def draw_clusters(rng: np.random.Generator, replicates: int, n_clusters: int) -> np.ndarray:
    """Draw `replicates` samples of `n_clusters` cluster rows with replacement."""
    return rng.integers(0, n_clusters, size=(replicates, n_clusters))


def resample(matrix: np.ndarray, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (overall, per-model) statistics of every replicate for the given cluster draws.

    The same draw indexes the rows of every model's column, so a replicate never mixes two resamples of the clusters
    (S04). A model with no defined cell among the drawn clusters is NaN in that replicate (S05).
    """
    matrix = np.asarray(matrix, dtype=float)
    return cluster_means(matrix)[indices].mean(axis=1), model_means(matrix[indices])


def cluster_bootstrap(matrix: np.ndarray, replicates: int, rng: np.random.Generator) -> Bootstrap:
    """Cluster bootstrap of the matrix: one resample of the clusters, shared by all models (S04).

    The overall statistic is the mean over clusters of the cluster-level difference, exactly the quantity the rank
    test sees. A per-model statistic is the mean over clusters of that model's column.
    """
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError("the bootstrap needs a matrix with at least one cluster row")
    if replicates < 1:
        raise ValueError(f"the bootstrap needs at least one replicate, got {replicates}")
    if np.isnan(matrix).all(axis=1).any():
        raise ValueError("a cluster with no paired cell cannot be resampled; drop it before the bootstrap")
    indices = draw_clusters(rng, replicates, matrix.shape[0])
    overall, per_model = resample(matrix, indices)
    return Bootstrap(float(cluster_means(matrix).mean()), model_means(matrix), overall, per_model, indices)


def percentile_interval(draws, level: float) -> tuple[float, float, int]:
    """Return (low, high, defined): the percentile interval over the defined replicates.

    Undefined replicates (NaN) are left out and counted by the caller as `len(draws) - defined`. The interval is NaN
    when fewer than half the replicates are defined (S05); the same rule gives the kappa interval.
    """
    draws = np.asarray(draws, dtype=float)
    defined = draws[~np.isnan(draws)]
    if defined.size == 0 or defined.size < draws.size * MIN_DEFINED_SHARE:
        return math.nan, math.nan, int(defined.size)
    low, high = np.percentile(defined, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return float(low), float(high), int(defined.size)


def supports(draws, theta0: float = 0.0, tolerance: float = TIE_TOLERANCE) -> np.ndarray:
    """Tell which resampled values support theta > theta0.

    A value within the tolerance of the boundary is a tie, and an undefined value (NaN) never supports (S05).
    """
    values = np.asarray(draws, dtype=float)
    with np.errstate(invalid="ignore"):
        return values > theta0 + tolerance


def one_sided_bootstrap_p(draws, theta0: float = 0.0, tolerance: float = TIE_TOLERANCE) -> float:
    """One-sided bootstrap p-value for theta > theta0: (1 + number of draws not supporting) / (B + 1).

    Ties and undefined draws count as not supporting, so a replicate that could not be computed can only raise p.
    """
    values = np.asarray(draws, dtype=float)
    if values.size == 0:
        return math.nan
    return float((1 + values.size - supports(values, theta0, tolerance).sum()) / (values.size + 1))


def _check_margin(margin: float) -> None:
    """Refuse a margin that is not a positive finite number (a boolean is not a number here)."""
    number = isinstance(margin, int | float) and not isinstance(margin, bool)
    if not (number and math.isfinite(margin) and margin > 0):
        raise ValueError(f"an equivalence margin must be a positive finite number, got {margin!r}")


def _mean_and_se(studies: np.ndarray, decimals: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Per row of snapped cluster differences: the mean and its standard error, NaN where there is no spread."""
    snapped = _snap_differences(studies, decimals)
    n = snapped.shape[1]
    if n < 2:
        nan = np.full(snapped.shape[0], math.nan)
        return nan, nan, n
    mean = snapped.mean(axis=1)
    sd = snapped.std(axis=1, ddof=1)
    # Equal values have no spread, though their float SD can be 1e-17 (three 0.1s do not average to 0.1): compare the
    # snapped values themselves; and an SD that underflows to 0 is none to divide by.
    spread = (snapped.max(axis=1) > snapped.min(axis=1)) & (sd > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        se = np.where(spread, sd / math.sqrt(n), math.nan)
    return mean, se, n


def tost_t_studies(studies, margin: float, decimals: int = DEFAULT_SNAP_DECIMALS) -> np.ndarray:
    """`tost_t` of every row of a (studies x clusters) array, by one vectorised computation."""
    _check_margin(margin)
    values = np.asarray(studies, dtype=float)
    if values.ndim != 2:
        raise ValueError("studies must be a (studies x clusters) array")
    mean, se, n = _mean_and_se(values, decimals)
    if n < 2:
        return mean  # NaN for every study
    with np.errstate(invalid="ignore"):
        lower = stats.t.sf((mean + margin) / se, n - 1)  # H0: theta <= -margin
        upper = stats.t.cdf((mean - margin) / se, n - 1)  # H0: theta >= margin
    return np.maximum(lower, upper)  # NaN where there is no spread


def tost_t(differences, margin: float, decimals: int = DEFAULT_SNAP_DECIMALS) -> float:
    """The two one-sided t-tests' p-value for equivalence of the mean cluster difference within plus or minus `margin`
    (Schuirmann's TOST, S15): the larger of the one-sided p-values of theta > -margin and of theta < margin, each a
    t-test on n - 1 degrees of freedom with the standard error sd / sqrt(n) of the n cluster differences.

    The clusters are the unit: independent, their differences roughly normal (the test is robust to that for a few
    dozen clusters). Undefined clusters (NaN) are left out; float noise is snapped first (S01). Fewer than two clusters,
    or clusters with no spread between them (a standard error of 0), give NaN, never a p-value of 0: with no spread
    estimated there is no evidence, and a NaN counts as p = 1 in Holm (S02).
    """
    values = np.asarray(differences, dtype=float)
    values = values[~np.isnan(values)]
    return float(tost_t_studies(values.reshape(1, -1), margin, decimals)[0])


def tost_t_interval(differences, level: float, decimals: int = DEFAULT_SNAP_DECIMALS) -> tuple[float, float]:
    """The interval dual to `tost_t` at `level` L: mean plus or minus the t quantile at 1 - L times the standard error,
    a 1 - 2L interval. TOST p <= L exactly when it lies within plus or minus the margin. NaN where `tost_t` is."""
    if not 0.0 < level < 0.5:
        raise ValueError(f"the level of a TOST interval must be strictly between 0 and 0.5, got {level}")
    values = np.asarray(differences, dtype=float)
    mean, se, n = _mean_and_se(values[~np.isnan(values)].reshape(1, -1), decimals)
    if n < 2 or math.isnan(se[0]):
        return math.nan, math.nan
    half = float(stats.t.ppf(1 - level, n - 1) * se[0])
    return float(mean[0]) - half, float(mean[0]) + half


def _snap_differences(values, decimals: int) -> np.ndarray:
    """Float noise snapped away from differences of any shape: tiny values are exact zeros, the rest are rounded."""
    values = np.asarray(values, dtype=float)
    return snap(np.where(np.abs(values) < 10.0**-decimals, 0.0, values), decimals)


def wilcoxon_input(differences, decimals: int = DEFAULT_SNAP_DECIMALS) -> np.ndarray:
    """The differences the rank test sees: undefined clusters (NaN) removed, float noise snapped (S01).

    A cluster difference that is zero in arithmetic comes out of a chain of means as +-1e-17 about two times in three,
    and the test would rank that noise as the smallest signed rank instead of dropping it as a zero. Differences
    below 10**-decimals are therefore exact zeros, and the rest are rounded to the same grid, so differences equal in
    arithmetic reach the test as equal and tie, whatever order the means were summed in.
    """
    values = np.asarray(differences, dtype=float)
    return _snap_differences(values[~np.isnan(values)], decimals)


def zeros_dropped(differences, decimals: int = DEFAULT_SNAP_DECIMALS) -> int:
    """How many cluster differences are zero and so dropped by the Wilcoxon test (S03).

    Counted on the array `wilcoxon_p` hands to scipy, with scipy's own test (`d == 0`).
    """
    return int((wilcoxon_input(differences, decimals) == 0).sum())


def wilcoxon_p(differences, decimals: int = DEFAULT_SNAP_DECIMALS, method: str = "auto") -> float:
    """Two-sided Wilcoxon signed-rank p-value of the cluster differences, zeros dropped.

    No difference left to test gives NaN. Differences that are all zero give 1.0, no evidence of a difference
    (scipy would raise) (S03).
    """
    if method not in WILCOXON_METHODS:
        raise ValueError(f"unknown Wilcoxon method {method!r}; expected one of {', '.join(WILCOXON_METHODS)}")
    d = wilcoxon_input(differences, decimals)
    if d.size == 0:
        return math.nan
    if np.all(d == 0):
        return 1.0
    return float(stats.wilcoxon(d, zero_method="wilcox", method=method).pvalue)


@functools.cache
def _p_at_rank_sum(n: int, rank_sum: int, decimals: int, method: str) -> float:
    """`wilcoxon_p` of n differences whose magnitudes are distinct and whose positive ranks add up to `rank_sum`.

    Such differences have a p-value that depends on that sum alone (the ranks are 1..n whatever the magnitudes), so
    the representative below, the ranks themselves with the signs that give the sum, stands for all of them. The
    greedy choice from the largest rank reaches every sum from 0 to n(n+1)/2.
    """
    signed = np.empty(n)
    remaining = rank_sum
    for rank in range(n, 0, -1):
        positive = rank <= remaining
        signed[rank - 1] = rank if positive else -rank
        remaining -= rank if positive else 0
    return wilcoxon_p(signed, decimals, method)


def wilcoxon_p_studies(studies, decimals: int = DEFAULT_SNAP_DECIMALS, method: str = "auto") -> np.ndarray:
    """`wilcoxon_p` of every row of a (studies x clusters) array: the same function, evaluated once per rank pattern.

    A study whose snapped differences hold no zero and no two equal magnitudes (what draws from a continuous
    distribution give) takes its p-value from `_p_at_rank_sum`, one call of `wilcoxon_p` for each distinct sum of
    positive ranks instead of one for each study, which is what makes a simulation of thousands of studies affordable.
    Any other study, with a zero, a tie, an undefined cluster or an infinite value (the tie test subtracts magnitudes,
    and infinity minus infinity is not a tie), goes through `wilcoxon_p` itself.
    """
    values = np.asarray(studies, dtype=float)
    if values.ndim != 2:
        raise ValueError("studies must be a (studies x clusters) array")
    n = values.shape[1]
    snapped = _snap_differences(values, decimals)
    magnitudes = np.abs(snapped)
    with np.errstate(invalid="ignore"):  # a non-finite value makes the row irregular, whatever these give for it
        irregular = ~np.isfinite(values).all(axis=1) | (magnitudes == 0).any(axis=1)
        irregular |= (np.diff(np.sort(magnitudes, axis=1), axis=1) == 0).any(axis=1)
    if n == 0:
        irregular[:] = True
    p_values = np.empty(values.shape[0])
    for index in np.flatnonzero(irregular):
        p_values[index] = wilcoxon_p(values[index], decimals, method)
    regular = ~irregular
    if regular.any():
        ranks = np.argsort(np.argsort(magnitudes[regular], axis=1), axis=1) + 1
        sums = (ranks * (snapped[regular] > 0)).sum(axis=1)
        distinct, inverse = np.unique(sums, return_inverse=True)
        table = np.array([_p_at_rank_sum(n, int(total), decimals, method) for total in distinct])
        p_values[regular] = table[inverse]
    return p_values


def level(alpha: float, weight: int, total: int) -> float:
    """A multiplicity level, alpha * weight / total, multiplied first (S06).

    Every level and every comparison uses this one expression, so a p-value computed as the same expression sits
    exactly on its level instead of one rounding away from it.
    """
    return alpha * weight / total


@dataclasses.dataclass(frozen=True)
class HolmResult:
    """Holm's step-down procedure over one declared family, in the order the p-values were given."""

    adjusted: tuple[float, ...]  # Holm-adjusted p for reports; NaN where the test had no p-value
    levels: tuple[float, ...]  # the level each p-value is compared with at its step
    reject: tuple[bool, ...]  # decided by comparing the raw p-value with its level, p <= level (see rejects)


def holm(p_values: Sequence[float | None], alpha: float) -> HolmResult:
    """Holm step-down over one pre-registered family.

    The family keeps its full size: a missing or NaN p-value (an untestable contrast) counts as p = 1 and stays in
    the family, so it cannot shrink m and make the others look more significant. It never rejects, and its adjusted
    value stays NaN (S02).

    Rejection compares the raw p-value with `level(alpha, 1, m - step)` using `<=`, so a p-value exactly on its level
    is rejected as declared. The adjusted p-values are for reporting; at the boundary a float product can land one
    rounding above alpha, which is why they do not decide (S06).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")
    missing = [p is None or math.isnan(p) for p in p_values]
    values = [1.0 if gone else float(p) for p, gone in zip(p_values, missing, strict=True)]
    if any(not 0.0 <= v <= 1.0 for v in values):
        raise ValueError("p-values must lie in [0, 1]")
    m = len(values)
    order = sorted(range(m), key=lambda i: (values[i], i))  # stable: ties keep the declared order
    levels, adjusted, reject = [math.nan] * m, [math.nan] * m, [False] * m
    stepping, running = True, 0.0
    for step, i in enumerate(order):
        levels[i] = level(alpha, 1, m - step)
        running = max(running, (m - step) * values[i])
        adjusted[i] = math.nan if missing[i] else min(running, 1.0)
        stepping = stepping and not missing[i] and bool(rejects(values[i], levels[i]))
        reject[i] = stepping
    return HolmResult(tuple(adjusted), tuple(levels), tuple(reject))
