# Provenance: new
"""How every planned run of a campaign ended, per condition: what `analysis.json` and `report.md` both show (C23).

The export's run lists partition the plan (`expcore.campaign.export`): each planned run is analysed, exhausted,
unfinished or, in a partial analysis, left out. Counting them per condition gives each condition's planned runs and how
each ended, so an exhausted run is seen under its own condition and not only in a total; an export whose lists do not
add up to `planned_runs` is refused. The attempts set aside are summed over the condition's infrastructure kinds. The
pairs each contrast used and missed are counted by the statistics kernel (`expcore.stats.pairing`).
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
