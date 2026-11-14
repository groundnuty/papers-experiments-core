# Provenance: new
"""Pairing: rows to paired cluster differences, for a named-condition contrast or a factor contrast.

A named-condition contrast compares condition A with condition B and pairs rows on the run identity that two
conditions share: task, model and seed. A factor contrast names the factor that varies, takes two of its levels and
pairs on every other factor of the run key: task, model, seed and each other declared factor. The conditions and the
factors are those of the campaign's factor schema, which the preregistration is validated against when it is loaded;
rows carry them as the campaign export writes them. Pairs are aggregated in plain-mean stages: pairs to
(task, model), tasks to (cluster, model). A pair with a missing outcome (None or NaN) on either side is excluded
and counted, never read as 0 (R10, S05); a cluster left without any pair is dropped and counted (S03).

Two units are kept apart. The estimation unit (`ContrastSpec.estimation_unit`) is the cell one value is computed in:
for a contrast of one outcome always one model's cell of a cluster, ("model", cluster), the mean of its tasks' means.
The independent unit is the cluster, a value of the task attribute: the input holds one row per cluster and one
column per model, so the kernel's test runs on each cluster's equal-weight mean over its models' cells and its
bootstrap draws clusters with all their cells. No cell or run is ever a unit of the test. A cell seen in either arm
and left without a value is counted by cause (`Pairing.cells_lost`, `CELL_CAUSES`).

A condition may run on only some of the tasks (a chain design lists the tasks of an arm). A task that has no run in
one of the contrast's two arms is dropped from that contrast, named and counted in its pairing; it takes no part in
any other contrast, and the runs of the other conditions are not looked at.

Every pair the contrast could have had is used or missing for exactly one cause (`MISSING_CAUSES`), each counted,
zeros included (S10, C23). A pair is every pairing key that a run of either arm has, analysed or absent:
- `treatment_excluded`: the contrast declares `treatment_outcomes = "excluded"` and either run is a treatment attempt.
  The preregistration decides, not the scorer, so such a pair is left out whatever its outcomes and whatever became
  of the other run;
- `treatment_outcome_none`: an outcome is missing on a run that is a treatment attempt, whatever became of the other
  run of its planned pair. The condition's manipulation then leaves its own contrast, which a confirmatory contrast
  may not do (`expcore.stats.analysis`). A run on a task the other arm never planned has no pair to leave: it is
  `unpaired_or_left_out`, whatever it is;
- `outcome_none`: both runs are analysed and an outcome is missing on an ordinary run only;
- `exhausted`: the run of either arm used all its attempts on infrastructure failures;
- `unpaired_or_left_out`: anything else without both runs analysed: the other run is unfinished, left out of a
  partial analysis, or not in the plan (a task one arm does not run).
The absent runs come from the export (`AbsentRun`); without them every pair with one run analysed is
`unpaired_or_left_out` and a pair with neither is not seen.

A ratio contrast (S13, `ContrastSpec.ratio`) reads two outcomes of each run, a numerator and a denominator, and a run
misses its outcome when either is None. Its used pairs are summed per cell of its declared estimation unit into one
log ratio per cell (`expcore.stats.ratio`): by default a cell is a cluster, every task, model and seed of it
together; with `estimation_unit = ("model", attribute)` it is one model's cell of a cluster, so the zero rule
excludes cells, and a cluster is dropped only when every cell of it is lost. Either way the clusters are the units of
the test. Its missing pairs have three more causes, `ratio.RATIO_CAUSES`, after the five above:
- `numerator_none_at_zero`: no treatment attempt lost an outcome, and a run's numerator is None while its denominator
  is 0, a failed run whose cost was not measured (an ordinary None otherwise is `outcome_none`);
- `zero_denominator`, `zero_numerator`: the pair is in a cell the declared rule excludes for a zero sum.
A contrast of one outcome counts the five causes alone, so its outputs are what they were before ratio contrasts.
When the arms read different columns (`RatioForm.columns`), each run is read in its own arm's columns, found from the
run's condition or level (`_columns`), whatever reads it: its value, a failed run's None numerator, its arm's count.

Beside the pairs, each arm's own planned runs are counted (`Pairing.arm_runs`, S14): measured, or lost to a None
outcome (on a treatment attempt or another run) or to having no analysed attempt (exhausted, unfinished, left out),
whatever became of their partners, so a reader sees which arm lost the runs a pair count leaves unattributed.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np

from expcore.stats import ratio
from expcore.stats.armruns import ArmRuns, count_arm
from expcore.stats.estimators import PairedDifferences
from expcore.stats.ratio import RatioForm
from expcore.stats.rows import AbsentRun, Row

TREATMENT_OUTCOMES = ("scored", "excluded")  # how a contrast declares its treatment attempts enter it
MISSING_CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")
MODEL_KEY = "model"  # the run's model, as a field of an estimation unit
CELL_CAUSES = ("no_pair",)  # why a cell seen in either arm has no value; a ratio contrast adds ratio.ZERO_CAUSES
NONE_CAUSES = ("treatment_outcome_none", "outcome_none", "numerator_none_at_zero")  # pairs lost to a None outcome


@dataclass(frozen=True)
class ContrastSpec:
    """One preregistered contrast: what is compared, on which outcome, with which estimator, clustered by what.

    Named-condition form: `conditions = (A, B)`. Factor form: `factor` and `levels = (A, B)`. The contrast is A - B.
    `equivalence_margin`, in the outcome's units, is declared only for a contrast whose preregistration allows the
    conclusion that the two arms are equivalent. `treatment_outcomes` says how treatment attempts enter the contrast:
    "scored" (the scorer returns values for them), "excluded" (every pair with one is left out) or None (undeclared).
    `max_unscored_treatment` is how many pairs a "scored" member of the Holm family may still lose to a treatment
    attempt's None: 0 unless preregistered, for a rare genuine "could not be measured". `ratio` makes it a ratio
    contrast (S13): it compares each arm's numerator per unit of its denominator (`RatioForm.columns`, one pair of
    columns for both arms unless the arms declare their own), its `outcome` is the label of the two
    (`RatioForm.label`), and its margin is a natural-log ratio. `cluster` is the task attribute that clusters the
    contrast, the independent unit its test, bootstrap and minimum detectable effect count. `estimation_unit` is the
    cell one value is computed in, nested in a cluster: `("model", cluster)` for a contrast of one outcome, which
    averages each model's pairs apart; for a ratio contrast `(cluster,)`, its sums pooling the models of a cluster
    (the default), or `("model", cluster)`, its sums per model's cell, the cluster's value their equal-weight mean.
    """

    name: str
    outcome: str
    estimator: str
    cluster: str
    conditions: tuple[str, str] | None = None
    factor: str | None = None
    levels: tuple[str, str] | None = None
    equivalence_margin: float | None = None
    treatment_outcomes: str | None = None
    max_unscored_treatment: int = 0
    ratio: RatioForm | None = None
    estimation_unit: tuple[str, ...] | None = None  # None is the form's default

    def __post_init__(self) -> None:
        """Require exactly one form, with two different arms, a positive finite margin and a known treatment word; a
        ratio contrast's outcome is its form's label; the estimation unit is the model's cell of the cluster or, for
        a ratio contrast, the cluster alone."""
        self._check_estimation_unit()
        if self.ratio is not None:
            if not isinstance(self.ratio, RatioForm):
                raise ValueError(f"contrast {self.name!r}: ratio must be a RatioForm, got {type(self.ratio).__name__}")
            if self.outcome != self.ratio.label:
                raise ValueError(
                    f"contrast {self.name!r}: a ratio contrast's outcome must be {self.ratio.label!r}, its numerator "
                    f"over its denominator (each arm's, when the arms read different columns), got {self.outcome!r}"
                )
        margin = self.equivalence_margin
        number = isinstance(margin, int | float) and not isinstance(margin, bool)
        if margin is not None and not (number and math.isfinite(margin) and margin > 0):
            raise ValueError(f"contrast {self.name!r}: the equivalence margin must be a positive finite number")
        if self.treatment_outcomes is not None and self.treatment_outcomes not in TREATMENT_OUTCOMES:
            raise ValueError(
                f"contrast {self.name!r}: treatment_outcomes must be 'scored' or 'excluded', "
                f"got {self.treatment_outcomes!r}"
            )
        tolerance = self.max_unscored_treatment
        if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
            raise ValueError(f"contrast {self.name!r}: max_unscored_treatment must be a non-negative integer")
        if tolerance and self.treatment_outcomes != "scored":
            raise ValueError(f'contrast {self.name!r}: max_unscored_treatment needs treatment_outcomes = "scored"')
        if self.conditions is not None and (self.factor is not None or self.levels is not None):
            raise ValueError(f"contrast {self.name!r}: give conditions, or a factor with its levels, not both")
        if self.conditions is None and (self.factor is None or self.levels is None):
            raise ValueError(f"contrast {self.name!r}: give two conditions, or a factor with two levels")
        arms = self.arms
        if len(arms) != 2 or arms[0] == arms[1]:
            raise ValueError(f"contrast {self.name!r}: the two arms must be two different names")

    def _check_estimation_unit(self) -> None:
        """Hold the canonical unit: ("model", cluster) for a contrast of one outcome; (cluster,) by default or
        ("model", cluster) for a ratio contrast."""
        by_model = (MODEL_KEY, self.cluster)
        if self.ratio is None:
            unit = by_model if self.estimation_unit is None else tuple(self.estimation_unit)
            if unit != by_model:
                raise ValueError(
                    f"contrast {self.name!r}: a contrast of one outcome estimates per model's cell of its cluster, "
                    f"{by_model!r}, and averages the models' cells; got {unit!r}"
                )
        else:
            unit = (self.cluster,) if self.estimation_unit is None else tuple(self.estimation_unit)
            if unit not in ((self.cluster,), by_model):
                raise ValueError(
                    f"contrast {self.name!r}: estimation_unit must be ({self.cluster!r},) or {by_model!r}, the "
                    f"cluster alone or the model's cell of it; got {unit!r}"
                )
        object.__setattr__(self, "estimation_unit", unit)

    @property
    def by_model(self) -> bool:
        """Whether a value is computed in one model's cell of a cluster (a contrast of one outcome always is)."""
        return self.estimation_unit == (MODEL_KEY, self.cluster)

    @property
    def arms(self) -> tuple[str, ...]:
        """The names of arm A and arm B: conditions, or levels of the varying factor."""
        return tuple(self.conditions if self.conditions is not None else self.levels or ())


