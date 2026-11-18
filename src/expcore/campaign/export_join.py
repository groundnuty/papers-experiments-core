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
- `deployment_shares` (B18), when the campaign has it: per model and fingerprint the runs and turns are summed and
  every share is computed again from the sums; a fingerprint is confirmed when any shard confirmed it, by the witness
  of the first shard, in index order, that did. Each shard serves one model under `expcore-shard/3`, and a store keeps
  a record of accepted fingerprints only when the models of its plan declare a set, so a shard's export may lack the
  key that another's has: it counts as no entries, and the joined export has the key exactly when any shard's does;
- `operator_holds` (C11), when any shard has it: every shard's own entries in shard order, each with `"shard"` added,
  so a report can say which host's platform check met each hold; a shard that met none adds none;
- `reset_settings` (C35), when any shard's store recorded the platform's settings: every shard's changes and unknown
  earlier starts, in shard order, each with `"shard"` added, since each shard's store records its own platform's
  settings and nothing of them adds up. A shard whose store recorded none exports none and adds none;
- `lost_in_kill` (O62), when any shard's store had spend a kill took out of it: every shard's own entries in shard
  order, each with `"shard"` added, since each shard counted its own journals against its own share;
- `interrupted_spend` (C05), when any shard's interrupted attempts kept a call log: every shard's own entries in shard
  order, each with `"shard"` added, since each was charged against its own shard's share;
- `attempt_diagnostics` (C40), when any shard's attempts kept a diagnostic snapshot: every shard's own entries in shard
  order, each with `"shard"` added;
- `shard_retries_by_kind` keeps what the sum of `retries_by_kind` loses: every row of every shard's own table, in shard
  order, with `"shard"` added, so a report can say which host met which kind. A shard with no row has none here, and
  the key is there all the same: it says the set-asides were recorded;
- `shards` is merge.json's list of shards, as it is;
- `campaign_fingerprint` (C36) is the fingerprint every shard shares, once, where the shards have it: a merge refuses
  shards of different fingerprints, and a join of exports whose fingerprints differ, or of which only some carry one,
  is refused too. Its hash is the `fingerprint` merge.json names.
- In a merge of selections (C38), each run entry also gains `"selection"`, the label of the selection that ran it,
  and `shards` lists each selection's levels; a merge of shards has neither.

The joined run lists must hold every run of the whole plan exactly once, and the planned runs must add up to it: a
merged batch with a shard left out is refused here too, whatever its merge.json says.

