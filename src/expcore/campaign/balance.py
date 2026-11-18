# Provenance: new
"""The balance of an elastic campaign over its federations and sites (#462 step 4, C45): reported, never enforced.

A federation of an elastic fleet takes one run at a time, in the seeded order, whatever its condition, so a federation
that ran one run of a model holds one condition of it by design. Balance is then a property of the whole campaign: how
each model's conditions spread over the sites and the federations that ran them. `balance_of` counts, from the export of
a batch merged by run (whose run entries carry `federation` and `site`, `campaign.export_join.by_run_export`), every
planned run by its counted lease's placement (a run the ledger exhausted for its lost leases by its last lease's): per
model, conditions by site and conditions by federation, and the runs exhausted by federation loss by condition and site,
every cell written out, zeros included. Nothing is tested and nothing is refused: the figures are what `analysis.json`
(`balance`) and the report's balance section (`report_balance`) show. The federation effect itself is the declared
sensitivity rerun (`stats.federation_effect`). An export without placements (any batch not merged by run) has no
balance.
"""

from collections.abc import Callable, Mapping
from typing import Any

from expcore.campaign.export_join import FEDERATION_KEY, LEDGER_SPEND_KEY, LOST_KEY_FLAG, SITE_KEY

_RUN_LISTS = ("analysed_attempts", "exhausted_runs", "unfinished_runs", "excluded_attempts")


def balance_of(export: Mapping[str, Any]) -> dict[str, Any] | None:
    """Per model, the planned runs of each condition by site and by federation; None for an export whose run entries
    carry no placement."""
    entries = [entry for name in _RUN_LISTS for entry in export.get(name, ())]
    if not entries or any(FEDERATION_KEY not in entry or SITE_KEY not in entry for entry in entries):
        return None
    models = sorted({str(entry["factors"]["model"]) for entry in entries})
    found = {"runs": len(entries), "models": [_model(model, entries) for model in models]}
    spend = export.get(LEDGER_SPEND_KEY)
    if spend is not None:  # the ledger's money at its snapshot (merge.json's `spend`), locked dollars included
        found["ledger_spend"] = dict(spend)
    return found


def _model(model: str, entries: list[Mapping[str, Any]]) -> dict[str, Any]:
    mine = [entry for entry in entries if str(entry["factors"]["model"]) == model]
    conditions = sorted({entry["condition"] for entry in mine})
    return {
        "model": model,
        "runs": len(mine),
        "conditions": conditions,
        "by_site": _cross(mine, conditions, SITE_KEY),
        "by_federation": _cross(mine, conditions, FEDERATION_KEY),
        # runs the ledger exhausted for their lost leases, by condition and by their last lease's site: a condition that
        # loses federations more often than another is a source of bias the counts above would hide
        "exhausted_by_loss": _cross(mine, conditions, SITE_KEY, lambda entry: bool(entry.get(LOST_KEY_FLAG))),
    }


def _cross(
    entries: list[Mapping[str, Any]],
    conditions: list[str],
    key: str,
    counted: Callable[[Mapping[str, Any]], bool] = lambda entry: True,
) -> dict[str, dict[str, int]]:
    """Condition by placement (`site` or `federation`): the runs `counted` takes per cell, every cell written out,
    over every placement the model's runs had."""
    places = sorted({entry[key] for entry in entries})
    counts = {condition: dict.fromkeys(places, 0) for condition in conditions}
    for entry in entries:
        if counted(entry):
            counts[entry["condition"]][entry[key]] += 1
    return counts