@dataclass(frozen=True)
class Pairing:
    """The paired differences of one contrast and what was left out, each count in the unit it names."""

    differences: PairedDifferences
    n_pairs: int  # pairs used
    n_treatment_measured: int  # treatment attempts with a value, of keys both arms planned, paired or not
    n_pairs_missing: int  # pairs lost because an outcome is missing on either side (NONE_CAUSES); may overlap
    # n_unpaired, since a treatment attempt left None with no analysed partner counts in both
    n_unpaired: int  # run keys analysed in one arm only
    n_clusters_dropped: int  # clusters seen in either arm that were left without a pair
    missing: Mapping[str, int]  # the pairs missing by each cause of MISSING_CAUSES (and RATIO_CAUSES), zeros included
    tasks_dropped: tuple[str, ...] = ()  # tasks with runs in one arm only, sorted
    # pairs both arms analysed, not left out by a treatment declaration, that hold an ordinary run whose numerator is
    # None at a denominator of 0, whatever single cause they are counted under (S13); 0 for a contrast of one outcome
    n_unmeasured_failures: int = 0
    n_cells: int = 0  # cells of the estimation unit with a value
    # cells seen in either arm and left without a value, by each cause of CELL_CAUSES (and ratio.ZERO_CAUSES), zeros
    # included
    cells_lost: Mapping[str, int] = field(default_factory=dict)
    arm_runs: tuple[ArmRuns, ...] = ()  # arm A's and arm B's own planned runs, measured or lost by cause (S14)

    @property
    def n_tasks_dropped(self) -> int:
        """How many tasks the contrast dropped because one of its arms has no run on them."""
        return len(self.tasks_dropped)


