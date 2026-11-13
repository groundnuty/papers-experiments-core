# Provenance: new
"""One-arm estimates: a rate or share of an outcome, or a ratio of two outcomes' totals, within one arm (S14).

An arm is one condition or a declared set of conditions, and a preregistration names each estimate in
`[descriptives.<name>]`. It is descriptive: an estimate and an interval, with no test, no conclusion word and no place
in a Holm family. Its interval comes from the kernel's cluster bootstrap (clusters drawn with replacement, one draw for
every model, S04) at the bootstrap seed and a digest of its name, so it draws the same resamples whichever others run.

- A rate (`outcome`): the mean of the outcome over the arm's runs that have it, aggregated as a contrast's
  differences are (`expcore.stats.pairing`): the runs of a (task, model) are averaged, every seed and condition of the
  arm together; then the tasks of a (cluster, model); the models of a cluster, each with equal weight; and the
  clusters, each with equal weight. For an outcome that is 0 or 1 it is a share. The interval is the percentile
  interval of the resampled means, and each model's estimate and interval come from the same draws, NaN where fewer
  than half the replicates hold the model (S05). With all clusters at one value the interval is that point: a
  percentile interval cannot be wider than the data, so few clusters at a share of 0 or 1 give [0, 0] or [1, 1].
- A ratio of totals (`numerator`, `denominator`): theta = sum of the numerator / sum of the denominator over every run
  of the arm that has both, every cluster, task, model and seed together, so a cluster weighs by its runs. A run whose
  denominator is 0 still adds its numerator. Each replicate is the ratio of the drawn clusters' totals; a replicate
  whose drawn denominators sum to 0 is undefined (NaN, S05), never 0 or an infinity, and is counted. With every
  denominator 0 the estimate is NaN. The totals are summed exactly and the replicates scaled, so no sum overflows.

Every planned run of the arm is measured or lost, for exactly one cause (`expcore.stats.armruns`). A value that is not
a number, is infinite, or is negative in a ratio of totals, is refused naming the run and the outcome.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from expcore.stats import contrasts as kernel
from expcore.stats.armruns import ArmRuns, count_arm
from expcore.stats.estimators import EstimatorSettings, ModelEstimate
from expcore.stats.rows import AbsentRun, Row

RATE, RATIO_OF_TOTALS = "rate", "ratio_of_totals"


@dataclass(frozen=True)
class DescriptiveSpec:
    """One preregistered one-arm estimate: a rate of `outcome`, or the ratio of `numerator`'s total over
    `denominator`'s, over the runs of `conditions`, clustered by the task attribute `cluster`."""

    name: str
    conditions: tuple[str, ...]
    cluster: str
    outcome: str | None = None
    numerator: str | None = None
    denominator: str | None = None

    def __post_init__(self) -> None:
        """Require one condition at least, each once, and one form: an outcome, or two different outcomes."""
        if not self.conditions:
            raise ValueError(f"descriptive {self.name!r}: name one condition or more")
        if len(set(self.conditions)) != len(self.conditions):
            raise ValueError(f"descriptive {self.name!r}: name each condition once")
        ratio = (self.numerator, self.denominator)
        if self.outcome is not None and any(part is not None for part in ratio):
            raise ValueError(f"descriptive {self.name!r}: give an outcome, or a numerator and a denominator, not both")
        if self.outcome is None and (None in ratio or self.numerator == self.denominator):
            raise ValueError(f"descriptive {self.name!r}: give an outcome, or two different numerator and denominator")

    @property
    def kind(self) -> str:
        """`rate` or `ratio_of_totals`."""
        return RATE if self.outcome is not None else RATIO_OF_TOTALS

    @property
    def label(self) -> str:
        """The outcome as tables show it."""
        return self.outcome if self.outcome is not None else f"{self.numerator}/{self.denominator}"


