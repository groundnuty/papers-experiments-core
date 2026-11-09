# Provenance: new
"""The row the analysis consumes: one analysed run in the field names of the campaign export, with its outcomes.

This file imports only the standard library. The record simulator builds these rows, so the simulator and the
pipeline it checks share the shape of the data and nothing else (S09). The identity fields are the ones an
`analysed_attempts` entry of the campaign export carries (`run_key`, `condition`, `factors`, `treatment`); the
outcomes come from the scorers and the attributes from the study's tasks, and `Row.from_export` joins the three.

A planned run with no analysed attempt is an `AbsentRun`: its identity, as the export lists it among the exhausted,
the unfinished or the left-out runs, and that reason. Pairing reads it only to say why a pair is missing (C23).
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

BUILTIN_FACTORS = ("task", "model", "seed")
ABSENT_REASONS = ("exhausted", "unfinished", "excluded")  # why a planned run has no analysed attempt


class _Identity:
    """What a run's identity says, for a class with `run_key` and `factors`: task, model, seed, a name for messages."""

    run_key: str
    factors: Mapping[str, str | int]

    def _require_builtin(self) -> None:
        missing = [name for name in BUILTIN_FACTORS if name not in self.factors]
        if missing:
            raise ValueError(f"run {self.run_key!r}: factors lack the built-in {', '.join(map(repr, missing))}")

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


def _require_entry(entry: Mapping[str, Any], names: tuple[str, ...], what: str) -> None:
    for name in names:
        if name not in entry:
            raise ValueError(f"export entry has no {name!r}; it is not {what} of campaign-export-1")


@dataclass(frozen=True)
class Row(_Identity):
    """One analysed run, reduced to what a contrast needs.

    `run_key` is the canonical text of the run key and `condition` the name of the condition the run belongs to.
    `factors` holds every factor of the run key by name, the built-in `task`, `model` and `seed` included, as the
    export writes them. `outcomes` maps an outcome name to a number, or to None when the scorer could not compute it:
    unknown is None, never 0. `attributes` holds the task's attributes by name, from which a contrast takes its cluster.
    `treatment` says that the run's scored attempt ended in an infrastructure failure its condition declares as its
    treatment; pairing counts a pair lost to such an attempt's None apart from any other (S10).
    """

    run_key: str
    condition: str
    factors: Mapping[str, str | int]
    outcomes: Mapping[str, float | None]
    attributes: Mapping[str, str] = field(default_factory=dict)
    treatment: bool = False

    def __post_init__(self) -> None:
        """Require the built-in factors, which every pairing key is made of, and a finite number or None per outcome."""
        self._require_builtin()
        if not isinstance(self.treatment, bool):
            raise ValueError(f"run {self.run_key!r}: treatment must be true or false")
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

    @classmethod
    def from_export(
        cls, entry: Mapping[str, Any], outcomes: Mapping[str, float | None], attributes: Mapping[str, str] | None = None
    ) -> Row:
        """Build a row from an `analysed_attempts` entry of the campaign export and the run's scores.

        The entry's `treatment` flag is required: a default would read every treatment attempt as an ordinary run.
        """
        _require_entry(entry, ("run_key", "condition", "factors", "treatment"), "an analysed attempt")
        return cls(
            entry["run_key"],
            entry["condition"],
            dict(entry["factors"]),
            dict(outcomes),
            dict(attributes or {}),
            entry["treatment"],
        )


@dataclass(frozen=True)
class AbsentRun(_Identity):
    """A planned run with no analysed attempt: its identity and why it has none, one of ABSENT_REASONS.

    `exhausted`: its charged infrastructure attempts reached the limit; `unfinished`: it is pending or stopped on a
    harness error; `excluded`: a partial analysis leaves its final attempt out.
    """

    run_key: str
    condition: str
    factors: Mapping[str, str | int]
    reason: str

    def __post_init__(self) -> None:
        """Require the built-in factors and a known reason."""
        self._require_builtin()
        if self.reason not in ABSENT_REASONS:
            raise ValueError(
                f"run {self.run_key!r}: reason must be one of {', '.join(map(repr, ABSENT_REASONS))}, "
                f"got {self.reason!r}"
            )

    @classmethod
    def from_export(cls, entry: Mapping[str, Any], reason: str) -> AbsentRun:
        """Build one from an entry of the export's `exhausted_runs`, `unfinished_runs` or `excluded_attempts`."""
        _require_entry(entry, ("run_key", "condition", "factors"), "a run")
        return cls(entry["run_key"], entry["condition"], dict(entry["factors"]), reason)
