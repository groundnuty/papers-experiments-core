# Provenance: new
"""Pairing: rows to paired cluster differences, for a named-condition contrast or a factor contrast.

A named-condition contrast compares condition A with condition B and pairs rows on the run identity that two
conditions share: task, model and seed. A factor contrast names the factor that varies, takes two of its levels and
pairs on every other factor of the run key: task, model, seed and each other declared factor. The conditions and the
factors are those of the campaign's factor schema, which the preregistration is validated against when it is loaded;
rows carry them as the campaign export writes them. Pairs are aggregated in plain-mean stages: pairs to
(task, model), tasks to (cluster, model). A pair with a missing outcome (None or NaN) on either side is excluded
and counted, never read as 0 (R10, S05); a cluster left without any pair is dropped and counted (S03).

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

Beside the pairs, each arm's own planned runs are counted (`Pairing.arm_runs`, S14): measured, or lost to a None
outcome (on a treatment attempt or another run) or to having no analysed attempt (exhausted, unfinished, left out),
whatever became of their partners, so a reader sees which arm lost the runs a pair count leaves unattributed.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np

from expcore.stats.armruns import ArmRuns, count_arm
from expcore.stats.estimators import PairedDifferences
from expcore.stats.rows import AbsentRun, Row

TREATMENT_OUTCOMES = ("scored", "excluded")  # how a contrast declares its treatment attempts enter it
MISSING_CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")


@dataclass(frozen=True)
class ContrastSpec:
    """One preregistered contrast: what is compared, on which outcome, with which estimator, clustered by what.

    Named-condition form: `conditions = (A, B)`. Factor form: `factor` and `levels = (A, B)`. The contrast is A - B.
    `equivalence_margin`, in the outcome's units, is declared only for a contrast whose preregistration allows the
    conclusion that the two arms are equivalent. `treatment_outcomes` says how treatment attempts enter the contrast:
    "scored" (the scorer returns values for them), "excluded" (every pair with one is left out) or None (undeclared).
    `max_unscored_treatment` is how many pairs a "scored" member of the Holm family may still lose to a treatment
    attempt's None: 0 unless preregistered, for a rare genuine "could not be measured".
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

    def __post_init__(self) -> None:
        """Require exactly one form, with two different arms, a positive finite margin and a known treatment word."""
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
    n_pairs_missing: int  # pairs lost because an outcome is missing on either side (both None causes); may overlap
    # n_unpaired, since a treatment attempt left None with no analysed partner counts in both
    n_unpaired: int  # run keys analysed in one arm only
    n_clusters_dropped: int  # clusters seen in either arm that were left without a pair
    missing: Mapping[str, int]  # the pairs missing, by each cause of MISSING_CAUSES, zeros included
    tasks_dropped: tuple[str, ...] = ()  # tasks with runs in one arm only, sorted
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


def _value(row: Row, spec: ContrastSpec) -> float | None:
    """Return the row's outcome as a float, or None when it is missing; refuse anything that is not a number."""
    if spec.outcome not in row.outcomes:
        raise ValueError(f"contrast {spec.name!r}: {row.describe()} has no outcome {spec.outcome!r}")
    value = row.outcomes[spec.outcome]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(
            f"contrast {spec.name!r}: outcome {spec.outcome!r} of {row.describe()} must be a number or None"
        )
    if math.isnan(value):
        return None
    if math.isinf(value):
        raise ValueError(f"contrast {spec.name!r}: outcome {spec.outcome!r} of {row.describe()} is infinite")
    return float(value)


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


def _paired_cause(row_a: Row, row_b: Row, spec: ContrastSpec) -> tuple[str | None, float]:
    """For a key both arms analysed: the cause it is missing for, or None and the difference A - B when it is used."""
    value_a, value_b = _value(row_a, spec), _value(row_b, spec)  # read first, so a bad value is refused either way
    if spec.treatment_outcomes == "excluded" and (row_a.treatment or row_b.treatment):
        return "treatment_excluded", math.nan
    if value_a is None or value_b is None:
        treated = (value_a is None and row_a.treatment) or (value_b is None and row_b.treatment)
        return ("treatment_outcome_none" if treated else "outcome_none"), math.nan
    return None, value_a - value_b


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


def _aggregate(cells: Mapping[tuple[str, str, str], list[float]]) -> PairedDifferences:
    """Plain means in stages: the pairs of a (cluster, model, task) cell, then the tasks of a (cluster, model)."""
    per_cell: dict[tuple[str, str], list[float]] = {}
    for (cluster, model, _task), differences in sorted(cells.items()):
        per_cell.setdefault((cluster, model), []).append(float(np.mean(differences)))
    clusters = sorted({cluster for cluster, _ in per_cell})
    models = sorted({model for _, model in per_cell})
    matrix = np.full((len(clusters), len(models)), math.nan)
    for (cluster, model), task_means in per_cell.items():
        matrix[clusters.index(cluster), models.index(model)] = np.mean(task_means)
    return PairedDifferences(tuple(clusters), tuple(models), matrix)


def pair_rows(rows: Iterable[Row], spec: ContrastSpec, absent: Iterable[AbsentRun] = ()) -> Pairing:
    """Pair the rows of the contrast's two arms and aggregate the differences A - B per cluster and model.

    `absent` are the planned runs with no analysed attempt; they say why a pair is missing and nothing else.
    """
    arms: tuple[dict[tuple, Row], dict[tuple, Row]] = ({}, {})
    cluster_of_task: dict[str, str] = {}
    seen: set[str] = set()
    for row in rows:
        arm = _arm(row, spec)
        if arm is None:
            continue
        seen.add(_cluster(row, spec, cluster_of_task))
        key = _key(row, spec)
        if key in arms[arm]:
            raise ValueError(
                f"contrast {spec.name!r}: two rows of {spec.arms[arm]!r} share one run key ({row.describe()})"
            )
        arms[arm][key] = row
    first, second = arms
    reasons = _absent_by_arm(absent, spec, arms)
    missing = dict.fromkeys(MISSING_CAUSES, 0)
    cells: dict[tuple[str, str, str], list[float]] = {}  # (cluster, model, task) -> pair differences
    paired, used, measured = first.keys() & second.keys(), 0, 0
    for key in sorted(paired):
        cause, difference = _paired_cause(first[key], second[key], spec)
        measured += sum(r.treatment and _value(r, spec) is not None for r in (first[key], second[key]))
        if cause is not None:
            missing[cause] += 1
            continue
        used += 1
        row = first[key]
        cells.setdefault((cluster_of_task[row.task], row.model, row.task), []).append(difference)
    for key in (first.keys() | second.keys() | reasons[0].keys() | reasons[1].keys()) - paired:
        held, why = (first.get(key), second.get(key)), (reasons[0].get(key), reasons[1].get(key))
        missing[_unpaired_cause(held, why, spec)] += 1
        measured += sum(_value(row, spec) is not None for row in _planned_treatments(held, why))
    differences = _aggregate(cells)
    return Pairing(
        differences=differences,
        n_pairs=used,
        n_treatment_measured=measured,
        n_pairs_missing=missing["treatment_outcome_none"] + missing["outcome_none"],
        n_unpaired=len(first.keys() ^ second.keys()),
        n_clusters_dropped=len(seen) - len(differences.clusters),
        missing=missing,
        tasks_dropped=tuple(sorted({r.task for r in first.values()} ^ {r.task for r in second.values()})),
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
