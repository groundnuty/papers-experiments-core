# Provenance: new
"""Running a preregistration: each declared contrast through its estimator, then Holm over the declared family.

Rows go in, results come out with the hash of the preregistration they were computed under (P04). Nothing here knows
an estimator: a contrast names one, the registry supplies it, and the result type carries what every estimator
reports. A contrast that cannot be estimated keeps its place with NaN values, and a NaN p-value stays in the Holm
family as p = 1 (S02).

Each contrast ends in one of five conclusions, and never in "no difference". A contrast that used no pair, whatever
the cause, is "untestable": nothing was tested, so no other word applies (S11). It has no estimate, interval, p-value
or minimum detectable effect, whatever its estimator returned for no data; in the Holm family it stays a member as
p = 1, the kernel's reading of a NaN p-value, so the family keeps its size and it never rejects. For every other
contrast two facts decide: whether the test is significant (a member of the Holm family by Holm's decision, any other
contrast by its raw p-value at alpha, since nothing adjusts for it), and whether the preregistration declares an
`equivalence_margin` for the contrast and the cluster-bootstrap interval at 1 - 2 alpha lies inside plus or minus that
margin (the interval form of two one-sided tests at alpha).
- "difference": significant, and not inside the margin (or no margin is declared);
- "trivial_difference": significant, and inside the margin. The effect is detected and smaller than the margin the
  study called negligible; it has its own word so that a table of conclusions cannot hide a detected effect;
- "equivalent": inside the margin, and not significant;
- "not_detected": everything else, including a non-significant result without a margin and a margin whose interval is
  too wide. Failing to detect a difference is not evidence of none;
- "untestable": no pair was used. It is not evidence of anything, least of all of no effect.

A contrast outside the preregistered Holm family is `exploratory`: nothing controls its multiplicity, it is judged by
its raw p-value at alpha, and every table or report that shows its conclusion shows that label with it. A
preregistration with `exploratory_only = true` has no family, so every contrast is exploratory, Holm decides nothing,
and the result says that no confirmatory claim is made (S12).

A treatment attempt (a run that ended in an infrastructure failure its condition declares as its treatment) is scored
as its condition's outcome, so a scorer that returns None for it takes its pair out of every contrast on that outcome,
and the manipulation silently leaves the comparison it was kept for (S10). Each result counts those pairs, with every
other cause of a missing pair (`pairing.MISSING_CAUSES`). A member of the Holm family that loses any is refused
(`TreatmentOutcomesRefused`) unless its preregistration declares `treatment_outcomes` = "excluded" (every pair with a
treatment attempt is left out of the contrast and reported as such). "scored" says the scorer returns values for
treatment attempts, and that claim is checked: a member that still loses a pair this way is refused too, past the
contrast's preregistered `max_unscored_treatment` (0 unless set). An exploratory contrast reports the count and is not
refused.

With power settings, a result also carries the minimum detectable effect at its own number of clusters.

Every result names its two units: `estimation_unit`, the cell one value is computed in, and `independent_unit`, the
cluster, whose values the test, the bootstrap and the minimum detectable effect count (`n_clusters`); `n_cells` and
`cells_lost` count the cells. A contrast of one outcome estimates per (model, cluster) cell and averages each
cluster's cells with equal weight per model, so several models on one cluster are one unit, never several.

A ratio contrast (S13, `expcore.stats.ratio`) runs the same way on its log ratios: its estimate, interval, margin and
minimum detectable effect are natural-log ratios. With an estimation unit that names the model its sums run per
(model, cluster) cell, each cluster's value is the equal-weight mean of its cells, `n_clusters` still counts clusters,
and `per_model` holds each model's estimate over its own cells, from the same draws of the clusters, with no test of
its own. A member of the Holm family that loses a pair because a run's numerator is None while its denominator is 0
is refused (`RatioNumeratorRefused`): its ratio would count the cost of successful runs alone. An exploratory one
reports the count. Both refusals are `ContrastRefused`.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from expcore.stats import contrasts as kernel
from expcore.stats import estimators, power
from expcore.stats.estimators import ModelEstimate, PairedDifferences
from expcore.stats.pairing import ContrastSpec, Pairing, pair_rows
from expcore.stats.prereg import Preregistration, name_digest
from expcore.stats.ratio import RatioForm
from expcore.stats.rejection import rejects
from expcore.stats.rows import AbsentRun, Row

UNTESTABLE = "untestable"  # the conclusion of a contrast that used no pair
CONCLUSIONS = ("difference", "trivial_difference", "equivalent", "not_detected", UNTESTABLE)


class ContrastRefused(ValueError):
    """A confirmatory contrast whose pairs were lost in a way that would bias it unseen; the message names the fix."""


class TreatmentOutcomesRefused(ContrastRefused):
    """A confirmatory contrast lost pairs to treatment attempts scored None, and its preregistration either does not
    say how treatment attempts are scored for it or claims they are scored and allows fewer such pairs."""


class RatioNumeratorRefused(ContrastRefused):
    """A confirmatory ratio contrast lost pairs because a run's numerator is None while its denominator is 0 (S13)."""


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
    n_pairs_missing: int  # pairs whose outcome is None on either side: pairs_missing's two None causes together
    n_unpaired: int  # run keys analysed in one arm only
    tasks_dropped: tuple[str, ...]  # tasks one of the two arms did not run, left out of this contrast only
    treatment_outcomes: str | None  # as preregistered: "scored", "excluded", or None when undeclared
    max_unscored_treatment: int  # as preregistered: the pairs a "scored" member may lose to a treatment None
    pairs_missing: Mapping[str, int]  # every cause of pairing.MISSING_CAUSES with its count, zeros included
    counts: Mapping[str, int]
    per_model: tuple[ModelEstimate, ...]
    in_family: bool  # a member of the Holm family; any other contrast has no multiplicity control
    exploratory: bool  # outside the Holm family, so judged by its raw p-value at alpha; label it wherever it is shown
    significant: bool  # Holm's decision for a member of the family, the raw p-value at alpha for any other
    equivalence_margin: float | None  # as preregistered; None when the contrast has none
    equivalence_interval: tuple[float, float] | None  # the 1 - 2 alpha interval; None without a margin
    equivalent: (
        bool  # the interval lies inside plus or minus the margin; False without one; says nothing of `significant`
    )
    conclusion: str  # one of CONCLUSIONS
    minimum_detectable_effect: float | None  # None without power settings, NaN when it cannot be reached
    ratio: RatioForm | None = None  # a ratio contrast's form (S13): every number above is then a natural-log ratio
    # the cell one value is computed in (S13): ("model", cluster) for a contrast of one outcome; for a ratio contrast
    # (cluster,), its sums pooling a cluster's models, or ("model", cluster), its sums per model's cell
    estimation_unit: tuple[str, ...] | None = None
    independent_unit: str | None = None  # the cluster, whose values n_clusters, the test and the bootstrap count
    n_cells: int | None = None  # cells of the estimation unit with a value
    cells_lost: Mapping[str, int] = dataclasses.field(default_factory=dict)  # cells seen with no value, by cause

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
    """Every declared contrast, the decisions over the family, and the manifest entries of the preregistration.

    `exploratory_only` is the preregistration's statement that it makes no confirmatory claim (S12): there is no Holm
    decision, and every contrast is exploratory.
    """

    contrasts: tuple[ContrastResult, ...]
    holm: tuple[HolmDecision, ...]
    alpha: float
    manifest: Mapping[str, str]
    exploratory_only: bool = False

    def contrast(self, name: str) -> ContrastResult:
        """Return the result of the contrast with this name."""
        for result in self.contrasts:
            if result.name == name:
                return result
        raise KeyError(f"no contrast named {name!r}; declared: {', '.join(r.name for r in self.contrasts)}")