What stays the shard's own is when and how an attempt began: `dispatch_seq`, `started_at` and `lease_wait_s` come from
the shard's store, where each shard counts its own dispatches, so they say nothing about the order across shards.
An unmerged batch's export has neither `shard` nor `shards`: nothing here runs for it. A run's wall time and turn
latency are no concern of the join either: they are added to each shard's export before it (`study.merged_times`).
"""

import collections
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.campaign.deployments import fingerprint_order
from expcore.campaign.export import EXPORT_FINGERPRINT, RUN_LISTS, ExportError, none_last
from expcore.campaign.interrupted_spend import EXPORT_KEY as INTERRUPTED_KEY
from expcore.campaign.lost_ledger import KEY as LOST_KEY
from expcore.campaign.reset_settings import EXPORT_KEY as SETTINGS_KEY
from expcore.campaign.shard import EXPORT_KEY
from expcore.campaign.start_diagnostic import EXPORT_KEY as DIAGNOSTICS_KEY
from expcore.errors import CORE_KINDS

SHARD_KEY = "shard"  # the key of each run entry in a merged export: the index of the shard that ran it
SELECTION_KEY = "selection"  # in a merge of selections (C38), each run entry's: the label of the selection that ran it
SHARDS_KEY = "shards"  # the top-level key of a merged export: merge.json's list of shards
SHARD_RETRIES_KEY = "shard_retries_by_kind"  # the top-level key of a merged export: each shard's own retries rows
LISTS = (*RUN_LISTS, "excluded_attempts")
_SUMMED = ("planned_runs", "running_attempts")
_TABLES = {
    "discarded_usage": ("condition", "model", "state"),
    "retries_by_kind": ("condition", "model", "kind", "state", "charged"),
    "infrastructure_by_condition": ("condition", "kind"),
    "budget_holds": ("condition", "model"),
}
DEPLOYMENTS = "deployment_shares"
HOLDS = "operator_holds"  # the holds an operator ends (C11): a shard's export has the key only when it met one
_MODEL_SUMS = ("analysed_runs", "runs_without_mix", "mixed_runs", "turns")
_FINGERPRINT_SUMS = ("turns", "runs")
# each shard's own entries, joined in shard order with the shard named (C11, O62, C05)
_ENTRIES = (HOLDS, LOST_KEY, INTERRUPTED_KEY, DIAGNOSTICS_KEY)
# keys that one shard's export may have and another's lack
_MAY_DIFFER = (EXPORT_KEY, DEPLOYMENTS, HOLDS, SETTINGS_KEY, LOST_KEY, INTERRUPTED_KEY, DIAGNOSTICS_KEY)


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
    for key in _keys_of_the_join(exports, order):
        if key == DEPLOYMENTS:  # a shard whose plan has no model with a set exports none: it has no entries
            joined[key] = _join_deployments([exports[index].get(key, ()) for index in order])
            continue
        if key in _ENTRIES:  # a shard that met no hold of an operator's, or lost nothing in a kill, has none
            joined[key] = [{**entry, SHARD_KEY: index} for index in order for entry in exports[index].get(key, ())]
            continue
        if key == SETTINGS_KEY:  # a shard whose store recorded no settings exports none: it adds nothing
            joined[key] = _join_settings({index: exports[index].get(key) for index in order})
            continue
        parts = [exports[index][key] for index in order]
        if key in LISTS:
            joined[key] = _in_plan_order(exports, order, key, seq_of, _selection_labels(shards))
        elif key in _SUMMED:
            joined[key] = sum(parts)
        elif key in _TABLES:
            joined[key] = _sum_table(key, _TABLES[key], parts)
        elif key == "partial":
            joined[key] = any(parts)
        elif key == EXPORT_FINGERPRINT:
            joined[key] = _shared_fingerprint(parts)
        elif key == "kinds":
            seen = {kind for part in parts for kind in part}
            joined[key] = [*CORE_KINDS, *sorted(seen - set(CORE_KINDS))]
        else:  # the format and the factors: every shard says the same
            value = first[key]
            if any(part != value for part in parts):
                raise ExportError(f"the shards' exports disagree on {key!r}")
            joined[key] = value
    _require_the_plan(joined, seq_of)
    joined[SHARD_RETRIES_KEY] = [
        {**row, SHARD_KEY: index} for index in order for row in exports[index]["retries_by_kind"]
    ]
    joined[SHARDS_KEY] = [dict(shard) for shard in shards]
    return joined


def _shared_fingerprint(parts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The campaign fingerprint every shard's export carries (C36): one, or the shards are of other campaigns. A merge
    refuses such shards already (C29); a join that is handed exports of them refuses them too."""
    first = parts[0]
    for part in parts[1:]:
        if part != first:
            raise ExportError(
                "the shards' exports are of other campaigns: their campaign fingerprints differ "
                f"({first['sha256']} and {part['sha256']})"
            )
    return first


def _require_the_plan(joined: Mapping[str, Any], seq_of: Mapping[str, int]) -> None:
    """Refuse a joined export whose run lists do not hold every run of the plan exactly once: a shard left out of a
    merged batch would otherwise be analysed as a smaller campaign."""
    held = collections.Counter(entry["run_key"] for name in LISTS for entry in joined.get(name, ()))
    twice = sorted((key for key, n in held.items() if n > 1), key=seq_of.__getitem__)
    if twice:
        raise ExportError(f"run {twice[0]} is listed more than once in the shards' exports")
    missing = sorted(set(seq_of) - set(held), key=seq_of.__getitem__)
    if missing:
        raise ExportError(
            f"run {missing[0]} of the plan is in no shard's export ({len(missing)} of the plan's {len(seq_of)} runs "
            "are missing); a merged batch holds every shard"
        )
    if joined["planned_runs"] != len(seq_of):
        raise ExportError(f"the shards' exports plan {joined['planned_runs']} runs, but the plan has {len(seq_of)}")


