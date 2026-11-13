# Provenance: new
"""How every planned run of a campaign ended, per condition: what `analysis.json` and `report.md` both show (C23).

The export's run lists partition the plan (`expcore.campaign.export`): each planned run is analysed, exhausted,
unfinished or, in a partial analysis, left out. Counting them per condition gives each condition's planned runs and how
each ended, so an exhausted run is seen under its own condition and not only in a total; an export whose lists do not
add up to `planned_runs` is refused. The attempts set aside are summed over the condition's infrastructure kinds. The
pairs each contrast used and missed are counted by the statistics kernel (`expcore.stats.pairing`).

A batch merged from shards (#154) has `shards` in its export and `shard` in each run entry; `shard_accounting` gives the
same counts per shard and condition, for the report's shard table.
"""

from collections.abc import Mapping
from typing import Any

RUN_LISTS = (
    ("analysed", "analysed_attempts"),
    ("exhausted", "exhausted_runs"),
    ("unfinished", "unfinished_runs"),
    ("left_out", "excluded_attempts"),  # only in the export of a partial analysis
)
FIELDS = ("planned", *(name for name, _ in RUN_LISTS), "set_aside")


def condition_accounting(export: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One entry per condition, in name order: its planned runs, how many ended in each run list, and the attempts
    set aside (`set_aside` of `infrastructure_by_condition`, summed over kinds). Every count is present, zeros
    included. Refuses an export whose run lists do not hold `planned_runs` runs."""
    counts: dict[str, dict[str, int]] = {}
    for name, part in RUN_LISTS:
        for entry in export.get(part, []) if name == "left_out" else export[part]:
            cell = counts.setdefault(entry["condition"], dict.fromkeys(FIELDS, 0))
            cell[name] += 1
            cell["planned"] += 1
    for kind in export["infrastructure_by_condition"]:
        counts.setdefault(kind["condition"], dict.fromkeys(FIELDS, 0))["set_aside"] += kind["set_aside"]
    held = sum(cell["planned"] for cell in counts.values())
    if held != export["planned_runs"]:
        raise ValueError(
            f"the export's planned_runs is {export['planned_runs']}, but its run lists hold {held} runs; "
            "every planned run must be analysed, exhausted, unfinished or left out"
        )
    return [{"condition": name, **counts[name]} for name in sorted(counts)]


def shard_accounting(export: Mapping[str, Any]) -> list[dict[str, Any]]:
    """For a merged, sharded batch: one entry per shard and condition, in shard order then condition name, with the
    shard's index, label and start and the counts of `condition_accounting` (without `set_aside`, which the export
    keeps only as a sum over every shard) over the runs that shard ran, each run entry naming its `shard`. Every
    condition the export has a run of is listed under every shard, with zeros where the shard ran none of it. `[]`
    for an export without `shards`: an unmerged batch has none.

    Refuses a `shards` that is not a non-empty list of entries with an index and a label, and a run entry whose shard
    the list does not hold, since the table would then show fewer runs than the batch has.
    """
    if "shards" not in export:
        return []
    shards = export["shards"]
    if (
        not isinstance(shards, list)
        or not shards
        or not all(isinstance(s, Mapping) and {"index", "label"} <= set(s) for s in shards)
    ):
        raise ValueError("the export's shards must be a non-empty list of shards, each with an index and a label")
    fields = [field for field in FIELDS if field != "set_aside"]
    known = {shard["index"] for shard in shards}
    counts: dict[tuple[int, str], dict[str, int]] = {}
    conditions: set[str] = set()
    for name, part in RUN_LISTS:
        for entry in export.get(part, []) if name == "left_out" else export[part]:
            if "shard" not in entry:
                raise ValueError(f"run {entry['run_key']!r} names no shard, but the export lists shards")
            if entry["shard"] not in known:
                raise ValueError(
                    f"run {entry['run_key']!r} is in shard {entry['shard']!r}, which the export's shards do not list"
                )
            conditions.add(entry["condition"])
            cell = counts.setdefault((entry["shard"], entry["condition"]), dict.fromkeys(fields, 0))
            cell[name] += 1
            cell["planned"] += 1
    return [
        {
            "shard": shard["index"],
            "label": shard["label"],
            "started_utc": shard.get("started_utc"),
            "condition": condition,
            **counts.get((shard["index"], condition), dict.fromkeys(fields, 0)),
        }
        for shard in sorted(shards, key=lambda shard: shard["index"])
        for condition in sorted(conditions)
    ]
