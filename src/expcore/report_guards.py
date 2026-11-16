# Provenance: new
"""The `## Egress and mount isolation together` section of the report of a batch checked by both guards (#179, O60):
the runs either guard leaves out, and the analysis again without them, beside each guard's own section and rerun.

A batch gets the section only when its records carry both the `egress` and the `mount_guard` instruments
(`expcore.guards`); with one guard alone, that guard's rerun is the combined one, and the report is the text it was.
The section shows the runs left out per condition, by which guard left each out; one row per left-out run with what
each guard found; and the rerun in the tables the primary analysis uses (rendered by the caller), or why there is none.
"""

from collections.abc import Callable
from typing import Any

from expcore.guards import COUNT_KEYS, GuardedRun, GuardsAnalysis
from expcore.report_notes import GUARDS_NOTES, GUARDS_SENSITIVITY_NOTES

LEFT_OUT = "the runs either guard leaves out"  # what the rerun's own tables say they leave out
BY_NAMES = {"egress": "egress", "mount": "mount guard", "both": "both"}


def _by_condition(guards: GuardsAnalysis, table: Callable[..., list[str]]) -> list[str]:
    rows = [[condition, *(counts[key] for key in COUNT_KEYS)] for condition, counts in guards.by_condition().items()]
    header = ["Condition", "Analysed", "Left out", "By egress alone", "By the mount guard alone", "By both"]
    return ["### Left out by either guard, by condition", "", *table(header, rows), ""]


def _row(run: GuardedRun) -> list[Any]:
    return [run.run_key, run.attempt, run.condition, run.egress, run.mount, BY_NAMES[str(run.by)]]


def _left_out(guards: GuardsAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = ["### Runs either guard leaves out", ""]
    if not guards.left_out:
        return [*lines, "None: every analysed run is clean to both guards.", ""]
    header = ["Run key", "Attempt", "Condition", "Egress", "Mount guard", "Left out by"]
    return [*lines, *table(header, [_row(run) for run in guards.left_out]), ""]


def guards_lines(
    guards: GuardsAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The section, or nothing for a batch not checked by both guards. `table` renders a table; `sensitivity` renders
    an analysis result's contrasts in the primary analysis's own tables."""
    if guards is None:
        return []
    n = guards.counts()
    lines = ["## Egress and mount isolation together (detected, not prevented)", "", *GUARDS_NOTES, ""]
    lines.append(
        f"{n['left_out']} of {n['analysed']} analysed run(s) left out by either guard: {n['egress_only']} by egress "
        f"alone, {n['mount_only']} by the mount guard alone, {n['both']} by both."
    )
    lines += ["", *_by_condition(guards, table), *_left_out(guards, table)]
    lines += [f"### Sensitivity analysis: {LEFT_OUT}, left out", "", *GUARDS_SENSITIVITY_NOTES, ""]
    if guards.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({guards.refused}).", ""]
    if guards.sensitivity is None:
        return [*lines, "Every run is clean to both guards, so the analysis without the others is the one above.", ""]
    return [*lines, f"Left out: {n['left_out']} run(s).", "", *sensitivity(guards.sensitivity), ""]
