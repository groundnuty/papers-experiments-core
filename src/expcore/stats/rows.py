# Provenance: new
"""The row the analysis consumes: one attempt's identity, its task attributes and its named outcomes.

This file imports only the standard library. The record simulator builds these rows, so the simulator and the
pipeline it checks share the shape of the data and nothing else (S09).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Row:
    """One run, reduced to what a contrast needs.

    `task`, `model` and `seed` are built-in factors. `factors` holds the declared factors of the run key, and
    `condition` the name of the condition the run belongs to (a named-condition contrast needs it, a factor contrast
    does not). `clusters` holds the task's attributes by name, from which a contrast takes its cluster. `outcomes`
    maps an outcome name to a number, or to None when the scorer could not compute it: unknown is None, never 0.
    """

    task: str
    model: str
    seed: str | int
    outcomes: Mapping[str, float | None]
    clusters: Mapping[str, str] = field(default_factory=dict)
    factors: Mapping[str, str] = field(default_factory=dict)
    condition: str | None = None

    def describe(self) -> str:
        """Name the run for a message, by its built-in factors only; outcomes are never echoed."""
        return f"task {self.task!r}, model {self.model!r}, seed {self.seed!r}"
