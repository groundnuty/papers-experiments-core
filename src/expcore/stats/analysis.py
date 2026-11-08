# Provenance: new
"""Running a preregistration: each declared contrast through its estimator, then Holm over the declared family.

Rows go in, results come out with the hash of the preregistration they were computed under (P04). Nothing here knows
an estimator: a contrast names one, the registry supplies it, and the result type carries what every estimator
reports. A contrast that cannot be estimated keeps its place with NaN values, and a NaN p-value stays in the Holm
family as p = 1 (S02).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence

from expcore.stats import contrasts as kernel
from expcore.stats import estimators, power
from expcore.stats.estimators import ModelEstimate
from expcore.stats.pairing import ContrastSpec, pair_rows
from expcore.stats.prereg import Preregistration
from expcore.stats.rows import Row


@dataclasses.dataclass(frozen=True)
class ContrastResult:
    """One contrast's estimate and what pairing left out; `counts` holds the estimator's own counts."""

    name: str
    outcome: str
    estimator: str
    cluster: str
    arms: tuple[str, str]  # A and B; the contrast is A - B
    factor: str | None  # the varying factor of a factor contrast, None for a named-condition contrast
    estimate: float
    p_value: float
    ci_low: float
    ci_high: float
    n_clusters: int
    n_clusters_dropped: int
    n_pairs: int
    n_pairs_missing: int
    n_unpaired: int
    counts: Mapping[str, int]
    per_model: tuple[ModelEstimate, ...]


@dataclasses.dataclass(frozen=True)
class HolmDecision:
    """Holm's decision for one contrast of the family: the raw and the adjusted p-value, the level, the verdict."""

    name: str
    p_value: float
    p_holm: float
    level: float
    reject: bool


@dataclasses.dataclass(frozen=True)
class AnalysisResult:
    """Every declared contrast, the decisions over the family, and the manifest entries of the preregistration."""

    contrasts: tuple[ContrastResult, ...]
    holm: tuple[HolmDecision, ...]
    alpha: float
    manifest: Mapping[str, str]

    def contrast(self, name: str) -> ContrastResult:
        """Return the result of the contrast with this name."""
        for result in self.contrasts:
            if result.name == name:
                return result
        raise KeyError(f"no contrast named {name!r}; declared: {', '.join(r.name for r in self.contrasts)}")


def run_contrast(rows: Iterable[Row], spec: ContrastSpec, prereg: Preregistration) -> ContrastResult:
    """Pair the rows for one contrast and run its registered estimator under the preregistration's settings."""
    pairing = pair_rows(rows, spec)
    estimate = estimators.get(spec.estimator)(pairing.differences, prereg.estimator_settings(spec))
    return ContrastResult(
        name=spec.name,
        outcome=spec.outcome,
        estimator=spec.estimator,
        cluster=spec.cluster,
        arms=(spec.arms[0], spec.arms[1]),
        factor=spec.factor,
        estimate=estimate.estimate,
        p_value=estimate.p_value,
        ci_low=estimate.ci_low,
        ci_high=estimate.ci_high,
        n_clusters=estimate.n_clusters,
        n_clusters_dropped=pairing.n_clusters_dropped,
        n_pairs=pairing.n_pairs,
        n_pairs_missing=pairing.n_pairs_missing,
        n_unpaired=pairing.n_unpaired,
        counts=dict(estimate.counts),
        per_model=estimate.per_model,
    )


def analyse(rows: Iterable[Row], prereg: Preregistration) -> AnalysisResult:
    """Run every declared contrast, then Holm over the declared family at the preregistered alpha."""
    rows = list(rows)
    results = tuple(run_contrast(rows, spec, prereg) for spec in prereg.contrasts.values())
    by_name = {result.name: result for result in results}
    p_values = [by_name[name].p_value for name in prereg.holm_family]
    outcome = kernel.holm(p_values, prereg.alpha)
    decisions = tuple(
        HolmDecision(name, p, adjusted, level, reject)
        for name, p, adjusted, level, reject in zip(
            prereg.holm_family, p_values, outcome.adjusted, outcome.levels, outcome.reject, strict=True
        )
    )
    return AnalysisResult(results, decisions, prereg.alpha, prereg.manifest())


def pilot_power(rows: Iterable[Row], prereg: Preregistration, names: Sequence[str] | None = None) -> list[dict]:
    """Power re-estimate from pilot rows: each contrast's between-cluster SD of paired differences feeds the table.

    Pairing is the pipeline's job, so it happens here and the power module receives plain arrays (S09).
    """
    if prereg.power is None:
        raise ValueError(f"{prereg.source}: has no [power] section, so there is no power study to run")
    rows = list(rows)
    chosen = list(names) if names is not None else list(prereg.contrasts)
    unknown = [name for name in chosen if name not in prereg.contrasts]
    if unknown:
        raise ValueError(f"{prereg.source}: no contrast named {unknown[0]!r}; declared: {', '.join(prereg.contrasts)}")
    pilot = {name: pair_rows(rows, prereg.contrasts[name]).differences.cluster_differences for name in chosen}
    return power.pilot_power(pilot, prereg.power, prereg.alpha)
