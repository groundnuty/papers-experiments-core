# Provenance: new
"""How the Markdown report (`expcore.report`) says the spend a kill took out of a store (O62).

A section "Spend lost in kills" follows the holds that needed an operator, with a row for each attempt, top-up and
probe charge of the export's `lost_in_kill`: what a killed dispatcher spent after its store's last copy, counted back
from its spend journal (`campaign.lost_spend`). An attempt shows its run, model, whether the journal saw it end, and its
counts and top-ups as journaled; a charge, its model and dollars. A batch merged from shards names each row's shard.
Such an attempt is no attempt of a run in the tables above: its run was run again.

An export without `lost_in_kill` has no such section and no such text, so a report keeps its bytes.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

HEADING = "### Spend lost in kills"
NOTES = [
    "Each row is spend a killed dispatcher made after its store's last copy, which the copy put back lacked, counted",
    "once from its spend journal and charged to the budget (its shard's share in a sharded campaign). An attempt whose",
    "end the journal did not see is charged its reserved bound; one that ended, its measured cost (its bound when its",
    "counts were not complete), its top-ups on top; USD charged is that figure when it was counted (`-`: a model",
    "without a price). A top-up or probe row is charged its dollars. A lost attempt is in none of the counts above:",
    "its run was run again.",
]
# With a Basis column (an export whose journal kept call logs, C05), how an attempt whose end was not seen is charged
BASIS_NOTES = [
    "Each row is spend a killed dispatcher made after its store's last copy, which the copy put back lacked, counted",
    "once from its spend journal and charged to the budget (its shard's share in a sharded campaign). An attempt whose",
    "end the journal did not see is charged by its journaled call log (Basis: measured, every request answered; or",
    "measured_plus_inflight, one request in flight at that request's bound), or its reserved bound (Basis: bound) when",
    "no log could be read; one that ended, its measured cost (its bound when its counts were not complete); top-ups on",
    "top; USD charged is that figure when it was counted (`-`: a model without a price). A top-up or probe row is",
    "charged its dollars. A lost attempt is in none of the counts above: its run was run again.",
]
UNKNOWN = "-"

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def _count(value: object) -> object:
    return UNKNOWN if value is None else value


def section(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for an export that lost nothing in a kill."""
    entries = export.get("lost_in_kill") or []
    if not entries:
        return []
    header = ["Kind", "Run", "Model", "Ended", "Input tokens", "Output tokens", "Top-ups", "USD charged"]
    rows = []
    for entry in entries:
        usd = UNKNOWN if entry["usd"] is None else f"{entry['usd']:.6f}"
        if entry["kind"] == "attempt":
            ended = "yes" if entry["ended"] else "no"
            counts = [_count(entry["input_tokens"]), _count(entry["output_tokens"]), entry["topups"]]
            rows.append(["attempt", entry["run_key"], entry["model"], ended, *counts, usd])
        else:
            blank = [UNKNOWN] * 4
            rows.append([entry["kind"], entry.get("run_key", UNKNOWN), entry["model"], *blank, usd])
    notes = NOTES
    if any("basis" in entry for entry in entries):  # an export from before the call log has no such column (C05)
        header.append("Basis")
        rows = [[*row, entry.get("basis", UNKNOWN)] for entry, row in zip(entries, rows, strict=True)]
        notes = BASIS_NOTES
    if any("shard" in entry for entry in entries):
        header.insert(0, "Shard")
        rows = [[entry.get("shard", UNKNOWN), *row] for entry, row in zip(entries, rows, strict=True)]
    return [HEADING, "", *notes, "", *table(header, rows), ""]
