# Provenance: new
"""The process-visibility part of an analysis: every analysed run's verdict, and the preregistered analysis again
without the runs that are not clean, as a sensitivity analysis. Built as the mount guard's is
(`expcore.mountguard.sensitivity`), so the two read alike.

`visibility_analysis(runs, rows, absent, prereg)` takes each analysed run's record with its condition, the analysis
rows in the same order, the absent runs and the preregistration. It returns None when no analysed record carries the
`process_visibility` instrument: such a batch was not checked (a Docker host, whose container's cgroup keeps the
memory limit whatever `/proc` shows), and its report is the text it always was. Once one record carries it, every
analysed run is judged (`expcore.visibility.flags.judge`): a run whose record lacks the snapshot is unknown
(`not_recorded`), never clean.

The sensitivity analysis is `stats.analysis.analyse` over the rows of the clean runs, with each other run (flagged, or
unknown) passed as an absent run of reason `excluded`. The primary analysis stands; this one says whether its
conclusions hold without the runs whose code hid a process or its memory from the harness, or whose memory could not
be read. A rerun the kernel refuses keeps the primary analysis and records the refusal. With every run clean there is
no second analysis: it would be the primary one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.stats.analysis import AnalysisResult, ContrastRefused, analyse
from expcore.stats.rows import AbsentRun, Row
from expcore.visibility.flags import FLAGS, VERDICTS, RunVisibility, judge
from expcore.visibility.instrument import NAMESPACE, OVER, UNKNOWN, WITHIN

LEFT_OUT_AS = "excluded"  # the reason a run that is not clean is passed to the sensitivity analysis with
# the counts of runs by memory reading; `memory_unknown` is already the flag's count, of the runs left out
READING_KEYS = {WITHIN: "memory_within", OVER: "memory_over", UNKNOWN: "memory_unknown_reading"}


@dataclass(frozen=True)
class VisibilityAnalysis:
    """Every analysed run's process-visibility verdict, in the export's order; the sensitivity analysis without the
    runs that are not clean (None when every run is clean or when it was refused); and why it was refused, if it
    was."""

    runs: tuple[RunVisibility, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None

    @property
    def left_out(self) -> tuple[RunVisibility, ...]:
        return tuple(run for run in self.runs if run.left_out)

    def counts(self) -> dict[str, int]:
        """The analysed runs, the left-out ones, each verdict's runs, the left-out ones by each flag (a run may have
        several), and the runs by memory reading (`memory_within`, `memory_over`, `memory_unknown_reading`)."""
        left = self.left_out
        return (
            {"analysed": len(self.runs), "left_out": len(left)}
            | {verdict: sum(1 for run in self.runs if run.verdict == verdict) for verdict in VERDICTS}
            | {flag: sum(1 for run in left if flag in run.flags) for flag in FLAGS}
            | {key: sum(1 for run in self.runs if run.memory == reading) for reading, key in READING_KEYS.items()}
        )


def checked(records: Sequence[Any]) -> bool:
    """True when one record carries the process-visibility instrument: the batch's runs were checked."""
    return any(NAMESPACE in record.instruments for record in records)


def visibility_analysis(
    runs: Sequence[tuple[Any, str]], rows: Sequence[Row], absent: Sequence[AbsentRun], prereg: Any
) -> VisibilityAnalysis | None:
    """The process-visibility verdicts and the sensitivity analysis of a batch; None for a batch not checked."""
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
        return VisibilityAnalysis(verdicts, None)
    kept = [row for row in rows if row.run_key not in out]
    left = [AbsentRun(row.run_key, row.condition, row.factors, LEFT_OUT_AS) for row in rows if row.run_key in out]
    try:
        return VisibilityAnalysis(verdicts, analyse(kept, prereg, [*absent, *left]))
    except ContrastRefused as error:
        return VisibilityAnalysis(verdicts, None, str(error))
