# Provenance: new
"""The `## Process visibility` section of the report of a batch whose sandbox watched for hidden processes (O56): the
runs that are not clean, and the sensitivity analysis without them. Built as the mount guard's section is
(`report_mountguard`), so the guards read alike.

A batch is checked when one of its analysed records carries the `process_visibility` instrument
(`expcore.visibility.sensitivity`); the report of any other batch has no such section and is the text it always was.
The section shows:

- per condition, the analysed runs, each verdict's runs (clean, flagged, unknown), the runs that are not clean by each
  flag, and the runs whose memory reading is unknown: their limit condition is unknown, never within their limit;
- one row per run that is not clean: its run key and attempt, its condition, its verdict and flags, its memory reading
  and why it is unknown, the hidden processes and the cells killed for hidden memory, and the largest growth of
  memory that nothing visible explained, against the margin;
- the sensitivity analysis: the preregistered contrasts again without the runs that are not clean, in the tables the
  primary analysis uses (rendered by the caller), or why there is none.
"""

from collections import Counter
from collections.abc import Callable
from typing import Any

from expcore.report_notes import VISIBILITY_NOTES, VISIBILITY_SENSITIVITY_NOTES
from expcore.visibility.flags import FLAGS, VERDICTS, RunVisibility
from expcore.visibility.instrument import UNKNOWN
from expcore.visibility.sensitivity import VisibilityAnalysis

LEFT_OUT = "the runs that hid from the watch or whose memory is unknown"  # what the rerun's own tables leave out
CELL_ITEMS = 5  # reasons shown in one cell before "and N more"
FLAG_NAMES = ("Hidden process", "Hidden memory", "Memory unknown")


def _more(items: tuple[str, ...]) -> str:
    shown = ", ".join(items[:CELL_ITEMS]) or "-"
    return shown if len(items) <= CELL_ITEMS else f"{shown} and {len(items) - CELL_ITEMS} more"


def _bytes(value: Any) -> str:
    return "-" if value is None else str(value)


def _by_condition(seen: VisibilityAnalysis, table: Callable[..., list[str]]) -> list[str]:
    analysed = Counter(run.condition for run in seen.runs)
    by_verdict = {v: Counter(run.condition for run in seen.runs if run.verdict == v) for v in VERDICTS}
    by_flag = {flag: Counter(run.condition for run in seen.left_out if flag in run.flags) for flag in FLAGS}
    limit_unknown = Counter(run.condition for run in seen.runs if run.memory == UNKNOWN)
    rows = [
        [condition, analysed[condition], *(by_verdict[v][condition] for v in VERDICTS),
         *(by_flag[flag][condition] for flag in FLAGS), limit_unknown[condition]]
        for condition in sorted(analysed)
    ]  # fmt: skip
    header = ["Condition", "Analysed", "Clean", "Flagged", "Unknown", *FLAG_NAMES, "Limit condition unknown"]
    return ["### Process visibility by condition", "", *table(header, rows), ""]


def _row(run: RunVisibility) -> list[Any]:
    figures = run.figures
    grown = f"{_bytes(figures.get('unexplained_peak_bytes'))} / {_bytes(figures.get('margin_bytes'))}"
    limit = "unknown" if run.memory == UNKNOWN else run.memory
    return [
        run.run_key, run.attempt, run.condition, run.verdict, ", ".join(run.flags), limit, _more(run.unknown),
        run.hidden_process, run.hidden_memory, grown,
    ]  # fmt: skip


def _not_clean(seen: VisibilityAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = ["### Runs that are not clean", ""]
    if not seen.left_out:
        return [
            *lines,
            "None: no analysed run hid a process or its memory from the watch, and every run's memory was read.",
            "",
        ]
    header = ["Run key", "Attempt", "Condition", "Verdict", "Flags", "Memory (limit condition)", "Unknown because"]
    header += ["Hidden processes", "Cells killed for hidden memory", "Unexplained bytes (largest / margin)"]
    return [*lines, *table(header, [_row(run) for run in seen.left_out]), ""]


def visibility_lines(
    seen: VisibilityAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The section, or nothing for a batch not checked. `table` renders a table; `sensitivity` renders an analysis
    result's contrasts in the primary analysis's own tables."""
    if seen is None:
        return []
    n = seen.counts()
    lines = ["## Process visibility (detected, not prevented)", "", *VISIBILITY_NOTES, ""]
    lines.append(
        f"{n['left_out']} of {n['analysed']} analysed run(s) not clean: {n['flagged']} flagged ({n['hidden_process']} "
        f"with a hidden process, {n['hidden_memory']} killed for hidden memory) and {n['unknown']} unknown; "
        f"{n['memory_unknown_reading']} with an unknown memory reading, whose limit condition is unknown (a run may "
        "have several flags)."
    )
    lines += ["", *_by_condition(seen, table), *_not_clean(seen, table)]
    lines += [f"### Sensitivity analysis: {LEFT_OUT}, left out", "", *VISIBILITY_SENSITIVITY_NOTES, ""]
    if seen.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({seen.refused}).", ""]
    if seen.sensitivity is None:
        return [*lines, "Every run is clean, so the analysis without the others is the analysis above.", ""]
    return [*lines, f"Left out: {n['left_out']} run(s).", "", *sensitivity(seen.sensitivity), ""]