@dataclass(frozen=True)
class DescriptiveResult:
    """A one-arm estimate with its interval and its runs; NaN where it cannot be computed, never 0."""

    name: str
    kind: str  # RATE or RATIO_OF_TOTALS
    outcome: str  # the outcome, or numerator/denominator
    conditions: tuple[str, ...]
    cluster: str
    estimate: float
    ci_low: float
    ci_high: float
    n_clusters: int  # clusters with a run that has the outcome
    n_runs: int  # runs that entered the estimate
    n_undefined_replicates: int  # replicates with no value (drawn denominators summing to 0)
    runs: ArmRuns
    per_model: tuple[ModelEstimate, ...]


def _number(row: Row, spec: DescriptiveSpec, outcome: str) -> float | None:
    """One outcome of the row, or None when missing; refuse anything else, and a negative part of a ratio."""
    if outcome not in row.outcomes:
        raise ValueError(f"descriptive {spec.name!r}: {row.describe()} has no outcome {outcome!r}")
    value = row.outcomes[outcome]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or math.isinf(value):
        raise ValueError(f"descriptive {spec.name!r}: outcome {outcome!r} of {row.describe()} must be a finite number")
    if math.isnan(value):
        return None
    if spec.kind == RATIO_OF_TOTALS and value < 0:
        raise ValueError(f"descriptive {spec.name!r}: outcome {outcome!r} of {row.describe()} is negative")
    return float(value)


def _value(row: Row, spec: DescriptiveSpec) -> float | tuple[float, float] | None:
    """The run's outcome, or for a ratio of totals its (numerator, denominator); None when any part is missing."""
    if spec.outcome is not None:
        return _number(row, spec, spec.outcome)
    numerator = _number(row, spec, spec.numerator or "")
    denominator = _number(row, spec, spec.denominator or "")
    return None if numerator is None or denominator is None else (numerator, denominator)


def _cluster(row: Row, spec: DescriptiveSpec, known: dict[str, str]) -> str:
    """The row's cluster, checking that every row of a task names the same one."""
    if spec.cluster not in row.attributes:
        raise ValueError(f"descriptive {spec.name!r}: {row.describe()} has no cluster attribute {spec.cluster!r}")
    cluster = row.attributes[spec.cluster]
    if known.setdefault(row.task, cluster) != cluster:
        raise ValueError(f"descriptive {spec.name!r}: task {row.task!r} belongs to two clusters by {spec.cluster!r}")
    return cluster


def _model_estimates(models, points, per_model: np.ndarray, settings: EstimatorSettings) -> tuple[ModelEstimate, ...]:
    """Each model's estimate and interval from the shared draws, with its undefined replicates counted."""
    estimates = []
    for column, model in enumerate(models):
        low, high, defined = kernel.percentile_interval(per_model[:, column], settings.level)
        estimates.append(ModelEstimate(model, float(points[column]), low, high, settings.replicates - defined))
    return tuple(estimates)


def _rate(used: list[tuple[str, Row, float]], settings: EstimatorSettings) -> tuple:
    """Staged plain means to a (cluster, model) matrix, then the kernel's cluster bootstrap of it."""
    cells: dict[tuple[str, str, str], list[float]] = {}
    for cluster, row, value in used:
        cells.setdefault((cluster, row.model, row.task), []).append(value)
    per_cell: dict[tuple[str, str], list[float]] = {}
    for (cluster, model, _task), values in sorted(cells.items()):
        per_cell.setdefault((cluster, model), []).append(float(np.mean(values)))
    clusters, models = sorted({c for c, _ in per_cell}), sorted({m for _, m in per_cell})
    matrix = np.full((len(clusters), len(models)), math.nan)
    for (cluster, model), task_means in per_cell.items():
        matrix[clusters.index(cluster), models.index(model)] = np.mean(task_means)
    boot = kernel.cluster_bootstrap(matrix, settings.replicates, np.random.default_rng(list(settings.seed)))
    low, high, defined = kernel.percentile_interval(boot.overall, settings.level)
    per_model = _model_estimates(models, boot.point_per_model, boot.per_model, settings)
    return boot.point, low, high, len(clusters), settings.replicates - defined, per_model


