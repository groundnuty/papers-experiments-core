# Provenance: new
"""The export of a merged batch: each shard's export, joined into the one a single driver of the plan makes (#154).

A merged batch has no store of its own, only the snapshot of every shard's, so `export_run_state` runs over each
snapshot and this module joins the results:

- the run lists (`exhausted_runs`, `unfinished_runs`, `analysed_attempts`, `excluded_attempts`) are concatenated in the
  order of the whole plan, so the analysis reads its rows in the order an unsharded campaign's export gives them. Each
  entry gains `"shard"`, the index of the shard that ran it;
- the figures that are sums are summed: `planned_runs`, `running_attempts`, and every count and usage of
  `discarded_usage`, `retries_by_kind`, `infrastructure_by_condition` and `budget_holds`, one row per key, in the order
  the unsharded export sorts them. `partial` holds when any shard's does, and `kinds` is the core's kinds followed by
  every other kind any shard used, sorted;
- `shards` is merge.json's list of shards, as it is.

What stays the shard's own is when and how an attempt began: `dispatch_seq`, `started_at` and `lease_wait_s` come from
the shard's store, where each shard counts its own dispatches, so they say nothing about the order across shards.
An unmerged batch's export has neither `shard` nor `shards`: nothing here runs for it.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from expcore.campaign.export import RUN_LISTS, ExportError, none_last
from expcore.campaign.shard import EXPORT_KEY
from expcore.errors import CORE_KINDS

SHARD_KEY = "shard"  # the key of each run entry in a merged export: the index of the shard that ran it
SHARDS_KEY = "shards"  # the top-level key of a merged export: merge.json's list of shards
LISTS = (*RUN_LISTS, "excluded_attempts")
_SUMMED = ("planned_runs", "running_attempts")
_TABLES = {
    "discarded_usage": ("condition", "model", "state"),
    "retries_by_kind": ("condition", "model", "kind", "state", "charged"),
    "infrastructure_by_condition": ("condition", "kind"),
    "budget_holds": ("condition", "model"),
}


def join_exports(
    exports: Mapping[int, Mapping[str, Any]], seq_of: Mapping[str, int], shards: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Join the exports of the shards of one campaign, by shard index, into the export of the whole.

    `seq_of` gives every planned run's place in the whole plan, which orders the run lists; `shards` is merge.json's
    list. Refuses exports that are not of one campaign: another format, other factors, other keys, or a figure that
    should agree and does not.
    """
    order = sorted(exports)
    if not order:
        raise ExportError("a merged export joins at least one shard's export")
    first = exports[order[0]]
    for index in order[1:]:
        _same_shape(first, exports[index], order[0], index)
    joined: dict[str, Any] = {}
    for key, value in first.items():
        if key == EXPORT_KEY:
            continue
        parts = [exports[index][key] for index in order]
        if key in LISTS:
            joined[key] = _in_plan_order(exports, order, key, seq_of)
        elif key in _SUMMED:
            joined[key] = sum(parts)
        elif key in _TABLES:
            joined[key] = _sum_table(key, _TABLES[key], parts)
        elif key == "partial":
            joined[key] = any(parts)
        elif key == "kinds":
            seen = {kind for part in parts for kind in part}
            joined[key] = [*CORE_KINDS, *sorted(seen - set(CORE_KINDS))]
        else:  # the format and the factors: every shard says the same
            if any(part != value for part in parts):
                raise ExportError(f"the shards' exports disagree on {key!r}")
            joined[key] = value
    joined[SHARDS_KEY] = [dict(shard) for shard in shards]
    return joined


def _same_shape(first: Mapping[str, Any], other: Mapping[str, Any], at: int, index: int) -> None:
    """Refuse an export whose keys are not the first one's (a shard's own `shard` key aside)."""
    keys = {key for key in first if key != EXPORT_KEY}
    found = {key for key in other if key != EXPORT_KEY}
    if keys != found:
        raise ExportError(
            f"shard {index}'s export has keys {sorted(found ^ keys)} that shard {at}'s does not, or lacks them"
        )


def _in_plan_order(
    exports: Mapping[int, Mapping[str, Any]], order: Sequence[int], key: str, seq_of: Mapping[str, int]
) -> list[dict[str, Any]]:
    """The entries of one run list of every shard, each marked with its shard, in the order of the whole plan."""
    entries = [{**entry, SHARD_KEY: index} for index in order for entry in exports[index][key]]
    unplanned = [entry["run_key"] for entry in entries if entry["run_key"] not in seq_of]
    if unplanned:
        raise ExportError(f"run {unplanned[0]} of the shards' exports is not in the plan")
    return sorted(entries, key=lambda entry: seq_of[entry["run_key"]])


def _sum_table(name: str, key_fields: tuple[str, ...], parts: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict]:
    """Sum the rows of one accounting table that share a key; the rows of other keys stay as they are, sorted as the
    unsharded export sorts them. A field that is no number (a flag) must be the same in every row of a key."""
    cells: dict[tuple, dict[str, Any]] = {}
    for rows in parts:
        for row in rows:
            key = tuple(row[field] for field in key_fields)
            if key not in cells:
                cells[key] = dict(row)
                continue
            cell = cells[key]
            for field, value in row.items():
                if field in key_fields:
                    continue
                if isinstance(value, bool) or not isinstance(value, int | float):
                    if cell[field] != value:
                        raise ExportError(f"the shards' {name} rows for {key} disagree on {field!r}")
                else:
                    cell[field] = cell[field] + value
    return [cell for _, cell in sorted(cells.items(), key=none_last)]