def conclude(significant: bool, equivalent: bool, untestable: bool = False) -> str:
    """The conclusion from the test and the margin: a significant effect inside the margin is a trivial difference.

    `untestable` (the contrast used no pair) overrides both: with no evidence, no other word applies (S11).
    """
    if untestable:
        return UNTESTABLE
    if equivalent:
        return "trivial_difference" if significant else "equivalent"
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
    contrast.

    That level is conservative for a member. Holm's step-down levels are alpha / m, alpha / (m - 1), ..., alpha, and
    alpha / m is the first and hardest of them, so this figure overstates the effect needed for every member except
    the one with the smallest p-value, which faces alpha / m itself; a member whose p-value ranks later is judged at a
    larger level and detects less than this. NaN when the SD is undefined or zero, when the estimator registered no
    p-values function to simulate with, or when the count of clusters is too small for the test to reach the target
    power at that level.
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


def _refuse_lost_treatment_pairs(spec: ContrastSpec, prereg: Preregistration, pairing: Pairing) -> None:
    """Refuse a member of the Holm family whose treatment attempts left it unseen (S10). "scored" is a claim checked
    here, not a switch that turns the check off, and a tolerance cannot cover every treatment attempt. "excluded" loses
    no pair this way, but may not leave the contrast with no pair, whose conclusion would read as no effect detected."""
    if spec.name not in prereg.holm_family:
        return
    if spec.treatment_outcomes == "excluded":
        removed = pairing.missing["treatment_excluded"]
        if removed and not pairing.n_pairs:
            raise TreatmentOutcomesRefused(
                f'{prereg.source}: confirmatory contrast {spec.name!r} declares treatment_outcomes = "excluded", which '
                f"leaves it no pair: all {removed} are treatment attempts, so nothing is estimated and its conclusion "
                'would read as no effect detected. Declare treatment_outcomes = "scored" and score them, or take the '
                "contrast out of holm_family"
            )
        return
    lost = pairing.missing["treatment_outcome_none"]
    if not lost:
        return
    if spec.treatment_outcomes == "scored":
        within = lost <= spec.max_unscored_treatment
        if within and pairing.n_treatment_measured:
            return
        unmeasured = "; no treatment attempt of the contrast was measured, which no tolerance excuses" if within else ""
        raise TreatmentOutcomesRefused(
            f'{prereg.source}: confirmatory contrast {spec.name!r} declares treatment_outcomes = "scored", but {lost} '
            f"pair(s) have a treatment attempt whose outcome {spec.outcome!r} is None "
            f"(max_unscored_treatment is {spec.max_unscored_treatment}{unmeasured}). Make the scorer return values for "
            f"treatment attempts; preregister max_unscored_treatment in [contrasts.{spec.name}] only for attempts "
            'that genuinely could not be measured; or declare treatment_outcomes = "excluded" to leave every pair '
            "with a treatment attempt out of this contrast, reported as such"
        )
    raise TreatmentOutcomesRefused(
        f"{prereg.source}: confirmatory contrast {spec.name!r} drops {lost} pair(s) because a treatment attempt's "
        f"outcome {spec.outcome!r} is None, so the condition's manipulation silently leaves its own contrast. Make the "
        "scorer return values for treatment attempts (None only for what could not be measured) and declare "
        f'treatment_outcomes = "scored" in [contrasts.{spec.name}], or declare treatment_outcomes = "excluded" there '
        "to leave every pair with a treatment attempt out of this contrast, reported as such"
    )


