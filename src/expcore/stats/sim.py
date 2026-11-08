# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/sim.py, blob 87b554dd03387cf667cdc77596a2308b48ba0a39
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Simulator of analysis rows with planted, independently specified effects.

DELIBERATELY INDEPENDENT of the analysis pipeline (S09): this file imports numpy and `expcore.stats.rows`, the shape of
a row, and nothing else (an import-graph test enforces it). The true effects are the *inputs* (`OutcomeSpec.means`),
stated on the outcome's own scale: a probability or a share for bounded outcomes, any real number for the others. All
noise is bounded (uniform), and a spec whose noise could push a probability or a share outside [0, 1] is refused, so
nothing is ever clipped and the true contrast of two conditions is the plain difference of the stated parameters:

    true(A - B) = mean_m [ mean[A] + effect[m][A] ] - mean_m [ mean[B] + effect[m][B] ]

Cluster shifts, model shifts and run noise are shared across conditions or mean-zero, so they cancel in expectation.
A run's outcome goes missing (None) with a stated probability that does not depend on the outcome.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

import numpy as np

from expcore.stats.rows import Row

KINDS = ("binary", "proportion", "real")
# A band that touches the boundary is valid; this only absorbs float rounding (1 - 0.8 is 0.19999999999999996).
BAND_TOLERANCE = 1e-12


@dataclasses.dataclass(frozen=True)
class OutcomeSpec:
    """One outcome's planted truth: `means` per condition, on the scale the kind names.

    `binary`: each run is 0 or 1, with the planted mean plus the shifts as its probability. `proportion`: a share in
    [0, 1], the planted mean plus the shifts plus uniform run noise. `real`: any real number, built the same way.
    `scale` multiplies the cluster, model and cluster-by-condition shifts for this outcome.
    """

    kind: str
    means: Mapping[str, float]
    run_halfwidth: float = 0.05
    scale: float = 1.0
    missing_probability: float = 0.0


@dataclasses.dataclass(frozen=True)
class SimSpec:
    """The design and the planted truth. `conditions` maps a condition name to its factor assignment (may be empty)."""

    conditions: Mapping[str, Mapping[str, str]]
    outcomes: Mapping[str, OutcomeSpec]
    models: tuple[str, ...] = ("model-a", "model-b", "model-c")
    seeds: tuple[int, ...] = (1, 2, 3)
    n_clusters: int = 25
    tasks_per_cluster: int = 2
    cluster_attribute: str = "group"
    # planted per-model heterogeneity: (outcome, model, condition) -> additive shift (default 0)
    model_effect: Mapping[tuple[str, str, str], float] = dataclasses.field(default_factory=dict)
    # nuisance shared across conditions (additive; cancels in every contrast)
    model_shift: float = 0.04  # half-width of a uniform per-model shift
    cluster_halfwidth: float = 0.08  # uniform cluster shift, shared across conditions
    cluster_condition_halfwidth: float = 0.08  # uniform cluster-by-condition shift: drives the SD of differences

    def effect(self, outcome: str, model: str, condition: str) -> float:
        """The planted shift of one model under one condition for one outcome."""
        return self.model_effect.get((outcome, model, condition), 0.0)

    def true_contrast(self, outcome: str, a: str, b: str) -> float:
        """The planted A - B contrast of an outcome, by plain arithmetic on the inputs."""
        means = self.outcomes[outcome].means

        def level(condition: str) -> float:
            return means[condition] + float(np.mean([self.effect(outcome, m, condition) for m in self.models]))

        return level(a) - level(b)

    def validate(self) -> None:
        """Refuse a spec whose planted truth could clip or that is malformed; every message names what to fix."""
        if not self.conditions or not self.outcomes or not self.models or not self.seeds:
            raise ValueError("a spec needs conditions, outcomes, models and seeds")
        if self.n_clusters < 1 or self.tasks_per_cluster < 1:
            raise ValueError("n_clusters and tasks_per_cluster must be at least 1")
        if min(self.model_shift, self.cluster_halfwidth, self.cluster_condition_halfwidth) < 0:
            raise ValueError("noise half-widths cannot be negative")
        for name, outcome in self.outcomes.items():
            self._validate_outcome(name, outcome)

    def _validate_outcome(self, name: str, outcome: OutcomeSpec) -> None:
        """Check one outcome: its kind, its conditions, and for bounded kinds that no probability could clip."""
        if outcome.kind not in KINDS:
            raise ValueError(f"outcome {name!r}: kind {outcome.kind!r} is not one of {', '.join(KINDS)}")
        if set(outcome.means) != set(self.conditions):
            raise ValueError(f"outcome {name!r}: means must name exactly the conditions {sorted(self.conditions)}")
        if outcome.run_halfwidth < 0 or outcome.scale < 0 or not 0.0 <= outcome.missing_probability < 1.0:
            raise ValueError(
                f"outcome {name!r}: run_halfwidth and scale cannot be negative; missing_probability is in [0, 1)"
            )
        if outcome.kind == "real":
            return
        shared = (self.model_shift + self.cluster_halfwidth + self.cluster_condition_halfwidth) * outcome.scale
        run = outcome.run_halfwidth if outcome.kind == "proportion" else 0.0
        for condition, mean in outcome.means.items():
            worst = max(abs(self.effect(name, m, condition)) for m in self.models)
            if not worst + shared + run <= min(mean, 1 - mean) + BAND_TOLERANCE:
                raise ValueError(
                    f"outcome {name!r}, condition {condition!r}: mean {mean} with its noise could leave [0, 1]; the planted truth would clip"
                )


