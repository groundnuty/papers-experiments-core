# Provenance: new
"""Running a preregistration: each declared contrast through its estimator, then Holm over the declared family.

Rows go in, results come out with the hash of the preregistration they were computed under (P04). Nothing here knows
an estimator: a contrast names one, the registry supplies it, and the result type carries what every estimator
reports. A contrast that cannot be estimated keeps its place with NaN values, and a NaN p-value stays in the Holm
family as p = 1 (S02).

Each contrast ends in one of three conclusions, and never in "no difference":
- "difference": the test is significant (a member of the Holm family by Holm's decision, any other contrast by its raw
  p-value at alpha, since nothing adjusts for it) and equivalence does not hold;
- "equivalent": the preregistration declares an `equivalence_margin` for the contrast and the cluster-bootstrap
  interval at 1 - 2 alpha lies inside plus or minus the margin (the interval form of two one-sided tests at alpha). It
  takes precedence over "difference": an effect shown to be smaller than the margin the study called negligible is
  reported as equivalent, with `significant` still saying whether the test rejected;
- "not_detected": everything else, including a non-significant result without a margin and a margin whose interval is
  too wide. Failing to detect a difference is not evidence of none.

With power settings, a result also carries the minimum detectable effect at its own number of clusters.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from expcore.stats import contrasts as kernel
from expcore.stats import estimators, power
from expcore.stats.estimators import ModelEstimate, PairedDifferences
from expcore.stats.pairing import ContrastSpec, pair_rows
from expcore.stats.prereg import Preregistration, name_digest
from expcore.stats.rejection import rejects
from expcore.stats.rows import Row

CONCLUSIONS = ("difference", "not_detected", "equivalent")


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
    tasks_dropped: tuple[str, ...]  # tasks one of the two arms did not run, left out of this contrast only
    counts: Mapping[str, int]
    per_model: tuple[ModelEstimate, ...]
    in_family: bool  # a member of the Holm family; any other contrast has no multiplicity control
    significant: bool  # Holm's decision for a member of the family, the raw p-value at alpha for any other
    equivalence_margin: float | None  # as preregistered; None when the contrast has none
    equivalence_interval: tuple[float, float] | None  # the 1 - 2 alpha interval; None without a margin
    equivalent: bool  # the interval lies inside plus or minus the margin; always False without one
    conclusion: str  # one of CONCLUSIONS
    minimum_detectable_effect: float | None  # None without power settings, NaN when it cannot be reached

    @property
    def n_tasks_dropped(self) -> int:
        """How many tasks were left out of this contrast because one of its arms did not run them."""
        return len(self.tasks_dropped)


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


def conclude(significant: bool, equivalent: bool) -> str:
    """The conclusion: equivalent if shown within the margin, else difference if significant, else not detected."""
    if equivalent:
        return "equivalent"
    return "difference" if significant else "not_detected"


def _equivalence(
    spec: ContrastSpec, prereg: Preregistration, differences: PairedDifferences
) -> tuple[tuple[float, float] | None, bool]:
    """Run the estimator again at level 1 - 2 alpha, with the same resamples, and test it against the margin."""
    margin = spec.equivalence_margin
    if margin is None:
        return None, False
    if prereg.alpha >= 0.5:
        raise ValueError(f"contrast {spec.name!r}: equivalence needs alpha below 0.5, got {prereg.alpha}")
    settings = dataclasses.replace(prereg.estimator_settings(spec), level=1 - 2 * prereg.alpha)
    result = estimators.get(spec.estimator)(differences, settings)
    interval = (result.ci_low, result.ci_high)
    return interval, bool(-margin <= result.ci_low and result.ci_high <= margin)


def _minimum_detectable_effect(
    spec: ContrastSpec, prereg: Preregistration, differences: PairedDifferences, in_family: bool
) -> float | None:
    """The smallest effect the contrast's cluster count could detect, from its own between-cluster SD.

    Rejection in the simulation is decided by the p-value function and the parameters the contrast's estimator
    analyses with (a preregistered Wilcoxon `method`, for one), so the effect found is one the analysis itself can
    reject (S06, S09). The level is alpha divided by the size of the Holm family for a member and alpha for any other
    contrast. Dividing is conservative: it is the level of the first step of Holm's procedure, the hardest to clear,
    and a member whose p-value ranks later is judged at a larger level and detects less than this. NaN when the SD is
    undefined or zero, when the estimator registered no p-values function to simulate with, or when the count of
    clusters is too small for the test to reach the target power at that level.
    """
    if prereg.power is None:
        return None
    sd = power.pilot_cluster_sd(differences.cluster_differences)
    if not (math.isfinite(sd) and sd > 0):
        return math.nan
    p_values = estimators.p_values_function(spec.estimator, prereg.estimator_settings(spec).params)
    if p_values is None:
        return math.nan
    level = prereg.alpha / len(prereg.holm_family) if in_family else prereg.alpha
    rng = np.random.default_rng([prereg.power.seed, name_digest(spec.name)])

    return power.minimum_detectable_effect(sd, len(differences.clusters), level, prereg.power, rng, p_values)


def run_contrast(
    rows: Iterable[Row], spec: ContrastSpec, prereg: Preregistration, significant: bool | None = None
) -> ContrastResult:
    """Pair the rows for one contrast and run its registered estimator under the preregistration's settings.

    `significant` is the decision of the test. `analyse` passes Holm's decision for a member of the family; left out,
    it is the raw p-value at alpha, with nothing adjusting for the family.
    """
    pairing = pair_rows(rows, spec)
    estimate = estimators.get(spec.estimator)(pairing.differences, prereg.estimator_settings(spec))
    in_family = spec.name in prereg.holm_family
    if significant is None:
        significant = bool(rejects(estimate.p_value, prereg.alpha))  # False for a NaN p-value
    interval, equivalent = _equivalence(spec, prereg, pairing.differences)
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
        tasks_dropped=pairing.tasks_dropped,
        counts=dict(estimate.counts),
        per_model=estimate.per_model,
        in_family=in_family,
        significant=significant,
        equivalence_margin=spec.equivalence_margin,
        equivalence_interval=interval,
        equivalent=equivalent,
        conclusion=conclude(significant, equivalent),
        minimum_detectable_effect=_minimum_detectable_effect(spec, prereg, pairing.differences, in_family),
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
    rejected = {d.name: d.reject for d in decisions}
    decided = tuple(
        dataclasses.replace(r, significant=rejected[r.name], conclusion=conclude(rejected[r.name], r.equivalent))
        if r.name in rejected
        else r
        for r in results
    )
    return AnalysisResult(decided, decisions, prereg.alpha, prereg.manifest())


@dataclasses.dataclass(frozen=True)
class PowerResult:
    """A power table with the manifest entries of the preregistration it was computed under (P04)."""

    rows: tuple[dict, ...]
    manifest: Mapping[str, str]


def power_study(prereg: Preregistration, simulate: bool = True) -> PowerResult:
    """The power table of the preregistration's [power] settings, at alpha and at alpha divided by the family size.

    It reads nothing but the preregistration and runs scipy's test directly (see power), so it checks the design
    without touching the pipeline.
    """
    if prereg.power is None:
        raise ValueError(f"{prereg.source}: has no [power] section, so there is no power study to run")
    rows = power.power_table(prereg.power, prereg.alpha, len(prereg.holm_family), simulate=simulate)
    return PowerResult(tuple(rows), prereg.manifest())


def pilot_power(rows: Iterable[Row], prereg: Preregistration, names: Sequence[str] | None = None) -> PowerResult:
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
    return PowerResult(tuple(power.pilot_power(pilot, prereg.power, prereg.alpha)), prereg.manifest())