def _keys_of_the_join(exports: Mapping[int, Mapping[str, Any]], order: Sequence[int]) -> list[str]:
    """The keys of the joined export, in the first shard's order, without the shard's own key. `deployment_shares`
    and `operator_holds`, `reset_settings`, `lost_in_kill`, `interrupted_spend` and `attempt_diagnostics` are in when
    any shard's export has them, where `export_run_state` puts them: after `kinds`, in that order."""
    keys = [key for key in exports[order[0]] if key != EXPORT_KEY]
    optional = (DEPLOYMENTS, HOLDS, SETTINGS_KEY, LOST_KEY, INTERRUPTED_KEY, DIAGNOSTICS_KEY)
    for position, extra in enumerate(optional):
        if extra not in keys and any(extra in exports[index] for index in order):
            anchors = [keys.index(key) for key in ("kinds", *optional[:position]) if key in keys]
            keys.insert(max(anchors) + 1 if anchors else len(keys), extra)
    return keys


def _same_shape(first: Mapping[str, Any], other: Mapping[str, Any], at: int, index: int) -> None:
    """Refuse an export whose keys are not the first one's (a shard's own `shard` key aside, and `deployment_shares`,
    which only an export of a plan with a model that declares a set has)."""
    keys = {key for key in first if key not in _MAY_DIFFER}
    found = {key for key in other if key not in _MAY_DIFFER}
    if keys != found:
        raise ExportError(
            f"shard {index}'s export has keys {sorted(found ^ keys)} that shard {at}'s does not, or lacks them"
        )


