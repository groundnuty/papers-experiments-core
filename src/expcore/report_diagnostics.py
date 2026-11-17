# Provenance: new
"""How the Markdown report (`expcore.report`) counts the attempts that kept a diagnostic snapshot (C40).

A section "Instrument start failures" follows the infrastructure tables, with a row per condition, instrument, kind
and state of the export's `attempt_diagnostics`: how many attempts, how many of them used one of the run's attempts,
and how many snapshots could not be kept. A condition with none has a row of zeros, so 0 is never mistaken for not
reported. What each snapshot says (what the instrument checked and what it saw) is in the export, under each attempt's
id. A batch merged from shards has a Shard column.

An export without `attempt_diagnostics` has no such section and no such text, so a report keeps its bytes.
"""

import textwrap
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.start_diagnostic import EXPORT_KEY

HEADING = "### Instrument start failures"
NONE = "-"  # an instrument that is not named, and the cells of a condition's row of zeros
NOTES = textwrap.wrap(
    "Attempts that kept a diagnostic snapshot, which a failed instrument start leaves on the attempt that was set "
    "aside and run again: the instrument that failed, and what it checked and saw as the instrument gave it, redacted "
    "as an instrument's snapshot is. An infrastructure failure from anywhere else that carried a diagnostic is listed "
    "with no instrument (-). Charged counts the attempts that used one of the run's attempts; Not kept counts the "
    "snapshots that could not be kept (not a JSON object, too large, a redaction that failed, or withheld because the "
    "executor did not declare its secrets before it started its instruments), which the export "
    f"records as failed. The snapshots are in export.json under {EXPORT_KEY}, one entry per attempt.",
    width=118,
)
Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]
_RUN_LISTS = ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts")


def _key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    shot = entry["diagnostic"]
    return (
        entry.get("shard"),
        entry["condition"],
        shot.get("instrument", NONE),
        entry["infra_kind"] or NONE,
        entry["state"],
    )


def section(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for an export without any attempt's diagnostic."""
    entries = export.get(EXPORT_KEY) or []
    if not entries:
        return []
    sharded = any("shard" in entry for entry in entries)
    found: Counter[tuple[Any, ...]] = Counter()
    charged: Counter[tuple[Any, ...]] = Counter()
    unkept: Counter[tuple[Any, ...]] = Counter()
    for entry in entries:
        key = _key(entry)
        found[key] += 1
        charged[key] += int(bool(entry["charged"]))
        unkept[key] += int(entry["diagnostic"].get("ok") is False)
    present = {key[:2] for key in found}  # (shard, condition) with a row
    planned = {(entry.get("shard"), entry["condition"]) for name in _RUN_LISTS for entry in export.get(name, [])}
    rows = [[*key, found[key], charged[key], unkept[key]] for key in found]
    rows += [[shard, condition, NONE, NONE, NONE, 0, 0, 0] for shard, condition in planned - present]
    rows.sort(key=_order)
    header = ["Condition", "Instrument", "Kind", "State", "Attempts", "Charged", "Not kept"]
    if sharded:
        header.insert(0, "Shard")
        rows = [[NONE if row[0] is None else row[0], *row[1:]] for row in rows]
    else:
        rows = [row[1:] for row in rows]
    return [HEADING, "", *NOTES, "", *table(header, rows), ""]


def _order(row: Sequence[Any]) -> tuple[Any, ...]:
    """Shards by their number, then the condition, instrument, kind and state by name."""
    shard = row[0]
    return (shard is not None, 0 if shard is None else shard, *("" if part is None else str(part) for part in row[1:5]))
