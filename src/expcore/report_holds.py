# Provenance: new
"""How the Markdown report (`expcore.report`) says the holds a platform said only an operator could end (C11).

A section "Holds that needed an operator" follows the budget holds, with a row for each outage of the health gate
whose reason the platform led with (a failed heal's stuck pairs, O35): when it began, when a check passed (`open` when
the campaign ended with the gate closed), whether the wait had outlasted `max_defer_s`, whether a run was charged an
attempt for it, and the platform's reason. A batch merged from shards names the shard whose platform check met each.
Such a hold starts no run and leaves no attempt, so the tables of the infrastructure failures do not show it.

An export without `operator_holds` has no such section and no such text, so a report keeps its bytes.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.report_notes import HOLD_NOTES

HEADING = "### Holds that needed an operator"
OPEN = "open"  # the gate was still closed when the export was made

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def _yes(flag: object) -> str:
    return "yes" if flag else "no"


def section(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for an export without such a hold."""
    holds = export.get("operator_holds") or []
    if not holds:
        return []
    sharded = any("shard" in hold for hold in holds)
    header = ["Since (UTC)", "Until (UTC)", "Blocking", "Charged a run", "Reason"]
    rows = [
        [
            hold["start_utc"],
            hold["end_utc"] or OPEN,
            _yes(hold["blocking"]),
            _yes(hold["charged"]),
            hold["reason"],
        ]
        for hold in holds
    ]
    if sharded:
        header.insert(0, "Shard")
        rows = [[hold.get("shard", "-"), *row] for hold, row in zip(holds, rows, strict=True)]
    return [HEADING, "", *HOLD_NOTES, "", *table(header, rows), ""]
