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
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from expcore.stats.estimators import PairedDifferences
from expcore.stats.rows import Row


@dataclass(frozen=True)
class ContrastSpec:
    """One preregistered contrast: what is compared, on which outcome, with which estimator, clustered by what.

    Named-condition form: `conditions = (A, B)`. Factor form: `factor` and `levels = (A, B)`. The contrast is A - B.
    """

    name: str
    outcome: str
    estimator: str
    cluster: str
    conditions: tuple[str, str] | None = None
    factor: str | None = None
    levels: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        """Require exactly one form, with two different arms."""
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
    n_pairs_missing: int  # pairs excluded because an outcome is missing on either side
    n_unpaired: int  # run keys present in one arm only
    n_clusters_dropped: int  # clusters seen in either arm that were left without a pair
    tasks_dropped: tuple[str, ...] = ()  # tasks with runs in one arm only, sorted

    @property
    def n_tasks_dropped(self) -> int:
        """How many tasks the contrast dropped because one of its arms has no run on them."""
        return len(self.tasks_dropped)


def _arm(row: Row, spec: ContrastSpec) -> int | None:
    """Return 0 for arm A, 1 for arm B, None for a row the contrast does not use."""
    if spec.factor is None:
        selector = row.condition
    else:
        if spec.factor not in row.factors:
            raise ValueError(f"contrast {spec.name!r}: {row.describe()} has no factor {spec.factor!r}")
        selector = str(row.factors[spec.factor])
    arms = spec.arms
    return arms.index(selector) if selector in arms else None


def _key(row: Row, spec: ContrastSpec) -> tuple:
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


def pair_rows(rows: Iterable[Row], spec: ContrastSpec) -> Pairing:
    """Pair the rows of the contrast's two arms and aggregate the differences A - B per cluster and model."""
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
    tasks_dropped = tuple(sorted({r.task for r in first.values()} ^ {r.task for r in second.values()}))
    cells: dict[tuple[str, str, str], list[float]] = {}  # (cluster, model, task) -> pair differences
    used = missing = 0
    for key in sorted(first.keys() & second.keys()):
        value_a, value_b = _value(first[key], spec), _value(second[key], spec)
        if value_a is None or value_b is None:
            missing += 1
            continue
        used += 1
        row = first[key]
        cells.setdefault((cluster_of_task[row.task], row.model, row.task), []).append(value_a - value_b)
    per_cell: dict[tuple[str, str], list[float]] = {}
    for (cluster, model, _task), differences in sorted(cells.items()):
        per_cell.setdefault((cluster, model), []).append(float(np.mean(differences)))
    clusters = sorted({cluster for cluster, _ in per_cell})
    models = sorted({model for _, model in per_cell})
    matrix = np.full((len(clusters), len(models)), math.nan)
    for (cluster, model), task_means in per_cell.items():
        matrix[clusters.index(cluster), models.index(model)] = np.mean(task_means)
    return Pairing(
        differences=PairedDifferences(tuple(clusters), tuple(models), matrix),
        n_pairs=used,
        n_pairs_missing=missing,
        n_unpaired=len(first.keys() ^ second.keys()),
        n_clusters_dropped=len(seen) - len(clusters),
        tasks_dropped=tasks_dropped,
    )
