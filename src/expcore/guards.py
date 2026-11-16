# Provenance: new
"""Both guards together: the preregistered analysis again without every run that the egress check or the mount guard
leaves out (#179, O60). It is the rerun a reader asks for of a batch checked by both, beside each guard's own.

`guards_analysis(rows, absent, prereg, egress, mount)` takes the analysis rows in the export's order, the absent runs,
the preregistration, and the two guards' analyses (`expcore.egress.sensitivity.egress_analysis`,
`expcore.mountguard.sensitivity.mount_guard_analysis`), whose verdicts are in that same order. It returns None unless
both guards checked the batch: with one alone, its own rerun is the combined one.

Each run is described by what each guard found: `clean`, `flagged` or `unknown` (for egress, a run flagged for an
unknown log alone is `unknown`; one with an outside connection or a static-scan hit is `flagged`), and for the mount
guard also `not_applicable` (a run with no mount, such as a REST run). A run is left out when either guard leaves it
out: flagged or unknown to either, so a run unknown to one guard and clean to the other is left out; **a guard that does
not apply to a run leaves it out for nothing**, so a run not applicable to the mount guard is left out only for
egress. Only `clean` and `not_applicable` keep a run, so a state this code does not know leaves it out. `by` says which
guard: `egress`, `mount` or `both`. The rerun is `stats.analysis.analyse` over the other rows, with
each left-out run passed as an absent run of reason `excluded`, as each guard's own rerun is; a rerun the kernel
refuses keeps the primary analysis and records why. With no run left out there is no second analysis.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.egress.flags import LOG_UNKNOWN
from expcore.egress.sensitivity import EgressAnalysis
from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.stats.analysis import AnalysisResult, ContrastRefused, analyse
from expcore.stats.rows import AbsentRun, Row

LEFT_OUT_AS = "excluded"  # the reason a left-out run is passed to the rerun with, as each guard's own rerun does
EGRESS, MOUNT, BOTH = "egress", "mount", "both"
CLEAN, FLAGGED, UNKNOWN, NOT_APPLICABLE = "clean", "flagged", "unknown", "not_applicable"
KEPT = (CLEAN, NOT_APPLICABLE)  # what a guard may say of a run it leaves in: any other state leaves the run out
COUNT_KEYS = ("analysed", "left_out", "egress_only", "mount_only", "both")


@dataclass(frozen=True)
class GuardedRun:
    """One run as both guards found it."""

    run_key: str
    attempt: int
    condition: str
    egress: str
    mount: str

    @property
    def by(self) -> str | None:
        """Which guard leaves the run out: `egress`, `mount`, `both`, or None for a run clean to both."""
        egress, mount = self.egress not in KEPT, self.mount not in KEPT
        return BOTH if egress and mount else EGRESS if egress else MOUNT if mount else None

    @property
    def left_out(self) -> bool:
        return self.by is not None


def _egress_state(flags: Sequence[str]) -> str:
    if not flags:
        return CLEAN
    return UNKNOWN if tuple(flags) == (LOG_UNKNOWN,) else FLAGGED


@dataclass(frozen=True)
class GuardsAnalysis:
    """Every analysed run as both guards found it, in the export's order; the rerun without the runs either leaves out
    (None when none is, or when it was refused); and why it was refused, if it was."""

    runs: tuple[GuardedRun, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None

    @property
    def left_out(self) -> tuple[GuardedRun, ...]:
        return tuple(run for run in self.runs if run.left_out)

    def _count(self, runs: Sequence[GuardedRun]) -> dict[str, int]:
        by = Counter(run.by for run in runs)
        left = len(runs) - by[None]
        return dict(zip(COUNT_KEYS, (len(runs), left, by[EGRESS], by[MOUNT], by[BOTH]), strict=True))

    def counts(self) -> dict[str, int]:
        """The analysed runs, the left-out ones, and those left out by egress alone, by the mount guard alone and by
        both."""
        return self._count(self.runs)

    def by_condition(self) -> dict[str, dict[str, int]]:
        """The same counts per condition, by name."""
        conditions = sorted({run.condition for run in self.runs})
        return {c: self._count([run for run in self.runs if run.condition == c]) for c in conditions}


def guards_analysis(
    rows: Sequence[Row],
    absent: Sequence[AbsentRun],
    prereg: Any,
    egress: EgressAnalysis | None,
    mount: MountGuardAnalysis | None,
) -> GuardsAnalysis | None:
    """The runs as both guards found them and the rerun without those either leaves out; None unless both checked."""
    if egress is None or mount is None:
        return None
    keys = [row.run_key for row in rows]
    if [r.run_key for r in egress.runs] != keys or [r.run_key for r in mount.runs] != keys:
        raise ValueError("the two guards' verdicts are not of the same runs as the analysis rows, in their order")
    runs = tuple(
        GuardedRun(e.run_key, e.attempt, e.condition, _egress_state(e.flags), m.verdict)
        for e, m in zip(egress.runs, mount.runs, strict=True)
    )
    out = {run.run_key for run in runs if run.left_out}
    if not out:
        return GuardsAnalysis(runs, None)
    kept = [row for row in rows if row.run_key not in out]
    left = [AbsentRun(row.run_key, row.condition, row.factors, LEFT_OUT_AS) for row in rows if row.run_key in out]
    try:
        return GuardsAnalysis(runs, analyse(kept, prereg, [*absent, *left]))
    except ContrastRefused as error:
        return GuardsAnalysis(runs, None, str(error))
