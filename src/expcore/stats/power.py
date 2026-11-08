# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/secondary.py, blob 142c57b3fcb519af1e7d90e24f13bf4bf1364365
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Power of the paired Wilcoxon signed-rank test by simulation, driven by the preregistration's power settings.

Independent of the pipeline it checks (S09): this file imports numpy and scipy only, and runs scipy's test directly
rather than the registered estimator, so a fault in the estimator cannot hide in its own power study. An import-graph
test enforces it. Pilot differences enter as plain arrays; building them from rows is the pipeline's job (see analysis).
"""

from __future__ import annotations

import dataclasses
import functools
import math
from collections.abc import Callable, Mapping, Sequence

import numpy as np
from scipy import stats

from expcore.stats.rejection import rejects


@dataclasses.dataclass(frozen=True)
class PowerSettings:
    """The power-study settings of a preregistration."""

    seed: int
    n_simulations: int
    target_clusters: int
    target_difference: float
    sd_grid: tuple[float, ...]
    target_power: float = 0.8  # the power a minimum detectable effect must reach


def power_sim(
    sd: float, n: int, delta: float, alpha: float, n_sim: int, rng: np.random.Generator, method: str = "auto"
) -> float:
    """Power of the paired Wilcoxon signed-rank test, by simulation.

    Cluster-level paired differences are drawn from N(delta, sd^2), n clusters per study, `n_sim` studies. The share
    of studies with a two-sided p-value at most alpha is the power (see `rejects`, S06). Zero differences are dropped,
    as in the pipeline.
    """
    if not (math.isfinite(sd) and sd > 0):
        raise ValueError(f"sd must be a positive number, got {sd}")
    if n < 1 or n_sim < 1:
        raise ValueError(f"n and n_sim must be at least 1, got n={n} and n_sim={n_sim}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")
    draws = rng.normal(delta, sd, size=(n_sim, n))
    hits = sum(bool(rejects(stats.wilcoxon(row, zero_method="wilcox", method=method).pvalue, alpha)) for row in draws)
    return hits / n_sim


EXACT_LIMIT = (
    200  # up to this many clusters the null distribution is listed exactly, beyond it the normal curve is used
)


@functools.cache
def _null_cdf(n: int) -> np.ndarray:
    """P(W <= k) for k = 0..n(n+1)/2, W the sum of the ranks that carry a plus sign when n ranks are signed at random."""
    total = n * (n + 1) // 2
    pmf = np.zeros(total + 1)
    pmf[0] = 1.0
    for rank in range(1, n + 1):
        shifted = np.zeros_like(pmf)
        shifted[rank:] = pmf[: total + 1 - rank]
        pmf = (pmf + shifted) / 2
    return np.cumsum(pmf)


def signed_rank_p_values(samples: np.ndarray) -> np.ndarray:
    """Two-sided signed-rank p-value of every row of a (studies x clusters) array of continuous differences.

    Rows are taken to hold no zero and no tie, which draws from a continuous distribution give with probability one.
    The p-value is twice the null probability of a rank sum at most as large as the smaller of the two signed sums
    (exact for up to EXACT_LIMIT clusters, the normal approximation beyond), at most 1. It agrees with scipy's exact
    test on such rows and runs over all rows at once, which a search for a minimum detectable effect needs.
    """
    x = np.asarray(samples, dtype=float)
    if x.ndim != 2 or x.shape[1] < 1:
        raise ValueError("samples must be a (studies x clusters) array with at least one cluster")
    n = x.shape[1]
    ranks = np.argsort(np.argsort(np.abs(x), axis=1), axis=1) + 1
    plus = (ranks * (x > 0)).sum(axis=1)
    total = n * (n + 1) // 2
    smaller = np.minimum(plus, total - plus)
    if n <= EXACT_LIMIT:
        return np.minimum(1.0, 2.0 * _null_cdf(n)[smaller])
    mean, sd = total / 2, math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    return np.minimum(1.0, 2.0 * stats.norm.cdf((smaller + 0.5 - mean) / sd))


PValueFunction = Callable[[np.ndarray], np.ndarray]  # a (studies x clusters) array to one two-sided p-value per study


def minimum_detectable_effect(
    sd: float,
    n: int,
    alpha: float,
    settings: PowerSettings,
    rng: np.random.Generator,
    p_values: PValueFunction | None = None,
) -> float:
    """The smallest difference the signed-rank test detects at `settings.target_power` with n clusters, by simulation.

    Cluster differences are drawn from N(delta, sd^2), `settings.n_simulations` studies of n clusters sharing one set
    of standard normal draws, and delta is found by bisection between 0 and 8 sd to a precision of sd / 1000. The
    result is NaN when even 8 sd does not reach the target power, which is when n is too small for a two-sided test at
    this alpha to ever reject (five clusters cannot get a p-value under 0.0625, so they reject only at alpha 0.0625 or
    more). A study rejects when its p-value is at most alpha (see `rejects`, S06). It scales with sd, since the test
    sees ranks.

    `p_values` is the test the studies are judged by; by default the vectorised exact signed-rank test. A caller that
    has a test with parameters of its own (the normal approximation, a snapping grid) passes the function it analyses
    with, so the effect found is the one that test can detect (S06). This module takes the function and imports none.
    """
    if not (math.isfinite(sd) and sd > 0):
        raise ValueError(f"sd must be a positive number, got {sd}")
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")
    noise = rng.standard_normal((settings.n_simulations, n))

    test = p_values if p_values is not None else signed_rank_p_values

    def power(delta: float) -> float:
        return float(rejects(np.asarray(test(delta + sd * noise), dtype=float), alpha).mean())

    low, high = 0.0, 8.0 * sd
    if power(high) < settings.target_power:
        return math.nan
    while high - low > sd / 1000:
        middle = (low + high) / 2
        if power(middle) >= settings.target_power:
            high = middle
        else:
            low = middle
    return high


def pilot_cluster_sd(differences: Sequence[float]) -> float:
    """Between-cluster SD of the paired differences of a pilot; NaN with fewer than two defined clusters."""
    values = np.asarray(differences, dtype=float)
    values = values[~np.isnan(values)]
    return float(np.std(values, ddof=1)) if values.size >= 2 else math.nan


def power_table(
    settings: PowerSettings,
    alpha: float,
    n_contrasts: int,
    sds: Sequence[float] | None = None,
    simulate: bool = True,
    method: str = "auto",
) -> list[dict]:
    """Power at the target number of clusters and difference, for each SD, at alpha and at alpha / n_contrasts.

    The SDs default to the settings' grid. A NaN SD, or `simulate=False`, gives a NaN power, so a gap shows. One
    generator, seeded from the settings, serves the rows in order.
    """
    rng = np.random.default_rng(settings.seed)
    rows = []
    for sd in sds if sds is not None else settings.sd_grid:
        for label, level in (("alpha", alpha), ("alpha_over_contrasts", alpha / n_contrasts)):
            runnable = simulate and not math.isnan(sd)
            power = (
                power_sim(
                    sd, settings.target_clusters, settings.target_difference, level, settings.n_simulations, rng, method
                )
                if runnable
                else math.nan
            )
            rows.append(
                {
                    "sd": sd,
                    "n_clusters": settings.target_clusters,
                    "delta": settings.target_difference,
                    "alpha_level": label,
                    "alpha": level,
                    "power": power,
                }
            )
    return rows


def pilot_power(
    pilot: Mapping[str, Sequence[float]], settings: PowerSettings, alpha: float, method: str = "auto"
) -> list[dict]:
    """Power re-estimate from a pilot: per contrast, the between-cluster SD of its paired differences feeds the table.

    `pilot` maps a contrast name to its cluster-level differences. A contrast the pilot cannot estimate (fewer than
    two clusters) keeps its rows with NaN SD and power. The correction for several contrasts divides alpha by the
    number of contrasts in `pilot`.
    """
    rows = []
    for name, differences in pilot.items():
        defined = int(np.count_nonzero(~np.isnan(np.asarray(differences, dtype=float))))
        table = power_table(settings, alpha, len(pilot), [pilot_cluster_sd(differences)], method=method)
        rows += [{"contrast": name, "pilot_clusters": defined, **row} for row in table]
    return rows