def _refuse_unmeasured_failures(spec: ContrastSpec, prereg: Preregistration, pairing: Pairing) -> None:
    """Refuse a member of the Holm family that lost a pair holding an ordinary run whose numerator is None while its
    denominator is 0 (S13): the ratio would then sum the cost of the runs that succeeded, and not of those that failed.
    Such a pair may be counted under a treatment attempt's None, which a tolerance excuses, so the count is its own."""
    lost = pairing.n_unmeasured_failures
    if spec.ratio is None or spec.name not in prereg.holm_family or not lost:
        return
    raise RatioNumeratorRefused(
        f"{prereg.source}: confirmatory ratio contrast {spec.name!r} drops {lost} pair(s) because a run's numerator "
        f"{spec.ratio.numerator!r} is None while its denominator {spec.ratio.denominator!r} is 0, so its ratio would "
        f"count the {spec.ratio.numerator!r} of the runs with a nonzero {spec.ratio.denominator!r} and leave out "
        f"that of the others. Make the scorer return {spec.ratio.numerator!r} for every run, a failed run's included, "
        "or take the contrast out of holm_family"
    )


def _check_form(spec: ContrastSpec) -> None:
    """Refuse a contrast whose form is not the one its estimator reads (S13): a spec built past the loader cannot hand
    a difference estimator log ratios, or the ratio estimator differences."""
    reads, form = estimators.form(spec.estimator), "difference" if spec.ratio is None else "ratio"
    if reads != form:
        raise ValueError(
            f"contrast {spec.name!r}: estimator {spec.estimator!r} reads a {reads} contrast, and this contrast is a "
            f"{form} contrast"
        )