def _arm(row: Row | AbsentRun, spec: ContrastSpec) -> int | None:
    """Return 0 for arm A, 1 for arm B, None for a row the contrast does not use."""
    if spec.factor is None:
        selector = row.condition
    else:
        if spec.factor not in row.factors:
            raise ValueError(f"contrast {spec.name!r}: {row.describe()} has no factor {spec.factor!r}")
        selector = str(row.factors[spec.factor])
    arms = spec.arms
    return arms.index(selector) if selector in arms else None


def _key(row: Row | AbsentRun, spec: ContrastSpec) -> tuple:
    """The pairing key from the run identity: task, model and seed, and for a factor contrast the other factors."""
    base = (row.task, row.model, row.seed)
    if spec.factor is None:
        return base
    others = (name for name in row.factors if name not in ("task", "model", "seed", spec.factor))
    return (*base, tuple(sorted((name, str(row.factors[name])) for name in others)))


def _cluster(row: Row, spec: ContrastSpec, known: dict[str, str]) -> str:
    """Return the row's cluster, checking that every row of a task names the same one."""
    if spec.cluster not in row.attributes:
        raise ValueError(f"contrast {spec.name!r}: {row.describe()} has no cluster attribute {spec.cluster!r}")
    cluster = row.attributes[spec.cluster]
    if known.setdefault(row.task, cluster) != cluster:
        raise ValueError(f"contrast {spec.name!r}: task {row.task!r} belongs to two clusters by {spec.cluster!r}")
    return cluster


