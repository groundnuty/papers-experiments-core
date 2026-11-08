# Provenance: new
"""The row the analysis consumes: one analysed run in the field names of the campaign export, with its outcomes.

This file imports only the standard library. The record simulator builds these rows, so the simulator and the
pipeline it checks share the shape of the data and nothing else (S09). The identity fields are the ones an
`analysed_attempts` entry of the campaign export carries (`run_key`, `condition`, `factors`); the outcomes come from
the scorers and the attributes from the study's tasks, and `Row.from_export` joins the three.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

BUILTIN_FACTORS = ("task", "model", "seed")


@dataclass(frozen=True)
class Row:
    """One analysed run, reduced to what a contrast needs.

    `run_key` is the canonical text of the run key and `condition` the name of the condition the run belongs to.
    `factors` holds every factor of the run key by name, the built-in `task`, `model` and `seed` included, as the
    export writes them. `outcomes` maps an outcome name to a number, or to None when the scorer could not compute it:
    unknown is None, never 0. `attributes` holds the task's attributes by name, from which a contrast takes its cluster.
    """

    run_key: str
    condition: str
    factors: Mapping[str, str | int]
    outcomes: Mapping[str, float | None]
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require the built-in factors, which every pairing key is made of, and a finite number or None per outcome."""
        missing = [name for name in BUILTIN_FACTORS if name not in self.factors]
        if missing:
            raise ValueError(f"run {self.run_key!r}: factors lack the built-in {', '.join(map(repr, missing))}")
        for name, value in self.outcomes.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise ValueError(
                    f"run {self.run_key!r}: outcome {name!r} must be a finite number or None, "
                    f"got {type(value).__name__}"
                )
            if not math.isfinite(value):
                raise ValueError(
                    f"run {self.run_key!r}: outcome {name!r} is not finite; a missing value is None, "
                    "never NaN or an infinity"
                )

    @property
    def task(self) -> str:
        """The task the run was on."""
        return str(self.factors["task"])

    @property
    def model(self) -> str:
        """The model the run used."""
        return str(self.factors["model"])

    @property
    def seed(self) -> str:
        """The run's seed as text, the way the run key writes it."""
        return str(self.factors["seed"])

    def describe(self) -> str:
        """Name the run for a message by its run key; outcomes are never echoed."""
        return f"run {self.run_key!r}"

    @classmethod
    def from_export(
        cls, entry: Mapping[str, Any], outcomes: Mapping[str, float | None], attributes: Mapping[str, str] | None = None
    ) -> Row:
        """Build a row from an `analysed_attempts` entry of the campaign export and the run's scores."""
        for name in ("run_key", "condition", "factors"):
            if name not in entry:
                raise ValueError(f"export entry has no {name!r}; it is not an analysed attempt of campaign-export-1")
        return cls(entry["run_key"], entry["condition"], dict(entry["factors"]), dict(outcomes), dict(attributes or {}))
