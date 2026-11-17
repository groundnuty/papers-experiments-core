# Provenance: new
"""The `## Read log` section of the report of a batch whose records carry the `read_log` instrument (O91, S20): the runs
whose code touched the read-log channel, and the sensitivity analysis without them. Built as the visibility section is
(`report_visibility`), so the guards read alike.

A batch is checked when one of its analysed records carries the `read_log` instrument
(`expcore.readlog.sensitivity`); the report of any other batch has no such section and is the text it always was. The
section says first that the scan is a heuristic over text and that nothing is prevented, then shows:

- per condition, the analysed runs and the flagged ones;
- one row per run that touched the channel: its run key and attempt, its condition, and its hits (kind, the fixed name
  of what was found, and the cell), each cut to a few with the number left, never the cell's own text;
- the sensitivity analysis: the preregistered contrasts again without the flagged runs, in the tables the primary
  analysis uses (rendered by the caller), or why there is none.
"""

from collections.abc import Callable, Sequence
from typing import Any

from expcore.readlog.flags import RunReadLog
from expcore.readlog.scan import KINDS
from expcore.readlog.sensitivity import ReadLogAnalysis
from expcore.report_notes import READ_LOG_NOTES, READ_LOG_SENSITIVITY_NOTES

LEFT_OUT = "the runs that touched the read-log channel"  # what the rerun's own tables leave out
CELL_ITEMS = 5  # hits shown in one cell before "and N more"


def _more(items: Sequence[str]) -> str:
    shown = ", ".join(items[:CELL_ITEMS]) or "none"
    return shown if len(items) <= CELL_ITEMS else f"{shown} and {len(items) - CELL_ITEMS} more"


def _hits(run: RunReadLog) -> str:
    hits = [f"`{h['kind']} {h['what']} (cell {h['cell']})`" for h in run.scan["hits"]]
    return _more(hits) + (" (cut)" if run.scan["hits_cut"] else "")


def _by_condition(analysis: ReadLogAnalysis, table: Callable[..., list[str]]) -> list[str]:
    conditions = sorted({run.condition for run in analysis.runs})
    rows = [
        [
            condition,
            sum(1 for run in analysis.runs if run.condition == condition),
            sum(1 for run in analysis.flagged if run.condition == condition),
        ]
        for condition in conditions
    ]
    return ["### Read-log channel by condition", "", *table(["Condition", "Analysed", "Flagged"], rows), ""]


def _flagged(analysis: ReadLogAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = ["### Runs that touched the channel", ""]
    if not analysis.flagged:
        return [*lines, "None: no analysed run's code names the channel or a way to it.", ""]
    rows = [[run.run_key, run.attempt, run.condition, _hits(run)] for run in analysis.flagged]
    return [*lines, *table(["Run key", "Attempt", "Condition", "Hits (kind, name, cell)"], rows), ""]


def read_log_lines(
    analysis: ReadLogAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The `## Read log` section, or nothing for a batch whose records carry no `read_log`. `table` renders a table;
    `sensitivity` renders an analysis result's contrasts in the primary analysis's own tables."""
    if analysis is None:
        return []
    counts = analysis.counts()
    named = ", ".join(f"{counts[kind]} {kind}" for kind in KINDS if counts[kind])
    lines = ["## Read log: runs that touched the channel (detected, not prevented)", "", *READ_LOG_NOTES, ""]
    lines.append(
        f"{counts['flagged']} of {counts['analysed']} analysed run(s) flagged"
        + (f" ({named}; a run may have several kinds of hit)." if named else ".")
    )
    lines += ["", *_by_condition(analysis, table), *_flagged(analysis, table)]
    lines += [f"### Sensitivity analysis: {LEFT_OUT}, left out", "", *READ_LOG_SENSITIVITY_NOTES, ""]
    if analysis.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({analysis.refused}).", ""]
    if analysis.sensitivity is None:
        return [*lines, "No run is flagged, so the analysis without them is the analysis above.", ""]
    return [*lines, f"Left out: {counts['flagged']} flagged run(s).", "", *sensitivity(analysis.sensitivity), ""]
