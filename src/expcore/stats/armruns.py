# Provenance: new
"""What became of one arm's planned runs for one outcome (S14), for a contrast's two arms and a one-arm estimate.

A planned run of the arm is measured (an analysed attempt whose outcome is known) or lost for exactly one cause: its
outcome is None on a treatment attempt (`treatment_outcome_none`) or on another run (`outcome_none`, a scorer's "not
determined"), or it has no analysed attempt (`exhausted`, `unfinished`, or `excluded`, left out of a partial
analysis). This counts the arm's own runs, whatever became of their partners: a pair lost to one None counts as one
pair in the pairs table, and here as a loss in the arm that lost it. The finish rate is the share of the planned runs
with an analysed attempt, the population every outcome is defined over.

This file imports only the standard library.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

LOST_CAUSES = ("treatment_outcome_none", "outcome_none", "exhausted", "unfinished", "excluded")
ABSENT_CAUSES = ("exhausted", "unfinished", "excluded")  # the causes of a run with no analysed attempt


@dataclass(frozen=True)
class ArmRuns:
    """One arm's planned runs: `finished` have an analysed attempt, `measured` of them have the outcome, and `lost`
    gives every other run by cause, every cause of `LOST_CAUSES` in order, zeros included."""

    arm: str
    planned: int
    finished: int
    measured: int
    lost: Mapping[str, int]

    def __post_init__(self) -> None:
        """Require every cause, and counts in which each planned run is in exactly one place."""
        if tuple(self.lost) != LOST_CAUSES:
            raise ValueError(f"arm {self.arm!r}: the runs lost must give every cause, {', '.join(LOST_CAUSES)}")
        unknown = self.lost["treatment_outcome_none"] + self.lost["outcome_none"]
        absent = sum(self.lost[cause] for cause in ABSENT_CAUSES)
        if self.measured + unknown != self.finished or self.finished + absent != self.planned:
            raise ValueError(
                f"arm {self.arm!r}: {self.measured} measured, {self.finished} finished and the runs lost do not add up "
                f"to the {self.planned} planned"
            )

    @property
    def finish_rate(self) -> float:
        """The share of the planned runs that finished; NaN for an arm with no planned run, never 0."""
        return self.finished / self.planned if self.planned else math.nan


def count_arm(arm: str, analysed: Iterable[tuple[bool, bool]], absent: Iterable[str]) -> ArmRuns:
    """Count one arm's planned runs: `analysed` gives each analysed run's (treatment, outcome known), `absent` the
    reason of each planned run with no analysed attempt."""
    lost = dict.fromkeys(LOST_CAUSES, 0)
    finished = measured = 0
    for treatment, known in analysed:
        finished += 1
        if known:
            measured += 1
        else:
            lost["treatment_outcome_none" if treatment else "outcome_none"] += 1
    for reason in absent:
        if reason not in ABSENT_CAUSES:
            raise ValueError(
                f"arm {arm!r}: an absent run's reason must be one of {', '.join(ABSENT_CAUSES)}, got {reason!r}"
            )
        lost[reason] += 1
    return ArmRuns(arm, finished + sum(lost[cause] for cause in ABSENT_CAUSES), finished, measured, lost)
