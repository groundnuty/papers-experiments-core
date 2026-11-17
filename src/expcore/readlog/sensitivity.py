# Provenance: new
"""The read-log part of an analysis: every analysed run's verdict, and the preregistered analysis again without the
flagged runs, as a sensitivity analysis (O91, S20). Built as `expcore.visibility.sensitivity` is, so the two read alike.

`read_log_analysis(runs, rows, absent, prereg)` takes each analysed run's record with its condition, the analysis rows
in the same order, the absent runs and the preregistration. It returns None when no analysed record carries the
`read_log` instrument: such a batch did not use the channel, and its analysis and report are what they always were.
Once one record carries it, every analysed run is judged (`expcore.readlog.flags.judge`), by its code.

The sensitivity analysis is `stats.analysis.analyse` over the rows of the runs that are not flagged, with each flagged
run passed as an absent run of reason `excluded`, so its pairs are counted as missing (`unpaired_or_left_out`) and its
arm's runs as left out: the vocabulary of causes is the primary analysis's. The primary analysis stands; this one says
whether its conclusions hold without the runs whose code reached for the channel. A rerun the kernel refuses
(`ContrastRefused`) keeps the primary analysis and records the refusal. With no run flagged there is no second
analysis: it would be the primary one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.readlog.flags import RunReadLog, judge
from expcore.readlog.scan import KINDS, NAMESPACE
from expcore.stats.analysis import AnalysisResult, ContrastRefused, analyse
from expcore.stats.rows import AbsentRun, Row

LEFT_OUT_AS = "excluded"  # the reason a flagged run is passed to the sensitivity analysis with


@dataclass(frozen=True)
class ReadLogAnalysis:
    """Every analysed run's verdict, in the export's order; the sensitivity analysis without the flagged runs (None
    when no run is flagged or when it was refused); and why it was refused, if it was."""

    runs: tuple[RunReadLog, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None

    @property
    def flagged(self) -> tuple[RunReadLog, ...]:
        return tuple(run for run in self.runs if run.flagged)

    def counts(self) -> dict[str, int]:
        """The analysed runs, the flagged ones, and the flagged ones by each kind of hit (a run may have several)."""
        flagged = self.flagged
        return {"analysed": len(self.runs), "flagged": len(flagged)} | {
            kind: sum(1 for run in flagged if kind in run.kinds) for kind in KINDS
        }


def checked(records: Sequence[Any]) -> bool:
    """True when one record carries the `read_log` instrument: the batch's runs used the channel."""
    return any(NAMESPACE in record.instruments for record in records)


def read_log_analysis(
    runs: Sequence[tuple[Any, str]], rows: Sequence[Row], absent: Sequence[AbsentRun], prereg: Any
) -> ReadLogAnalysis | None:
    """The verdicts and the sensitivity analysis of a batch; None for a batch whose records carry no `read_log`.

    The analysis is `stats.analysis.analyse` over the rows of the runs that are not flagged, with each flagged run
    passed as an absent run of reason `excluded`: its pairs are counted as missing and its arm's runs as left out, the
    primary analysis's own vocabulary. It stands beside the primary analysis; a rerun the kernel refuses keeps the
    primary and records why."""
    if len(runs) != len(rows):
        raise ValueError(f"{len(runs)} analysed record(s) for {len(rows)} analysis row(s)")
    if not checked([record for record, _ in runs]):
        return None
    for (record, _), row in zip(runs, rows, strict=True):
        if record.run_key != row.run_key:
            raise ValueError(f"the record of {record.run_key!r} is paired with the row of {row.run_key!r}")
    verdicts = tuple(judge(record, condition) for record, condition in runs)
    out = {verdict.run_key for verdict in verdicts if verdict.flagged}
    if not out:
        return ReadLogAnalysis(verdicts, None)
    kept = [row for row in rows if row.run_key not in out]
    left = [AbsentRun(row.run_key, row.condition, row.factors, LEFT_OUT_AS) for row in rows if row.run_key in out]
    try:
        return ReadLogAnalysis(verdicts, analyse(kept, prereg, [*absent, *left]))
    except ContrastRefused as error:
        return ReadLogAnalysis(verdicts, None, str(error))