def _scaled(totals: np.ndarray) -> tuple[np.ndarray, Fraction]:
    """Exact totals as floats in [0, 1] and the scale that restores them, so no float sum of them overflows."""
    top = max(totals.flat, default=Fraction(0))
    if top == 0:
        return np.zeros(totals.shape), Fraction(0)
    return np.vectorize(lambda value: float(value / top), otypes=[float])(totals), top


def _ratio_draws(numerator: np.ndarray, denominator: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """The ratio of the drawn clusters' totals in each replicate, NaN where the drawn denominators sum to 0."""
    scaled_n, scale_n = _scaled(numerator)
    scaled_d, scale_d = _scaled(denominator)
    sums_d = scaled_d[indices].sum(axis=1)
    if scale_d == 0:
        return np.full(indices.shape[0], math.nan)
    factor = float(scale_n / scale_d)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(sums_d > 0, scaled_n[indices].sum(axis=1) / sums_d * factor, math.nan)


def _ratio_of_totals(used: list[tuple[str, Row, tuple[float, float]]], settings: EstimatorSettings) -> tuple:
    """Exact totals per (cluster, model), the ratio of their sums, and the cluster bootstrap of that ratio."""
    clusters = sorted({cluster for cluster, _, _ in used})
    models = sorted({row.model for _, row, _ in used})
    numerator = np.full((len(clusters), len(models)), Fraction(0), dtype=object)
    denominator = np.full((len(clusters), len(models)), Fraction(0), dtype=object)
    for cluster, row, (n, d) in used:
        place = clusters.index(cluster), models.index(row.model)
        numerator[place] += Fraction(n)
        denominator[place] += Fraction(d)
    indices = kernel.draw_clusters(np.random.default_rng(list(settings.seed)), settings.replicates, len(clusters))
    draws = _ratio_draws(numerator.sum(axis=1), denominator.sum(axis=1), indices)
    low, high, defined = kernel.percentile_interval(draws, settings.level)
    points, by_model = [], np.empty((settings.replicates, len(models)))
    for column in range(len(models)):
        total_d = denominator[:, column].sum()
        points.append(float(numerator[:, column].sum() / total_d) if total_d else math.nan)
        by_model[:, column] = _ratio_draws(numerator[:, column], denominator[:, column], indices)
    total_d = denominator.sum()
    point = float(numerator.sum() / total_d) if total_d else math.nan
    per_model = _model_estimates(models, points, by_model, settings)
    return point, low, high, len(clusters), settings.replicates - defined, per_model


def describe(
    rows: Iterable[Row], spec: DescriptiveSpec, settings: EstimatorSettings, absent: Iterable[AbsentRun] = ()
) -> DescriptiveResult:
    """The one-arm estimate of `spec` over the rows of its conditions; `absent` says why planned runs are missing."""
    known: dict[str, str] = {}
    analysed, used, keys = [], [], set()
    for row in sorted((r for r in rows if r.condition in spec.conditions), key=lambda r: r.run_key):
        if row.run_key in keys:
            raise ValueError(f"descriptive {spec.name!r}: two rows share one run key ({row.describe()})")
        keys.add(row.run_key)
        cluster, value = _cluster(row, spec, known), _value(row, spec)
        analysed.append((row.treatment, value is not None))
        if value is not None:
            used.append((cluster, row, value))
    gone = [run.reason for run in absent if run.condition in spec.conditions]
    runs = count_arm(" + ".join(spec.conditions), analysed, gone)
    if not used:  # nothing to estimate and nothing drawn
        estimate, low, high, n_clusters, undefined, per_model = math.nan, math.nan, math.nan, 0, 0, ()
    elif spec.kind == RATE:
        estimate, low, high, n_clusters, undefined, per_model = _rate(used, settings)
    else:
        estimate, low, high, n_clusters, undefined, per_model = _ratio_of_totals(used, settings)
    return DescriptiveResult(
        name=spec.name,
        kind=spec.kind,
        outcome=spec.label,
        conditions=spec.conditions,
        cluster=spec.cluster,
        estimate=float(estimate),
        ci_low=low,
        ci_high=high,
        n_clusters=n_clusters,
        n_runs=len(used),
        n_undefined_replicates=undefined,
        runs=runs,
        per_model=per_model,
    )
