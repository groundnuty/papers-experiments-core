# Provenance: new
"""How the Markdown report (`expcore.report`) says what the budget charged the attempts a kill interrupted (C05).

A section "Interrupted attempts' spend" follows the spend lost in kills, with a row per model of the export's
`interrupted_spend`: how many of its interrupted attempts were charged on each basis. `measured`: every request was
answered, and the replies' counts were charged at the model's price; `measured_plus_inflight`: one request was sent
and not answered, charged at one request's bound on top; `bound`: the attempt's whole bound, because no call log could
be read or trusted. A batch merged from shards has a Shard column.

An export without `interrupted_spend` has no such section and no such text, so a report keeps its bytes.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.interrupted_spend import BASES, EXPORT_KEY

HEADING = "### Interrupted attempts' spend"
NOTES = [
    "Each row counts the attempts of a model that a kill interrupted, by what the budget charged them. Measured: every",
    "request the attempt sent was answered, and the replies' counts were charged at the model's price. Measured plus",
    "in flight: one request was sent and not answered, and was charged the most one request can cost on top. Bound:",
    "the most the attempt's caps let it cost, because no call log could be read or trusted (no budget, an attempt from",
    "before the log, a reply that left a count out, or a store copied back whose own spend journal was not counted).",
    "Top-ups are charged on top of each. An interrupted attempt is in none of the scores: its run was run again.",
]
LABELS = {"measured": "Measured", "measured_plus_inflight": "Measured plus in flight", "bound": "Bound"}

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def section(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for an export without the key."""
    entries = export.get(EXPORT_KEY) or []
    if not entries:
        return []
    sharded = any("shard" in entry for entry in entries)
    counts: dict[tuple[Any, ...], dict[str, int]] = {}
    for entry in entries:
        cell = (entry.get("shard"), entry["model"]) if sharded else (entry["model"],)
        found = counts.setdefault(cell, dict.fromkeys(BASES, 0))
        found[entry["basis"]] = found.get(entry["basis"], 0) + 1
    header = [*(["Shard"] if sharded else []), "Model", *(LABELS[b] for b in BASES)]
    rows = [[*cell, *(found[b] for b in BASES)] for cell, found in sorted(counts.items(), key=_order)]
    return [HEADING, "", *NOTES, "", *table(header, rows), ""]


def _order(item: tuple[tuple[Any, ...], Any]) -> tuple[str, ...]:
    return tuple("" if part is None else str(part) for part in item[0])
