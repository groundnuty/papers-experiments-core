# Provenance: new
"""The egress part of an analysis: every analysed run's verdict, and the preregistered analysis again without the
flagged runs, as a sensitivity analysis.

`egress_analysis(runs, rows, absent, prereg, package_indexes=())` takes each analysed run's record with its condition,
the analysis rows in the same order, the absent runs, the preregistration and the package indexes the campaign
declared (O95; none by default, and then nothing here changes). It returns None when no analysed record carries the
`egress` instrument: such a batch was not checked for egress (a Docker host, whose agent has no route out), and its
report is the text it always was. Once one record carries it, the batch is one whose runs were checked, and every
analysed run is judged (`expcore.egress.flags.judge`): a run whose record lacks the snapshot is flagged as unknown
(`not_recorded`), never clean.

The sensitivity analysis is `stats.analysis.analyse` over the rows of the runs that are not flagged, with each flagged
run passed as an absent run of reason `excluded`, so its pairs are counted as missing (`unpaired_or_left_out`) and its
arm's runs as left out: the vocabulary of causes is the primary analysis's. The bootstrap seed of each contrast is the
preregistered seed and the contrast's name, so the rerun is as deterministic as the primary analysis. The primary
analysis stands; this one says whether its conclusions hold without the flagged runs. A rerun the kernel refuses
(`ContrastRefused`, a treatment attempt's None, say) keeps the primary analysis and records the refusal. With no run
flagged there is no second analysis: it would be the primary one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from expcore.egress.flags import FLAGS, RunEgress, judge
from expcore.egress.instrument import NAMESPACE
from expcore.egress.package_index import PACKAGE_INDEX
from expcore.stats.analysis import AnalysisResult, ContrastRefused, analyse
from expcore.stats.rows import AbsentRun, Row

LEFT_OUT_AS = "excluded"  # the reason a flagged run is passed to the sensitivity analysis with


@dataclass(frozen=True)
class EgressAnalysis:
    """Every analysed run's egress verdict, in the export's order; the sensitivity analysis without the flagged ones
    (None when no run is flagged or when it was refused); and why it was refused, if it was."""

    runs: tuple[RunEgress, ...]
    sensitivity: AnalysisResult | None
    refused: str | None = None
    package_indexes: tuple[str, ...] = ()  # the indexes the campaign declared (O95); none: the counts are as they were

    @property
    def flagged(self) -> tuple[RunEgress, ...]:
        return tuple(run for run in self.runs if run.flagged)

    def counts(self) -> dict[str, int]:
        """The analysed runs, the flagged ones, and the flagged ones by each flag (a run may have several); under
        declared package indexes, also the runs with a `package_index` hit, flagged or not."""
        flagged = self.flagged
        counts = {"analysed": len(self.runs), "flagged": len(flagged)} | {
            flag: sum(1 for run in flagged if flag in run.flags) for flag in FLAGS
        }
        if self.package_indexes:
            counts[PACKAGE_INDEX] = sum(1 for run in self.runs if run.package_index)
        return counts


def checked(records: Sequence[Any]) -> bool:
    """True when one record carries the egress instrument: the batch's runs were checked for egress."""
    return any(NAMESPACE in record.instruments for record in records)


def egress_analysis(
    runs: Sequence[tuple[Any, str]],
    rows: Sequence[Row],
    absent: Sequence[AbsentRun],
    prereg: Any,
    *,
    package_indexes: Sequence[str] = (),
) -> EgressAnalysis | None:
    """The egress verdicts and the sensitivity analysis of a batch; None for a batch not checked for egress."""
    if len(runs) != len(rows):
        raise ValueError(f"{len(runs)} analysed record(s) for {len(rows)} analysis row(s)")
    records = [record for record, _ in runs]
    if not checked(records):
        return None
    for (record, _), row in zip(runs, rows, strict=True):
        if record.run_key != row.run_key:
            raise ValueError(f"the record of {record.run_key!r} is paired with the row of {row.run_key!r}")
    indexes = tuple(package_indexes)
    verdicts = tuple(judge(record, condition, package_indexes=indexes) for record, condition in runs)
    flagged = {verdict.run_key for verdict in verdicts if verdict.flagged}
    if not flagged:
        return EgressAnalysis(verdicts, None, package_indexes=indexes)
    kept = [row for row in rows if row.run_key not in flagged]
    left = [AbsentRun(row.run_key, row.condition, row.factors, LEFT_OUT_AS) for row in rows if row.run_key in flagged]
    try:
        return EgressAnalysis(verdicts, analyse(kept, prereg, [*absent, *left]), package_indexes=indexes)
    except ContrastRefused as error:
        return EgressAnalysis(verdicts, None, str(error), indexes)