def _selection_labels(shards: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """The label of each selection of a merge of selections (C38), by index; empty for a merge of shards."""
    return {shard["index"]: shard["label"] for shard in shards if "selection" in shard}


def _in_plan_order(
    exports: Mapping[int, Mapping[str, Any]],
    order: Sequence[int],
    key: str,
    seq_of: Mapping[str, int],
    selections: Mapping[int, str],
) -> list[dict[str, Any]]:
    """The entries of one run list of every shard, each marked with its shard (and, for a merge of selections, with
    the selection's label), in the order of the whole plan."""
    entries = [
        {**entry, SHARD_KEY: index, **({SELECTION_KEY: selections[index]} if index in selections else {})}
        for index in order
        for entry in exports[index][key]
    ]
    unplanned = [entry["run_key"] for entry in entries if entry["run_key"] not in seq_of]
    if unplanned:
        raise ExportError(f"run {unplanned[0]} of the shards' exports is not in the plan")
    return sorted(entries, key=lambda entry: seq_of[entry["run_key"]])


def _join_deployments(parts: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Join the shards' `deployment_shares` (B18), given in shard order, into the whole campaign's: per model, in model
    order, the sums of the runs and turns and the fingerprints of every shard, each share computed again from the
    sums as `deployments.deployment_shares` computes it. Every shard declares the model's set the same way. A shard
    with no entries (it exports none) adds nothing."""
    models: dict[str, dict[str, Any]] = {}
    for part in parts:
        for entry in part:
            held = models.get(entry["model"])
            if held is None:  # a copy, in the entry's own order of keys, which the export's bytes keep
                models[entry["model"]] = {**entry, "fingerprints": [dict(f) for f in entry["fingerprints"]]}
                continue
            if held["accepted_fingerprints"] != entry["accepted_fingerprints"]:
                raise ExportError(f"the shards' exports declare other accepted fingerprints for {entry['model']!r}")
            for field in _MODEL_SUMS:
                held[field] += entry[field]
            _join_fingerprints(held["fingerprints"], entry["fingerprints"])
    for held in models.values():
        with_mix = held["analysed_runs"] - held["runs_without_mix"]
        order = fingerprint_order(held["accepted_fingerprints"])
        held["fingerprints"].sort(key=lambda cell: order(cell["fingerprint"]))
        for cell in held["fingerprints"]:
            cell["turn_share"] = cell["turns"] / held["turns"] if held["turns"] else None
            cell["run_share"] = cell["runs"] / with_mix if with_mix else None
    return [models[model] for model in sorted(models)]


def _join_settings(parts: Mapping[int, Mapping[str, Any] | None]) -> dict[str, list[dict[str, Any]]]:
    """Join the shards' `reset_settings` (C35), given by shard index in shard order: every change and every start of
    unknown earlier settings, each shard's in its own order, with the shard that saw it added last. Each shard's store
    records its own platform's settings, so nothing is summed or merged, and a shard that exports none adds none."""
    joined: dict[str, list[dict[str, Any]]] = {"changes": [], "unknown_before": []}
    for index, part in parts.items():
        if part is None:
            continue
        for name in joined:
            joined[name] += [{**entry, SHARD_KEY: index} for entry in part[name]]
    return joined


def _join_fingerprints(held: list[dict[str, Any]], more: Sequence[Mapping[str, Any]]) -> None:
    """Add one shard's fingerprints of a model to those joined so far (`held`, the join's own copies, changed in
    place): the runs and turns summed, a fingerprint new to the join kept. A fingerprint is confirmed when any shard
    confirmed it; its witness, the step and the run that confirmed it, is the first shard's that did."""
    by_name = {cell["fingerprint"]: cell for cell in held}
    for row in more:
        cell = by_name.get(row["fingerprint"])
        if cell is None:
            held.append(dict(row))
            by_name[row["fingerprint"]] = held[-1]
            continue
        for field in _FINGERPRINT_SUMS:
            cell[field] += row[field]
        if cell["confirmed_by"] is None and row["confirmed_by"] is not None:
            cell["confirmed_by"], cell["confirming_run"] = row["confirmed_by"], row["confirming_run"]


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


LEASE_KEY = "lease_id"  # in an export of a batch merged by run (C45), each entry's: the lease that ran it, as a lease
# store's own export names it in its run rows (`export.LEASE_KEY`)
FEDERATION_KEY = "federation"  # each run entry's: the federation that held its counted lease, as the ledger labels it
SITE_KEY = "site"  # each run entry's: the site of that federation, as the ledger labels it
LEASES_KEY = "leases"  # the top-level key of such an export: merge.json's list of counted leases
LEASE_RETRIES_KEY = "lease_retries_by_kind"  # each counted lease's own retries rows, with its lease id
LEDGER_SPEND_KEY = "ledger_spend"  # merge.json's `spend`: the ledger's money at its snapshot, locked dollars included


def by_run_export(
    joined: Mapping[str, Any], leases: Sequence[Mapping[str, Any]], spend: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The export of a batch merged by run (C45): the counted leases' exports joined as a merge of shards joins them
    (`join_exports`, one part per lease), with each run's lease, federation and site in place of a shard.

    Every entry `join_exports` marked with its part (`shard`) is marked with its lease (`lease_id`, as a lease store's
    own run rows already are) instead, and each entry of a
    run list (and of the per-entry lists: holds, losses, interrupted spend, diagnostics) gains `federation` and `site`
    from merge.json's `leases`, which the check and the analysis rebuilt from the ledger. `shards` becomes `leases`
    and `shard_retries_by_kind` becomes `lease_retries_by_kind`; merge.json's `spend` (the ledger's money at its
    snapshot: what every lease is charged, the ceilings in force and what unsettled leases still lock) is carried as
    `ledger_spend`. A batch merged otherwise never reaches this."""
    placed = {entry["lease_id"]: entry for entry in leases}
    out: dict[str, Any] = {}
    for key, value in joined.items():
        if key == SHARDS_KEY:
            continue
        if key == SHARD_RETRIES_KEY:
            out[LEASE_RETRIES_KEY] = [_leased(row, placed, place=False) for row in value]
        elif isinstance(value, list) and value and all(isinstance(e, dict) and SHARD_KEY in e for e in value):
            out[key] = [_leased(entry, placed, place=True) for entry in value]
        elif key == SETTINGS_KEY:  # each change and unknown start names the lease whose store recorded it
            out[key] = {name: [_leased(entry, placed, place=False) for entry in items] for name, items in value.items()}
        else:
            out[key] = value
    out[LEASES_KEY] = [dict(entry) for entry in leases]
    if spend is not None:
        out[LEDGER_SPEND_KEY] = dict(spend)
    return out


def _leased(entry: Mapping[str, Any], placed: Mapping[int, Mapping[str, Any]], *, place: bool) -> dict[str, Any]:
    """An entry with its part's lease id under `lease` in place of `shard`, and its federation and site with `place`."""
    lease = placed[entry[SHARD_KEY]]
    found = {key: value for key, value in entry.items() if key != SHARD_KEY}
    where = {FEDERATION_KEY: lease["federation"], SITE_KEY: lease["site"]} if place else {}
    return {**found, LEASE_KEY: lease["lease_id"], **where}
