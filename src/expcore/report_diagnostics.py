# Provenance: new
"""How the Markdown report (`expcore.report`) counts the attempts that kept a diagnostic snapshot (C40).

A section "Instrument start failures" follows the infrastructure tables, with a row per condition, instrument, kind
and state of the export's `attempt_diagnostics`: how many attempts, how many of them used one of the run's attempts,
and what became of each one's diagnostic, in four categories that add up to the attempts: shown, withheld (the executor
declared no secrets), none given (the failed start gave no diagnostic) and not kept. A condition with none has a row of
zeros, so 0 is never mistaken for not reported. What each snapshot says (what the instrument checked and what it saw)
is in the export, under each attempt's id. A batch merged from shards has a Shard column.

An export without `attempt_diagnostics` has no such section and no such text, so a report keeps its bytes.
"""

import textwrap
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.start_diagnostic import EXPORT_KEY, WITHHELD_ERROR

HEADING = "### Instrument start failures"
SHOWN, WITHHELD, NO_DIAGNOSTIC, NOT_KEPT = "Shown", "Withheld", "No diagnostic", "Not kept"
CATEGORIES = (SHOWN, WITHHELD, NO_DIAGNOSTIC, NOT_KEPT)  # the columns, in order: each attempt is in exactly one
NONE = "-"  # an instrument that is not named, and the cells of a condition's row of zeros
NOTES = textwrap.wrap(
    "Attempts that kept a diagnostic snapshot, which a failed instrument start leaves on the attempt that was set "
    "aside and run again: the instrument that failed, and what it checked and saw as the instrument gave it, redacted "
    "as an instrument's snapshot is. An infrastructure failure from anywhere else that carried a diagnostic is listed "
    "with no instrument (-). Charged counts the attempts that used one of the run's attempts. Attempts is the sum of "
    "Shown, Withheld, No diagnostic and Not kept. Shown counts the attempts whose diagnostic is in the export. "
    "Withheld counts the attempts whose executor declared no secrets (`RunRequest.declare_secrets`, called before it "
    "starts an instrument) and whose instrument gave a diagnostic: the core cannot show a diagnostic redacted against "
    "a partial list of the run's secrets, so the export and this report say that it was withheld and why, never its "
    "text, which is in the campaign's local log only; such an attempt names no instrument. No diagnostic counts the "
    "failed starts that gave none, whether or not the executor declared: there is nothing to show or to withhold, "
    "and the instrument is named only when the executor declared. Not kept counts the snapshots that could not be "
    "kept (not a JSON object, too large, or a redaction that failed), which the export records as failed. The "
    f"snapshots are in export.json under {EXPORT_KEY}, one entry per attempt.",
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


def category(snapshot: Mapping[str, Any]) -> str:
    """What became of an attempt's diagnostic, by the shape of its snapshot: exactly one of `SHOWN`, `WITHHELD`,
    `NO_DIAGNOSTIC` and `NOT_KEPT`, so the four columns add up to the attempts. A failed snapshot (`ok` false) is a
    withheld stub or one that could not be kept; any other has the instrument's diagnostic or has none."""
    if snapshot.get("ok") is False:
        return WITHHELD if snapshot.get("error") == WITHHELD_ERROR else NOT_KEPT
    return SHOWN if "diagnostic" in snapshot else NO_DIAGNOSTIC


def section(export: Mapping[str, Any], table: Table) -> list[str]:
    """The lines of the section, ending with a blank line; none for an export without any attempt's diagnostic."""
    entries = export.get(EXPORT_KEY) or []
    if not entries:
        return []
    sharded = any("shard" in entry for entry in entries)
    found: Counter[tuple[Any, ...]] = Counter()
    charged: Counter[tuple[Any, ...]] = Counter()
    by_category: Counter[tuple[Any, ...]] = Counter()
    for entry in entries:
        key = _key(entry)
        found[key] += 1
        charged[key] += int(bool(entry["charged"]))
        by_category[(*key, category(entry["diagnostic"]))] += 1
    present = {key[:2] for key in found}  # (shard, condition) with a row
    planned = {(entry.get("shard"), entry["condition"]) for name in _RUN_LISTS for entry in export.get(name, [])}
    rows = [[*key, found[key], charged[key], *(by_category[(*key, name)] for name in CATEGORIES)] for key in found]
    zeros = [0] * (2 + len(CATEGORIES))  # attempts, charged and each category
    rows += [[shard, condition, NONE, NONE, NONE, *zeros] for shard, condition in planned - present]
    rows.sort(key=_order)
    header = ["Condition", "Instrument", "Kind", "State", "Attempts", "Charged", *CATEGORIES]
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