def run_contrast(
    rows: Iterable[Row],
    spec: ContrastSpec,
    prereg: Preregistration,
    significant: bool | None = None,
    absent: Iterable[AbsentRun] = (),
) -> ContrastResult:
    """Pair the rows for one contrast and run its registered estimator under the preregistration's settings.

    `significant` is the decision of the test. `analyse` passes Holm's decision for a member of the family; left out,
    it is the raw p-value at alpha, with nothing adjusting for the family. `absent` are the planned runs with no
    analysed attempt, which say why a pair is missing. A member of the family that loses a pair to a treatment
    attempt scored None is refused (`TreatmentOutcomesRefused`), unless it declares `treatment_outcomes = "excluded"`
    or the loss is within a "scored" contrast's preregistered `max_unscored_treatment`; a ratio contrast in the family
    that loses one to a failed run's None numerator is refused too (`RatioNumeratorRefused`, S13).
    """
    _check_form(spec)
    pairing = pair_rows(rows, spec, absent)
    _refuse_lost_treatment_pairs(spec, prereg, pairing)
    _refuse_unmeasured_failures(spec, prereg, pairing)
    estimate = estimators.get(spec.estimator)(pairing.differences, prereg.estimator_settings(spec))
    in_family = spec.name in prereg.holm_family
    untestable = pairing.n_pairs == 0
    if untestable:  # no data: whatever the estimator returned is not evidence, and NaN enters Holm as p = 1 (S11)
        estimate = dataclasses.replace(estimate, estimate=math.nan, p_value=math.nan, ci_low=math.nan, ci_high=math.nan)
        significant = False
    if significant is None:
        significant = bool(rejects(estimate.p_value, prereg.alpha))  # False for a NaN p-value
    if untestable:
        interval, equivalent = (None if spec.equivalence_margin is None else (math.nan, math.nan)), False
    else:
        interval, equivalent = _equivalence(spec, prereg, pairing.differences)
    mde = None if untestable else _minimum_detectable_effect(spec, prereg, pairing.differences, in_family)
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
        treatment_outcomes=spec.treatment_outcomes,
        max_unscored_treatment=spec.max_unscored_treatment,
        pairs_missing=dict(pairing.missing),
        counts=dict(estimate.counts),
        per_model=estimate.per_model,
        in_family=in_family,
        exploratory=not in_family,
        significant=significant,
        equivalence_margin=spec.equivalence_margin,
        equivalence_interval=interval,
        equivalent=equivalent,
        conclusion=conclude(significant, equivalent, untestable),
        minimum_detectable_effect=mde,
        ratio=spec.ratio,
        estimation_unit=spec.estimation_unit,
        independent_unit=spec.cluster,
        n_cells=pairing.n_cells,
        cells_lost=dict(pairing.cells_lost),
    )


def analyse(rows: Iterable[Row], prereg: Preregistration, absent: Iterable[AbsentRun] = ()) -> AnalysisResult:
    """Run every declared contrast, then Holm over the declared family at the preregistered alpha.

    `absent` are the planned runs with no analysed attempt (exhausted, unfinished, left out), from the export.
    """
    rows, absent = list(rows), list(absent)
    results = tuple(run_contrast(rows, spec, prereg, absent=absent) for spec in prereg.contrasts.values())
    by_name = {result.name: result for result in results}
    p_values = [by_name[name].p_value for name in prereg.holm_family]
    outcome = kernel.holm(p_values, prereg.alpha)
    decisions = tuple(
        HolmDecision(name, p, adjusted, level, reject)
        for name, p, adjusted, level, reject in zip(
            prereg.holm_family, p_values, outcome.adjusted, outcome.levels, outcome.reject, strict=True
        )
    )
    rejected = {d.name: d.reject for d in decisions}  # an untestable member's NaN p never rejects
    decided = tuple(
        dataclasses.replace(
            r,
            significant=rejected[r.name],
            conclusion=conclude(rejected[r.name], r.equivalent, untestable=r.n_pairs == 0),
        )
        if r.name in rejected
        else r
        for r in results
    )
    return AnalysisResult(decided, decisions, prereg.alpha, prereg.manifest(), prereg.exploratory_only)


@dataclasses.dataclass(frozen=True)
class PowerResult:
    """A power table with the manifest entries of the preregistration it was computed under (P04)."""

    rows: tuple[dict, ...]
    manifest: Mapping[str, str]


def power_study(prereg: Preregistration, simulate: bool = True) -> PowerResult:
    """The power table of the preregistration's [power] settings, at alpha and at alpha divided by the family size;
    at alpha only when the preregistration has no family (S12).

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
