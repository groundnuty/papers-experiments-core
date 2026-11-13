# Provenance: new
"""The ratio form of a contrast: an outcome per unit of another, such as tokens per correct result (S13).

A ratio contrast names a numerator outcome and a denominator outcome in place of one outcome. Its estimand is the
mean over clusters of the cluster's log ratio of ratios of sums,

    theta = mean over clusters c of  ln( (N_cA / D_cA) / (N_cB / D_cB) ),

where N_ca and D_ca are the sums of the numerator and of the denominator over every run of arm a in cluster c that
belongs to a used pair. It is a ratio of sums, not a mean of per-run ratios: a run whose denominator is 0 (a failed
run, with no correct result) has no ratio of its own, and its numerator still counts toward its arm's sum. exp(theta)
is the geometric mean over clusters of how many times arm A's numerator per unit of denominator is arm B's, so the log
scale makes "x2" and "x1/2" symmetric thresholds. The inference on theta is the statistics kernel's, on these
per-cluster log ratios (see `estimators.ratio`).

What a cluster is, the contrast declares (`ContrastSpec.cluster_key`), and the two keys are two estimands:
- the task attribute alone (the default, `cluster = "group"`): a cluster is a value of the attribute, and its sums run
  over every task, model and seed of it, so its models are pooled. A model with no correct result in the cluster
  still adds its cost there, and the models that spend the most weigh the most in its ratio. The input has one column,
  `POOLED` (`aggregate`);
- the model and the attribute (`cluster = ["model", "group"]`): a cluster is one model's cell of a value of the
  attribute, its sums run over that model's tasks and seeds only, and each cell is a unit of the test and of the
  bootstrap, as a power analysis by (model, instance) cell counts them. The cells of one value of the attribute are
  treated as independent of each other, which is that declared unit's assumption, not something the data show. The
  input has one row per cell and one column per model, the cell's log ratio in its own model's column and NaN in the
  others (`aggregate_by_model`), so each model's estimate is the mean of its own cells.

A cluster whose sum is 0 in either arm has an undefined (0 / 0, x / 0) or infinite (ln 0) log ratio. It is never set
to 0 or an infinity and never dropped silently: the preregistration declares one rule, `zero_denominator`, which
applies to each cluster of the declared key (to each (model, value) cell when the key names the model),
- "exclude": the cluster leaves this contrast, and each of its pairs is counted under its cause, `zero_denominator`
  (a denominator sum of 0 in either arm, checked first) or `zero_numerator` (a numerator sum of 0 in either arm);
- "continuity": a declared `continuity_correction` c > 0 is added to all four sums of every cluster, a zero or not,
  so theta becomes the mean of ln( ((N_cA + c) / (D_cA + c)) / ((N_cB + c) / (D_cB + c)) ). It is one fixed
  transformation of the data, not a choice made after seeing a zero, and so a corrected estimand: c is in each
  outcome's own units, and it moves a cluster's log ratio most where its sums are small, in either direction (arms of
  one ratio, 100 / 10 and 10 / 1, give 0.31 with c = 0.5, not 0).
The same rule covers a zero numerator sum, whose log ratio is minus infinity.

A pair lost because a run's numerator is None while its denominator is 0 is counted under `numerator_none_at_zero`:
a failed run's cost left unmeasured makes the ratio one over successful runs alone, which is what it exists to avoid.
The analysis refuses a confirmatory contrast that has any (`analysis.RatioNumeratorRefused`).
"""

from __future__ import annotations

import json
import math
import numbers
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from expcore.stats.estimators import POOLED, PairedDifferences

ZERO_RULES = ("exclude", "continuity")  # what a cluster with a zero sum does, as the preregistration declares
RATIO_CAUSES = ("numerator_none_at_zero", "zero_denominator", "zero_numerator")  # the missing causes it adds
__all__ = [
    "POOLED",
    "RATIO_CAUSES",
    "ZERO_RULES",
    "RatioForm",
    "Totals",
    "aggregate",
    "aggregate_by_model",
    "cell_label",
    "log_ratio",
]

Totals = tuple[float, float]  # (numerator, denominator) of one run, or the sums of an arm in a cluster


