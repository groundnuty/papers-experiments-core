# Provenance: new
"""Every guard together: the preregistered analysis again without every run that any guard leaves out (#179, O60, O76).
It is the rerun a reader asks for of a batch checked by two or more guards, beside each guard's own.

`guards_analysis(rows, absent, prereg, egress, mount, visibility=None)` takes the analysis rows in the export's order,
the absent runs, the preregistration, and the guards' analyses (`expcore.egress.sensitivity.egress_analysis`,
`expcore.mountguard.sensitivity.mount_guard_analysis`, `expcore.visibility.sensitivity.visibility_analysis`), whose
verdicts are in that same order; a guard that did not check the batch is None. It returns None unless at least two
guards checked the batch: with one alone, its own rerun is the combined one.

Each run is described by what each guard that checked the batch found: `clean`, `flagged` or `unknown` (for egress, a
run flagged for an unknown log alone is `unknown`; one with an outside connection or a static-scan hit is `flagged`),
and, for the mount guard, `not_applicable` (a run with no mount, such as a REST run). A run is left out when any guard
leaves it out: flagged or unknown to any, so a run unknown to one guard and clean to the others is left out. **A guard
that does not apply to a run leaves it out for nothing**: only `clean` and `not_applicable` keep a run, so a state this
code does not know leaves it out. `left_by` names the guards that leave a run out, in the order egress, mount,
visibility; `by` says it in one word: the guard's name, `both` when the batch was checked by two guards and both leave
the run out, else the names joined by `+`. The rerun is `stats.analysis.analyse` over the other rows, with each
left-out run passed as an absent run of reason `excluded`, as each guard's own rerun is; a rerun the kernel refuses
keeps the primary analysis and records why. With no run left out there is no second analysis.
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
from expcore.visibility.sensitivity import VisibilityAnalysis

LEFT_OUT_AS = "excluded"  # the reason a left-out run is passed to the rerun with, as each guard's own rerun does
EGRESS, MOUNT, VISIBILITY = "egress", "mount", "visibility"
GUARDS = (EGRESS, MOUNT, VISIBILITY)  # the order guards are named in
BOTH, SEVERAL = "both", "several"  # `both`: both guards of a two-guard batch; `several`: two or more of three
CLEAN, FLAGGED, UNKNOWN, NOT_APPLICABLE = "clean", "flagged", "unknown", "not_applicable"
KEPT = (CLEAN, NOT_APPLICABLE)  # what a guard may say of a run it leaves in: any other state leaves the run out
LEGACY = (EGRESS, MOUNT)  # the two guards the combined rerun first covered: its text and keys are unchanged for them


@dataclass(frozen=True)
class GuardedRun:
    """One run as each guard that checked the batch found it; a guard that did not check it says None."""

    run_key: str
    attempt: int
    condition: str
    egress: str | None
    mount: str | None
    visibility: str | None = None

    @property
    def states(self) -> dict[str, str]:
        """What each guard that checked the batch found, by guard, in the order egress, mount, visibility."""
        found = {EGRESS: self.egress, MOUNT: self.mount, VISIBILITY: self.visibility}
        return {guard: state for guard, state in found.items() if state is not None}

    @property
    def left_by(self) -> tuple[str, ...]:
        """The guards that leave the run out: those whose state is not one that keeps it."""
        return tuple(guard for guard, state in self.states.items() if state not in KEPT)

    @property
    def by(self) -> str | None:
        """Which guards leave the run out: `egress`, `mount` or `visibility`; `both` for both guards of a batch two
        guards checked; the names joined by `+` for several of three; None for a run no guard leaves out."""
        left = self.left_by
        if not left:
            return None
        if len(left) == 1:
            return left[0]
        return BOTH if len(left) == len(self.states) == 2 else "+".join(left)

    @property
    def left_out(self) -> bool:
        return bool(self.left_by)


def _egress_state(flags: Sequence[str]) -> str:
    if not flags:
        return CLEAN
    return UNKNOWN if tuple(flags) == (LOG_UNKNOWN,) else FLAGGED


@dataclass(frozen=True)
class GuardsAnalysis:
    """Every analysed run as each guard that checked the batch found it, in the export's order; the rerun without the
    runs any guard leaves out (None when none is, or when it was refused); and why it was refused, if it was."""

    runs: tuple[GuardedRun, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None

    @property
    def guards(self) -> tuple[str, ...]:
        """The guards that checked the batch, in the order egress, mount, visibility (egress and mount for no run)."""
        return tuple(g for g in GUARDS if any(g in run.states for run in self.runs)) or LEGACY

    @property
    def legacy(self) -> bool:
        """Whether the batch was checked by egress and the mount guard alone, whose section and keys are unchanged."""
        return self.guards == LEGACY

    @property
    def left_out(self) -> tuple[GuardedRun, ...]:
        return tuple(run for run in self.runs if run.left_out)

    def count_keys(self) -> tuple[str, ...]:
        """The keys of `counts()`: the analysed runs, the left-out ones, those left out by each guard alone, and those
        left out by both guards of a batch of two (`both`) or by several of three (`several`)."""
        guards = self.guards
        return ("analysed", "left_out", *(f"{g}_only" for g in guards), BOTH if len(guards) == 2 else SEVERAL)

    def _count(self, runs: Sequence[GuardedRun]) -> dict[str, int]:
        left = [run.left_by for run in runs if run.left_by]
        alone = Counter(by[0] for by in left if len(by) == 1)
        counts = [len(runs), len(left), *(alone[g] for g in self.guards), sum(1 for by in left if len(by) > 1)]
        return dict(zip(self.count_keys(), counts, strict=True))

    def counts(self) -> dict[str, int]:
        """The analysed runs, the left-out ones, and those left out by each guard alone and by several guards."""
        return self._count(self.runs)

    def by_condition(self) -> dict[str, dict[str, int]]:
        """The same counts per condition, by name."""
        conditions = sorted({run.condition for run in self.runs})
        return {c: self._count([run for run in self.runs if run.condition == c]) for c in conditions}


def _states(
    egress: EgressAnalysis | None, mount: MountGuardAnalysis | None, visibility: VisibilityAnalysis | None
) -> dict[str, list[str]]:
    """Each guard that checked the batch's verdict of each run, in the rows' order."""
    found: dict[str, list[str]] = {}
    if egress is not None:
        found[EGRESS] = [_egress_state(run.flags) for run in egress.runs]
    if mount is not None:
        found[MOUNT] = [run.verdict for run in mount.runs]
    if visibility is not None:
        found[VISIBILITY] = [run.verdict for run in visibility.runs]
    return found


def guards_analysis(
    rows: Sequence[Row],
    absent: Sequence[AbsentRun],
    prereg: Any,
    egress: EgressAnalysis | None,
    mount: MountGuardAnalysis | None,
    visibility: VisibilityAnalysis | None = None,
) -> GuardsAnalysis | None:
    """The runs as each guard found them and the rerun without those any guard leaves out; None unless two or more
    guards checked the batch."""
    analyses = [found for found in (egress, mount, visibility) if found is not None]
    if len(analyses) < 2:
        return None
    keys = [row.run_key for row in rows]
    if any([run.run_key for run in found.runs] != keys for found in analyses):
        raise ValueError("the guards' verdicts are not of the same runs as the analysis rows, in their order")
    states = _states(egress, mount, visibility)
    runs = tuple(
        GuardedRun(
            first.run_key,
            first.attempt,
            first.condition,
            *(states[guard][i] if guard in states else None for guard in GUARDS),
        )
        for i, first in enumerate(analyses[0].runs)
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
