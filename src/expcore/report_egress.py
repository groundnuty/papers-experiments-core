# Provenance: new
"""The `## Egress` section of the report of a batch checked for egress (#179): the flagged runs, and the sensitivity
analysis without them.

A batch is checked when one of its analysed records carries the `egress` instrument (`expcore.egress.sensitivity`);
the report of any other batch has no such section and is the text it always was. The section shows:

- per condition, the analysed runs, the flagged ones, and the flagged ones by each flag;
- under package indexes the campaign declared (O95), how many runs installed from them or named them, a class of its
  own that flags nothing;
- one row per flagged run: its run key and attempt, its condition, its flags, the state of its log and why it is
  unknown, its outside destinations (address and port), and its static-scan hits (kind, what, cell), each cut to a
  few with the number left;
- the sensitivity analysis: the preregistered contrasts again without the flagged runs, in the tables the primary
  analysis uses (rendered by the caller, so the two cannot differ in form), or why there is none.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.egress.flags import FLAGS, RunEgress
from expcore.egress.sensitivity import EgressAnalysis
from expcore.report_notes import EGRESS_NOTES, SENSITIVITY_NOTES

CELL_ITEMS = 5  # destinations or hits shown in one cell before "and N more"


def _more(items: Sequence[str]) -> str:
    shown = ", ".join(items[:CELL_ITEMS]) or "none"
    return shown if len(items) <= CELL_ITEMS else f"{shown} and {len(items) - CELL_ITEMS} more"


def _destination(entry: Mapping[str, Any]) -> str:
    address = entry.get("address", "?")
    host = f"[{address}]" if ":" in str(address) else str(address)
    return f"{host}:{entry.get('port', '?')}/{entry.get('type', '?')} x{entry.get('attempts', '?')}"


def _hits(run: RunEgress) -> str:
    hits = [f"{h['kind']} {h['what']} (cell {h['cell']})" for h in run.scan.get("hits", []) if h.get("flag")]
    cut = " (cut)" if run.scan.get("hits_cut") else ""
    return _more(hits) + cut


def _by_condition(egress: EgressAnalysis, table: Callable[..., list[str]]) -> list[str]:
    analysed = Counter(run.condition for run in egress.runs)
    flagged = Counter(run.condition for run in egress.flagged)
    by_flag = {flag: Counter(run.condition for run in egress.flagged if flag in run.flags) for flag in FLAGS}
    rows = [
        [condition, analysed[condition], flagged[condition], *(by_flag[flag][condition] for flag in FLAGS)]
        for condition in sorted(analysed)
    ]
    header = ["Condition", "Analysed", "Flagged", "Outside connection", "Log unknown", "Static-scan hit"]
    return ["### Egress by condition", "", *table(header, rows), ""]


def _flagged(egress: EgressAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = ["### Flagged runs", ""]
    if not egress.flagged:
        return [
            *lines,
            "None: every analysed run's log is intact, lists no outside destination, and its code no hit.",
            "",
        ]
    rows = [
        [
            run.run_key,
            run.attempt,
            run.condition,
            ", ".join(run.flags),
            run.log,
            ", ".join(run.unknown) or "-",
            _more([_destination(d) for d in run.outside]),
            _hits(run),
        ]
        for run in egress.flagged
    ]
    header = ["Run key", "Attempt", "Condition", "Flags", "Log", "Unknown because", "Outside destinations"]
    return [*lines, *table([*header, "Static-scan hits"], rows), ""]


def egress_lines(
    egress: EgressAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The section, or nothing for a batch not checked for egress. `table` renders a table; `sensitivity` renders an
    analysis result's contrasts in the primary analysis's own tables."""
    if egress is None:
        return []
    counts = egress.counts()
    lines = ["## Egress (detected, not prevented)", "", *EGRESS_NOTES, ""]
    lines.append(
        f"{counts['flagged']} of {counts['analysed']} analysed run(s) flagged: {counts['outside']} with an outside "
        f"connection, {counts['log_unknown']} with an unknown log, {counts['static_scan']} with a static-scan hit "
        "(a run may have several flags)."
    )
    if egress.package_indexes:
        lines.append(
            f"{counts['package_index']} analysed run(s) installed from or named the declared package indexes "
            f"({', '.join(egress.package_indexes)}): reported in their own class, not flagged for it, and kept by "
            "the reruns unless another flag leaves them out."
        )
    lines += ["", *_by_condition(egress, table), *_flagged(egress, table)]
    lines += ["### Sensitivity analysis: the flagged runs left out", "", *SENSITIVITY_NOTES, ""]
    if egress.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({egress.refused}).", ""]
    if egress.sensitivity is None:
        return [*lines, "No run is flagged, so the analysis without them is the analysis above.", ""]
    return [*lines, f"Left out: {counts['flagged']} flagged run(s).", "", *sensitivity(egress.sensitivity), ""]