Value = float | tuple[float, float]  # a run's outcome: one number, or a ratio contrast's (numerator, denominator)


def _number(row: Row, spec: ContrastSpec, outcome: str) -> float | None:
    """Return one outcome of the row as a float, or None when it is missing; refuse anything that is not a number."""
    if outcome not in row.outcomes:
        raise ValueError(f"contrast {spec.name!r}: {row.describe()} has no outcome {outcome!r}")
    value = row.outcomes[outcome]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"contrast {spec.name!r}: outcome {outcome!r} of {row.describe()} must be a number or None")
    if math.isnan(value):
        return None
    if math.isinf(value):
        raise ValueError(f"contrast {spec.name!r}: outcome {outcome!r} of {row.describe()} is infinite")
    return float(value)


def _columns(row: Row, spec: ContrastSpec, form: RatioForm) -> tuple[str, str]:
    """The (numerator, denominator) columns a ratio contrast reads on this row: those of the row's own arm, found from
    the row itself (its condition, or its level of the varying factor), so no caller can read a run in the other
    arm's columns."""
    arm = _arm(row, spec)
    if arm is None:  # every row read here has passed `_arm`; one that has not is a caller's error
        raise ValueError(f"contrast {spec.name!r}: {row.describe()} is in neither of its arms")
    return form.columns(arm)


def _value(row: Row, spec: ContrastSpec) -> Value | None:
    """Return the row's outcome, or None when it is missing: its one number, or for a ratio contrast its numerator and
    denominator, in its own arm's columns, None when either is, each refused when negative."""
    if spec.ratio is None:
        return _number(row, spec, spec.outcome)
    names = _columns(row, spec, spec.ratio)
    numerator, denominator = (_number(row, spec, name) for name in names)
    for name, part in zip(names, (numerator, denominator), strict=True):
        if part is not None and part < 0:
            raise ValueError(
                f"contrast {spec.name!r}: outcome {name!r} of {row.describe()} is negative; a ratio contrast sums "
                "costs and counts, which never are"
            )
    return None if numerator is None or denominator is None else (numerator, denominator)


def _failed_unmeasured(row: Row, spec: ContrastSpec) -> bool:
    """A run of a ratio contrast whose denominator is 0 and whose numerator is None, both in its own arm's columns: a
    failed run's cost, unmeasured."""
    if spec.ratio is None:
        return False
    numerator, denominator = _columns(row, spec, spec.ratio)
    return _number(row, spec, numerator) is None and _number(row, spec, denominator) == 0


def _absent_by_arm(
    absent: Iterable[AbsentRun], spec: ContrastSpec, analysed: tuple[dict[tuple, Row], dict[tuple, Row]]
) -> tuple[dict[tuple, str], dict[tuple, str]]:
    """The reason of each absent run of the two arms by pairing key; a key held twice in one arm is refused."""
    reasons: tuple[dict[tuple, str], dict[tuple, str]] = ({}, {})
    for run in absent:
        arm = _arm(run, spec)
        if arm is None:
            continue
        key = _key(run, spec)
        if key in analysed[arm] or key in reasons[arm]:
            raise ValueError(
                f"contrast {spec.name!r}: two runs of {spec.arms[arm]!r} share one run key ({run.describe()})"
            )
        reasons[arm][key] = run.reason
    return reasons