@dataclass(frozen=True)
class RatioForm:
    """What a ratio contrast compares: `numerator` per unit of `denominator`, and its declared rule for a zero sum.

    `continuity_correction` is given with `zero_denominator = "continuity"` and only then.
    """

    numerator: str
    denominator: str
    zero_denominator: str
    continuity_correction: float | None = None

    def __post_init__(self) -> None:
        """Require two different named outcomes, a declared rule, and a positive finite correction with its rule."""
        for role in ("numerator", "denominator"):
            value = getattr(self, role)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a ratio contrast's {role} must name an outcome, got {value!r}")
        if self.numerator == self.denominator:
            raise ValueError(
                f"a ratio's numerator and denominator must be two different outcomes, got {self.numerator!r}"
            )
        if self.zero_denominator not in ZERO_RULES:
            raise ValueError(
                f"zero_denominator must be one of {', '.join(map(repr, ZERO_RULES))}, got {self.zero_denominator!r}"
            )
        correction = self.continuity_correction
        if self.zero_denominator != "continuity":
            if correction is not None:
                raise ValueError('continuity_correction is declared only with zero_denominator = "continuity"')
            return
        if correction is None:
            raise ValueError('zero_denominator = "continuity" needs a continuity_correction')
        number = isinstance(correction, numbers.Real) and not isinstance(correction, bool)
        if not (number and math.isfinite(correction) and correction > 0):
            raise ValueError(f"continuity_correction must be a positive finite number, got {correction!r}")

    @property
    def label(self) -> str:
        """The contrast's outcome as tables show it: numerator over denominator."""
        return f"{self.numerator}/{self.denominator}"

    @property
    def correction(self) -> float:
        """The constant added to every sum: the declared correction, or 0 when zero sums are excluded."""
        return float(self.continuity_correction) if self.zero_denominator == "continuity" else 0.0


def log_ratio(
    arm_a: tuple[float | Fraction, float | Fraction], arm_b: tuple[float | Fraction, float | Fraction], form: RatioForm
) -> tuple[str | None, float]:
    """One cluster's value from its arms' sums: (None, ln of the ratio of ratios), or (cause, NaN) when the rule
    excludes it.

    The arithmetic is exact (rationals) up to the logarithm, so arms with one ratio give 0.0 exactly, never float noise
    a rank test would rank, and no sum, product or quotient can overflow on the way.
    """
    c = Fraction(form.correction)
    (na, da), (nb, db) = ((Fraction(n) + c, Fraction(d) + c) for n, d in (arm_a, arm_b))
    if da == 0 or db == 0:
        return "zero_denominator", math.nan
    if na == 0 or nb == 0:
        return "zero_numerator", math.nan
    ratio = (na * db) / (da * nb)
    return None, math.log(ratio.numerator) - math.log(ratio.denominator)


def _sums(pairs: Sequence[tuple[Totals, Totals]]) -> tuple[tuple[Fraction, Fraction], tuple[Fraction, Fraction]]:
    """Each arm's sums over the pairs, exact rationals: no float sum can overflow, and the order does not matter."""
    na, da, nb, db = (
        sum((Fraction(value) for value in column), Fraction(0))
        for column in zip(*(a + b for a, b in pairs), strict=True)
    )
    return (na, da), (nb, db)


def _values[K: (str, tuple[str, str])](
    cells: Mapping[K, Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[dict[K, float], dict[str, int]]:
    """Each cluster's log ratio, in the order of the sorted keys (an attribute's value, or a (model, value) cell),
    and the pairs the rule excluded by cause."""
    excluded = {"zero_denominator": 0, "zero_numerator": 0}
    values: dict[K, float] = {}
    for cluster, pairs in sorted(cells.items()):
        cause, value = log_ratio(*_sums(pairs), form)
        if cause is not None:
            excluded[cause] += len(pairs)
        else:
            values[cluster] = value
    return values, excluded


def aggregate(
    cells: Mapping[str, Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[PairedDifferences, dict[str, int]]:
    """The input of a ratio contrast's estimator under the default key, and the pairs the rule excluded by cause.

    `cells` maps a cluster to its used pairs, each the (numerator, denominator) of the run of arm A and of arm B, every
    model's together. A cluster the rule excludes is not a row, and its pairs are counted; the one column is `POOLED`,
    and the input is marked `pooled`.
    """
    values, excluded = _values(cells, form)
    if not values:
        return PairedDifferences((), (), np.empty((0, 0)), pooled=True), excluded
    clusters = tuple(values)
    return PairedDifferences(clusters, (POOLED,), np.array([[values[c]] for c in clusters]), pooled=True), excluded


def cell_label(model: str, cluster: str) -> str:
    """The name of a (model, cluster) cell among an input's clusters: the two as a JSON array, so no two cells share
    one whatever characters their names hold."""
    return json.dumps([model, cluster], ensure_ascii=False)


def aggregate_by_model(
    cells: Mapping[tuple[str, str], Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[PairedDifferences, dict[str, int]]:
    """The input of a ratio contrast's estimator under a (model, cluster) key, and the pairs the rule excluded by cause.

    `cells` maps a (model, cluster) cell to that model's used pairs in that cluster. Each cell the rule keeps is a row,
    named by `cell_label`, its log ratio in its model's column and NaN in every other; a cell the rule excludes is no
    row, and its pairs are counted. The columns are the models with a cell left, sorted, and the input is not
    `pooled`, whatever the models are called.
    """
    values, excluded = _values(cells, form)
    if not values:
        return PairedDifferences((), (), np.empty((0, 0))), excluded
    models = tuple(sorted({model for model, _ in values}))
    column = {model: j for j, model in enumerate(models)}
    matrix = np.full((len(values), len(models)), np.nan)
    for row, ((model, _), value) in enumerate(values.items()):
        matrix[row, column[model]] = value
    return PairedDifferences(tuple(cell_label(*cell) for cell in values), models, matrix), excluded