def simulate(spec: SimSpec, rng: np.random.Generator) -> list[Row]:
    """Draw one row per cluster, task, condition, model and seed, with the planted effects and the stated noise."""
    spec.validate()
    uniform = rng.uniform
    clusters = [f"c{i:02d}" for i in range(spec.n_clusters)]
    shifts = {
        name: {
            "cluster": uniform(-spec.cluster_halfwidth, spec.cluster_halfwidth, spec.n_clusters),
            "model": {m: uniform(-spec.model_shift, spec.model_shift) for m in spec.models},
            "cluster_condition": {
                c: uniform(-spec.cluster_condition_halfwidth, spec.cluster_condition_halfwidth, spec.n_clusters)
                for c in spec.conditions
            },
        }
        for name in spec.outcomes
    }
    rows: list[Row] = []
    for i, cluster in enumerate(clusters):
        for j in range(spec.tasks_per_cluster):
            task = f"{cluster}-t{j}"
            for condition, assignment in spec.conditions.items():
                for model in spec.models:
                    for seed in spec.seeds:
                        outcomes = {
                            name: _draw(spec, rng, name, outcome, shifts[name], i, condition, model)
                            for name, outcome in spec.outcomes.items()
                        }
                        rows.append(
                            Row(
                                task=task,
                                model=model,
                                seed=seed,
                                outcomes=outcomes,
                                clusters={spec.cluster_attribute: cluster},
                                factors=dict(assignment),
                                condition=condition,
                            )
                        )
    return rows


def _draw(spec, rng, name, outcome, shifts, cluster, condition, model) -> float | None:
    """Draw one run's value of one outcome, or None when it goes missing (independently of the value)."""
    centre = outcome.means[condition] + spec.effect(name, model, condition)
    centre += outcome.scale * (
        shifts["cluster"][cluster] + shifts["cluster_condition"][condition][cluster] + shifts["model"][model]
    )
    if outcome.kind == "binary":
        value = float(rng.random() < centre)
    else:
        value = centre + rng.uniform(-outcome.run_halfwidth, outcome.run_halfwidth)
    # the draw above is made for every run, so the missing flag never changes what the other runs draw
    gone = rng.random() < outcome.missing_probability
    return None if gone else float(value)