def _paired_cause(row_a: Row, row_b: Row, spec: ContrastSpec) -> str | tuple[Value, Value]:
    """For a key both arms analysed: the cause it is missing for, or the two runs' values when it is used.

    A treatment attempt's None comes first, then a ratio contrast's failed run with no numerator, then any other None.
    """
    value_a, value_b = _value(row_a, spec), _value(row_b, spec)  # read first, so a bad value is refused either way
    if spec.treatment_outcomes == "excluded" and (row_a.treatment or row_b.treatment):
        return "treatment_excluded"
    if value_a is None or value_b is None:
        if (value_a is None and row_a.treatment) or (value_b is None and row_b.treatment):
            return "treatment_outcome_none"
        if _failed_unmeasured(row_a, spec) or _failed_unmeasured(row_b, spec):
            return "numerator_none_at_zero"
        return "outcome_none"
    return value_a, value_b


def _planned_treatments(rows: tuple[Row | None, Row | None], reasons: tuple[str | None, str | None]) -> list[Row]:
    """The treatment attempts of a key one arm analysed and the other planned without an analysed attempt. A key the
    other arm never planned (a task only one arm runs) gives none: it is outside the contrast's pairs."""
    if not any(row is None and reason is not None for row, reason in zip(rows, reasons, strict=True)):
        return []
    return [row for row in rows if row is not None and row.treatment]


def _unpaired_cause(
    rows: tuple[Row | None, Row | None], reasons: tuple[str | None, str | None], spec: ContrastSpec
) -> str:
    """For a key at most one arm analysed: the cause it is missing for. A treatment attempt comes first, as it does in
    a pair both arms analysed: the other run's fate must not hide that the manipulation left its contrast."""
    treatments = _planned_treatments(rows, reasons)
    if treatments and spec.treatment_outcomes == "excluded":
        return "treatment_excluded"
    if any(_value(row, spec) is None for row in treatments):
        return "treatment_outcome_none"
    return "exhausted" if "exhausted" in reasons else "unpaired_or_left_out"


def _aggregate(used: list[tuple[Row, tuple[Value, Value]]], cluster_of_task: Mapping[str, str]) -> PairedDifferences:
    """Plain means in stages: the differences A - B of a (cluster, model, task) cell, then the tasks of a (cluster,
    model)."""
    cells: dict[tuple[str, str, str], list[float]] = {}
    for row, (value_a, value_b) in used:
        cells.setdefault((cluster_of_task[row.task], row.model, row.task), []).append(value_a - value_b)
    per_cell: dict[tuple[str, str], list[float]] = {}
    for (cluster, model, _task), differences in sorted(cells.items()):
        per_cell.setdefault((cluster, model), []).append(float(np.mean(differences)))
    clusters = sorted({cluster for cluster, _ in per_cell})
    models = sorted({model for _, model in per_cell})
    matrix = np.full((len(clusters), len(models)), math.nan)
    for (cluster, model), task_means in per_cell.items():
        matrix[clusters.index(cluster), models.index(model)] = np.mean(task_means)
    return PairedDifferences(tuple(clusters), tuple(models), matrix)


def _ratio_input(
    used: list[tuple[Row, tuple[Value, Value]]], cluster_of_task: Mapping[str, str], form: RatioForm, by_model: bool
) -> tuple[PairedDifferences, dict[str, int], dict[str, int]]:
    """A ratio contrast's input: the used pairs of each cell of its estimation unit summed into one log ratio under
    the declared rule, a cluster's models together (`ratio.aggregate`) or each model's cell apart, one row per cluster
    (`ratio.aggregate_by_model`); and the pairs and the cells the rule excluded, by cause. Both runs of a pair share
    the model, which is part of every pairing key."""
    if not by_model:
        pooled: dict[str, list[tuple[ratio.Totals, ratio.Totals]]] = {}
        for row, values in used:
            pooled.setdefault(cluster_of_task[row.task], []).append(values)
        return ratio.aggregate(pooled, form)
    cells: dict[tuple[str, str], list[tuple[ratio.Totals, ratio.Totals]]] = {}
    for row, values in used:
        cells.setdefault((row.model, cluster_of_task[row.task]), []).append(values)
    return ratio.aggregate_by_model(cells, form)


