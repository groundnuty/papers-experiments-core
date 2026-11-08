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
import math
from collections.abc import Mapping, Sequence

import numpy as np
from scipy import stats


@dataclasses.dataclass(frozen=True)
class PowerSettings:
    """The power-study settings of a preregistration."""

    seed: int
    n_simulations: int
    target_clusters: int
    target_difference: float
    sd_grid: tuple[float, ...]


def power_sim(
    sd: float, n: int, delta: float, alpha: float, n_sim: int, rng: np.random.Generator, method: str = "auto"
) -> float:
    """Power of the paired Wilcoxon signed-rank test, by simulation.

    Cluster-level paired differences are drawn from N(delta, sd^2), n clusters per study, `n_sim` studies. The share
    of studies with a two-sided p-value below alpha is the power. Zero differences are dropped, as in the pipeline.
    """
    if not (math.isfinite(sd) and sd > 0):
        raise ValueError(f"sd must be a positive number, got {sd}")
    if n < 1 or n_sim < 1:
        raise ValueError(f"n and n_sim must be at least 1, got n={n} and n_sim={n_sim}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")
    draws = rng.normal(delta, sd, size=(n_sim, n))
    hits = sum(stats.wilcoxon(row, zero_method="wilcox", method=method).pvalue < alpha for row in draws)
    return hits / n_sim


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
