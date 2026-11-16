# Provenance: new
"""The mount-guard part of an analysis: every analysed run's verdict, and the preregistered analysis again without the
runs that are not clean, as a sensitivity analysis. Built as the egress check's is (`expcore.egress.sensitivity`), so
the two read alike.

`mount_guard_analysis(runs, rows, absent, prereg)` takes each analysed run's record with its condition, the analysis
rows in the same order, the absent runs and the preregistration. It returns None when no analysed record carries the
`mount_guard` instrument: such a batch was not checked (a Docker host, whose agent never shares a uid with oneclient),
and its report is the text it always was. Once one record carries it, every analysed run is judged
(`expcore.mountguard.flags.judge`): a run whose record lacks the snapshot is unknown (`not_recorded`), never clean.

A campaign may mix data paths. A run whose record says it had no mount (`NoMount`: a REST run, a run with no data path)
is `not_applicable`: it had no mount exposure, so it stays in the rerun and is counted and shown as such; the guard is
listed for it with `expcore.mountguard.mount_guard_for`. Only that statement makes a run not applicable: a mount run
whose record lacks its snapshot is unknown and left out, as before.

The sensitivity analysis is `stats.analysis.analyse` over the rows of the clean runs, with each other run (flagged, or
unknown) passed as an absent run of reason `excluded`, so its pairs are counted as missing and its arm's runs as left
out. The bootstrap seed of each contrast is the preregistered one, so the rerun is as deterministic as the primary
analysis. The primary analysis stands; this one says whether its conclusions hold without the runs that touched the
mount's token or oneclient's process, or could not be shown not to. A rerun the kernel refuses keeps the primary
analysis and records the refusal. With every run clean there is no second analysis: it would be the primary one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.mountguard.flags import FLAGS, NOT_APPLICABLE, VERDICTS, RunMountGuard, judge
from expcore.mountguard.instrument import NAMESPACE
from expcore.stats.analysis import AnalysisResult, ContrastRefused, analyse
from expcore.stats.rows import AbsentRun, Row

LEFT_OUT_AS = "excluded"  # the reason a run that is not clean is passed to the sensitivity analysis with


@dataclass(frozen=True)
class MountGuardAnalysis:
    """Every analysed run's mount-guard verdict, in the export's order; the sensitivity analysis without the runs that
    are not clean (None when no run is left out or when it was refused); and why it was refused, if it was. A run the
    guard does not apply to is not left out."""

    runs: tuple[RunMountGuard, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None

    @property
    def left_out(self) -> tuple[RunMountGuard, ...]:
        return tuple(run for run in self.runs if run.left_out)

    def counts(self) -> dict[str, int]:
        """The analysed runs, the left-out ones, each verdict's runs (the runs with no mount, `not_applicable`, kept
        apart from `clean`), and the left-out ones by each flag (a run may have several)."""
        left, verdicts = self.left_out, (*VERDICTS, NOT_APPLICABLE)
        return (
            {"analysed": len(self.runs), "left_out": len(left)}
            | {verdict: sum(1 for run in self.runs if run.verdict == verdict) for verdict in verdicts}
            | {flag: sum(1 for run in left if flag in run.flags) for flag in FLAGS}
        )


def checked(records: Sequence[Any]) -> bool:
    """True when one record carries the mount-guard instrument, a statement of no mount included: the batch's runs were
    checked, and a record without it is judged."""
    return any(NAMESPACE in record.instruments for record in records)


def mount_guard_analysis(
    runs: Sequence[tuple[Any, str]], rows: Sequence[Row], absent: Sequence[AbsentRun], prereg: Any
) -> MountGuardAnalysis | None:
    """The mount-guard verdicts and the sensitivity analysis of a batch; None for a batch not checked."""
    if len(runs) != len(rows):
        raise ValueError(f"{len(runs)} analysed record(s) for {len(rows)} analysis row(s)")
    records = [record for record, _ in runs]
    if not checked(records):
        return None
    for (record, _), row in zip(runs, rows, strict=True):
        if record.run_key != row.run_key:
            raise ValueError(f"the record of {record.run_key!r} is paired with the row of {row.run_key!r}")
    verdicts = tuple(judge(record, condition) for record, condition in runs)
    out = {verdict.run_key for verdict in verdicts if verdict.left_out}
    if not out:
        return MountGuardAnalysis(verdicts, None)
    kept = [row for row in rows if row.run_key not in out]
    left = [AbsentRun(row.run_key, row.condition, row.factors, LEFT_OUT_AS) for row in rows if row.run_key in out]
    try:
        return MountGuardAnalysis(verdicts, analyse(kept, prereg, [*absent, *left]))
    except ContrastRefused as error:
        return MountGuardAnalysis(verdicts, None, str(error))