def pair_rows(rows: Iterable[Row], spec: ContrastSpec, absent: Iterable[AbsentRun] = ()) -> Pairing:
    """Pair the rows of the contrast's two arms and aggregate the differences A - B per cluster and model, or for a
    ratio contrast the log ratio of each cell's sums, one row per cluster.

    `absent` are the planned runs with no analysed attempt; they say why a pair is missing and nothing else.
    """
    arms: tuple[dict[tuple, Row], dict[tuple, Row]] = ({}, {})
    cluster_of_task: dict[str, str] = {}
    seen: set[str] = set()  # the clusters seen in either arm
    cells: set[tuple[str, ...]] = set()  # the cells of the estimation unit seen in either arm
    for row in rows:
        arm = _arm(row, spec)
        if arm is None:
            continue
        cluster = _cluster(row, spec, cluster_of_task)
        seen.add(cluster)
        cells.add((row.model, cluster) if spec.by_model else (cluster,))
        key = _key(row, spec)
        if key in arms[arm]:
            raise ValueError(
                f"contrast {spec.name!r}: two rows of {spec.arms[arm]!r} share one run key ({row.describe()})"
            )
        arms[arm][key] = row
    first, second = arms
    reasons = _absent_by_arm(absent, spec, arms)
    missing = dict.fromkeys(MISSING_CAUSES + (ratio.RATIO_CAUSES if spec.ratio is not None else ()), 0)
    used_pairs: list[tuple[Row, tuple[Value, Value]]] = []
    paired, measured = first.keys() & second.keys(), 0
    unmeasured_failures = 0
    for key in sorted(paired):
        outcome = _paired_cause(first[key], second[key], spec)
        measured += sum(r.treatment and _value(r, spec) is not None for r in (first[key], second[key]))
        if isinstance(outcome, str):
            missing[outcome] += 1
            ordinary = (r for r in (first[key], second[key]) if not r.treatment)
            if outcome != "treatment_excluded" and any(_failed_unmeasured(r, spec) for r in ordinary):
                unmeasured_failures += 1  # counted apart, so a treatment tolerance cannot excuse it
            continue
        used_pairs.append((first[key], outcome))
    for key in (first.keys() | second.keys() | reasons[0].keys() | reasons[1].keys()) - paired:
        held, why = (first.get(key), second.get(key)), (reasons[0].get(key), reasons[1].get(key))
        missing[_unpaired_cause(held, why, spec)] += 1
        measured += sum(_value(row, spec) is not None for row in _planned_treatments(held, why))
    lost: dict[str, int] = {}
    if spec.ratio is None:
        differences = _aggregate(used_pairs, cluster_of_task)
    else:
        differences, excluded, lost = _ratio_input(used_pairs, cluster_of_task, spec.ratio, spec.by_model)
        missing.update(excluded)
    n_cells = int(np.count_nonzero(~np.isnan(differences.matrix)))
    cells_lost = {"no_pair": len(cells) - n_cells - sum(lost.values()), **lost}
    return Pairing(
        differences=differences,
        n_pairs=len(used_pairs) - sum(missing.get(cause, 0) for cause in ("zero_denominator", "zero_numerator")),
        n_treatment_measured=measured,
        n_pairs_missing=sum(missing.get(cause, 0) for cause in NONE_CAUSES),
        n_unpaired=len(first.keys() ^ second.keys()),
        n_clusters_dropped=len(seen) - len(differences.clusters),
        missing=missing,
        tasks_dropped=tuple(sorted({r.task for r in first.values()} ^ {r.task for r in second.values()})),
        n_unmeasured_failures=unmeasured_failures,
        n_cells=n_cells,
        cells_lost=cells_lost,
        arm_runs=_arm_runs(arms, reasons, spec),
    )


def _arm_runs(
    arms: tuple[dict[tuple, Row], dict[tuple, Row]],
    reasons: tuple[dict[tuple, str], dict[tuple, str]],
    spec: ContrastSpec,
) -> tuple[ArmRuns, ...]:
    """Each arm's planned runs whose pairing key the other arm plans too (analysed or absent), by what became of them
    (S14): the runs of the contrast's planned pairs. A run with no planned partner (a task the other arm never runs,
    or a factor contrast's run whose other factors no run of the other level shares) is outside every pair, and is
    neither counted nor read."""
    planned = [set(arms[side]) | set(reasons[side]) for side in (0, 1)]
    shared = planned[0] & planned[1]
    return tuple(
        count_arm(
            spec.arms[side],
            ((row.treatment, _value(row, spec) is not None) for key, row in arms[side].items() if key in shared),
            (reason for key, reason in reasons[side].items() if key in shared),
        )
        for side in (0, 1)
    )
