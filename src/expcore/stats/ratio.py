# Provenance: new
"""The ratio form of a contrast: an outcome per unit of another, such as tokens per correct result (S13).

A ratio contrast names a numerator outcome and a denominator outcome in place of one outcome. Its value in one cell of
its estimation unit is the log ratio of ratios of sums,

    ln( (N_A / D_A) / (N_B / D_B) ),

where N_a and D_a are the sums of the numerator and of the denominator over every run of arm a in the cell that
belongs to a used pair. It is a ratio of sums, not a mean of per-run ratios: a run whose denominator is 0 (a failed
run, with no correct result) has no ratio of its own, and its numerator still counts toward its arm's sum. The log
scale makes "x2" and "x1/2" symmetric thresholds.

The independent unit is the contrast's cluster, a task attribute, as for every contrast: the signed-rank test, the
cluster bootstrap and the minimum detectable effect count its values (groups). What one cell is, the contrast
declares (`ContrastSpec.estimation_unit`), and the two units are two estimands:
- the attribute alone (the default, `estimation_unit = ["group"]`): a cell is a group, and its sums run over every
  task, model and seed of it, so its models are pooled. A model with no correct result in the group still adds its
  cost there, and the models that spend the most weigh the most in its ratio. The input has one column, `POOLED`
  (`aggregate`);
- the model and the attribute (`estimation_unit = ["model", "group"]`): a cell is one model's part of a group, its
  sums run over that model's tasks and seeds only, and the group's value is the mean of its cells' log ratios, each
  model weighing the same. The cells of one group share its response to the treatment, so they are never units of the
  test or of the bootstrap: the input has one row per group and one column per model, a cell's log ratio in its
  model's column and NaN where the cell is lost (`aggregate_by_model`), which is the kernel's convention, so the test
  runs on the groups' values and the bootstrap draws groups with all their cells. Each model's estimate is the mean
  of its own cells, a breakdown with no test of its own.
The estimand is theta, the mean over groups of the group's value; exp(theta) is a geometric mean over groups of how
many times arm A's numerator per unit of denominator is arm B's.

A cell whose sum is 0 in either arm has an undefined (0 / 0, x / 0) or infinite (ln 0) log ratio. It is never set to
0 or an infinity and never dropped silently: the preregistration declares one rule, `zero_denominator`, which applies
to each cell of the estimation unit,
- "exclude": the cell leaves this contrast, and each of its pairs is counted under its cause, `zero_denominator` (a
  denominator sum of 0 in either arm, checked first) or `zero_numerator` (a numerator sum of 0 in either arm), and the
  cell under the same cause. A group keeps its other cells and drops only when every cell of it is lost;
- "continuity": a declared `continuity_correction` c > 0 is added to all four sums of every cell, a zero or not, so a
  cell's value becomes ln( ((N_A + c) / (D_A + c)) / ((N_B + c) / (D_B + c)) ). It is one fixed transformation of
  the data, not a choice made after seeing a zero, and so a corrected estimand: c is in each outcome's own units, and
  it moves a cell's log ratio most where its sums are small, in either direction (arms of one ratio, 100 / 10 and
  10 / 1, give 0.31 with c = 0.5, not 0).
The same rule covers a zero numerator sum, whose log ratio is minus infinity.

A pair lost because a run's numerator is None while its denominator is 0 is counted under `numerator_none_at_zero`:
a failed run's cost left unmeasured makes the ratio one over successful runs alone, which is what it exists to avoid.
The analysis refuses a confirmatory contrast that has any (`analysis.RatioNumeratorRefused`).
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from expcore.stats.estimators import POOLED, PairedDifferences

ZERO_RULES = ("exclude", "continuity")  # what a cell with a zero sum does, as the preregistration declares
ZERO_CAUSES = ("zero_denominator", "zero_numerator")  # why the rule excludes a cell, and so each of its pairs
RATIO_CAUSES = ("numerator_none_at_zero", *ZERO_CAUSES)  # the missing causes it adds
__all__ = [
    "POOLED",
    "RATIO_CAUSES",
    "ZERO_CAUSES",
    "ZERO_RULES",
    "RatioForm",
    "Totals",
    "aggregate",
    "aggregate_by_model",
    "log_ratio",
]

Totals = tuple[float, float]  # (numerator, denominator) of one run, or the sums of an arm in a cell


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
    """One cell's value from its arms' sums: (None, ln of the ratio of ratios), or (cause, NaN) when the rule excludes
    it.

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


Excluded = dict[str, int]  # a count by each of ZERO_CAUSES


def _values[K: (str, tuple[str, str])](
    cells: Mapping[K, Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[dict[K, float], Excluded, Excluded]:
    """Each cell's log ratio, in the order of the sorted keys (a group, or a (model, group) cell), and the pairs and
    the cells the rule excluded, by cause."""
    pairs, lost = dict.fromkeys(ZERO_CAUSES, 0), dict.fromkeys(ZERO_CAUSES, 0)
    values: dict[K, float] = {}
    for cell, used in sorted(cells.items()):
        cause, value = log_ratio(*_sums(used), form)
        if cause is not None:
            pairs[cause] += len(used)
            lost[cause] += 1
        else:
            values[cell] = value
    return values, pairs, lost


def aggregate(
    cells: Mapping[str, Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[PairedDifferences, Excluded, Excluded]:
    """The input of a ratio contrast's estimator when a cell is a group, and the pairs and the cells (groups) the rule
    excluded, by cause.

    `cells` maps a group to its used pairs, each the (numerator, denominator) of the run of arm A and of arm B, every
    model's together. A group the rule excludes is not a row, and its pairs are counted; the one column is `POOLED`,
    and the input is marked `pooled`.
    """
    values, pairs, lost = _values(cells, form)
    if not values:
        return PairedDifferences((), (), np.empty((0, 0)), pooled=True), pairs, lost
    groups = tuple(values)
    return PairedDifferences(groups, (POOLED,), np.array([[values[g]] for g in groups]), pooled=True), pairs, lost


def aggregate_by_model(
    cells: Mapping[tuple[str, str], Sequence[tuple[Totals, Totals]]], form: RatioForm
) -> tuple[PairedDifferences, Excluded, Excluded]:
    """The input of a ratio contrast's estimator when a cell is one model's part of a group, and the pairs and the
    cells the rule excluded, by cause.

    `cells` maps a (model, group) cell to that model's used pairs in that group. The input has one row per group with
    a cell left, sorted, and one column per model with a cell left, sorted: each kept cell's log ratio in its group's
    row and its model's column, NaN where the cell is lost or absent. A group is a row while any of its cells is kept,
    so the group's value, the kernel's equal-weight mean over the models with a cell, is the mean of its kept cells,
    and the test and the bootstrap take the groups, never the cells, as their units. The input is not `pooled`,
    whatever the models are called.
    """
    values, pairs, lost = _values(cells, form)
    if not values:
        return PairedDifferences((), (), np.empty((0, 0))), pairs, lost
    groups = tuple(sorted({group for _, group in values}))
    models = tuple(sorted({model for model, _ in values}))
    row, column = {g: i for i, g in enumerate(groups)}, {m: j for j, m in enumerate(models)}
    matrix = np.full((len(groups), len(models)), np.nan)
    for (model, group), value in values.items():
        matrix[row[group], column[model]] = value
    return PairedDifferences(groups, models, matrix), pairs, lost
