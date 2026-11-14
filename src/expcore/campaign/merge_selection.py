# Provenance: new
"""The merge of a campaign run in selections (C35, `expcore-shard/4`): the checks and the merged layout's facts.

`python -m expcore merge OUT SELECTION_OUT...` merges selections as it merges shards (`campaign.merge`), with the
checks a selection needs in place of the partition's. It refuses, naming the selection and the reason, unless:

1. every store is a selection (a merge takes shards or selections, never both), at least 2, all concurrent or all
   `--sequential`, with distinct labels;
2. every selection has the same campaign fingerprint, and records the same environment identity (C29);
3. **the selections are disjoint and together cover the plan:** every run of the whole plan is selected by exactly one
   selection, every run a store holds is one its selection selects, and the stores together hold exactly the plan;
4. under `--sequential`, **the selections ran one after another:** they form one chain, the first started after none,
   each later one after every earlier one (`after` in its shares, read from their stores at its start, with what each
   had spent then), its record of each earlier one still what that one's store says (a selection run again after a
   later one started has spent more, or ran later), and no window overlaps the next (`selection_ledger.overlaps`), so
   the full caps each had were never in use twice at once;
5. with a budget and the study's prices (`Pricing`), **the selections' spend together never passed the totals:**
   `max_usd` and each `per_model_usd` ceiling, plus what releases added to it in any selection;
6. each passes its replay check (`study.merge`).

The merge numbers the selections 1..N by their canonical selection text (`numbered`), a pure function of the
snapshots, so the check and the analysis, which rebuild merge.json from the snapshots, number them alike. Each entry of
merge.json's `shards` then also holds the selection, whether it ran sequentially and its window.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

from expcore.campaign.gate import ALL as ALL_SCOPE
from expcore.campaign.manifest import Manifest
from expcore.campaign.merge import MergeRefused, ShardFacts, check_environment, check_fingerprints
from expcore.campaign.selection import SelectionSpec, canonical
from expcore.campaign.selection_ledger import Pricing, overlaps, spend_json

_FLOAT_NOISE = Fraction(1, 10**9)
"""How far over a ceiling the summed spends may read: the rounding of adding floats, never a share's micro-dollar."""


def is_selection(shard: ShardFacts) -> bool:
    """Whether a store ran as a selection of its campaign."""
    return isinstance(shard.spec, SelectionSpec)


def numbered(shards: Sequence[ShardFacts]) -> list[ShardFacts]:
    """The selections in merge order (by canonical selection text, then label), each numbered i of N."""
    ordered = sorted(shards, key=lambda shard: (canonical(shard.spec.selection), shard.spec.label))
    count = len(ordered)
    return [dataclasses.replace(s, spec=s.spec.at(i, count)) for i, s in enumerate(ordered, 1)]


def check_selections(shards: Sequence[ShardFacts], plan: Manifest, pricing: Pricing | None) -> list[ShardFacts]:
    """Run checks 1 to 5 over the selections and return them numbered; raise MergeRefused for the first fault."""
    _check_kind(shards)
    ordered = numbered(shards)
    check_fingerprints(ordered)
    _check_plan(plan, ordered[0].fingerprint)
    _check_runs(ordered, plan)
    check_environment(ordered)
    mismatch = None if pricing is None else pricing.mismatch(ordered[0].fingerprint)
    if mismatch is not None:
        raise MergeRefused(
            f"{mismatch}, so what the selections spent cannot be counted: use the study at the commit they ran"
        )
    if ordered[0].spec.sequential:
        _check_sequence(ordered, pricing)
    if pricing is not None:
        _check_totals(ordered, pricing)
    return ordered


def _check_kind(shards: Sequence[ShardFacts]) -> None:
    """1: selections only, at least two, one mode, distinct labels."""
    shard = next((s for s in shards if not is_selection(s)), None)
    if shard is not None:
        other = next(s for s in shards if is_selection(s))
        raise MergeRefused(
            f"{shard.name} is a shard and {other.name} a selection: a merge takes the shards of a campaign or its "
            "selections, never both"
        )
    if len(shards) < 2:
        raise MergeRefused(
            f"only {shards[0].name} was given: a campaign run in selections is merged from at least 2 of them"
        )
    sequential = [s for s in shards if s.spec.sequential]
    if sequential and len(sequential) != len(shards):
        concurrent = next(s for s in shards if not s.spec.sequential)
        raise MergeRefused(
            f"{sequential[0].name} ran with --sequential and {concurrent.name} without it: the selections of one "
            "campaign run all one after another, or all with split caps and budget"
        )
    labels: dict[str, ShardFacts] = {}
    for shard in sorted(shards, key=lambda s: canonical(s.spec.selection)):
        if shard.spec.label in labels:
            raise MergeRefused(f"{shard.name} and {labels[shard.spec.label].name} have the same label")
        labels[shard.spec.label] = shard


def _check_plan(plan: Manifest, fingerprint: Mapping[str, Any]) -> None:
    """The study's plan, which the cover is checked against, must be the whole plan the selections' fingerprint
    declares: every condition x task x model x seed of it, once. A study copy with a condition, a task, a model or a
    seed taken out would otherwise let selections that leave those runs out pass as a whole campaign."""
    schema = fingerprint.get("schema") if isinstance(fingerprint.get("schema"), Mapping) else {}
    conditions = schema.get("conditions") or {}
    listed = schema.get("condition_tasks") or {}
    tasks, models = list(fingerprint.get("tasks") or {}), list(fingerprint.get("models") or {})
    seeds = [int(seed) for seed in fingerprint.get("seeds") or {}]
    declared = {
        (name, task, model, seed)
        for name in conditions
        for task in tasks
        if listed.get(name) is None or task in listed[name]
        for model in models
        for seed in seeds
    }
    found = []
    for row in plan.rows:
        factors = row.key.factors
        name = next((n for n, levels in conditions.items() if _assigns(levels, factors)), None)
        found.append((name, factors.get("task"), factors.get("model"), factors.get("seed")))
    if len(found) != len(declared) or set(found) != declared:
        raise MergeRefused(
            f"the study's plan ({len(found)} runs) is not the plan the selections' campaign fingerprint declares "
            f"({len(declared)} runs): merge with the study at the commit the selections ran"
        )


def _assigns(levels: Any, factors: Mapping[str, Any]) -> bool:
    """Whether a condition's levels (from a fingerprint) are the ones a run's factors carry."""
    return isinstance(levels, Mapping) and all(str(factors.get(k)) == str(v) for k, v in levels.items())


def _check_runs(shards: Sequence[ShardFacts], plan: Manifest) -> None:
    """3: every planned run selected by exactly one selection; each store holds what its selection selects, judged by
    the plan's factors of each run key, never by the factors a store says it has."""
    planned_factors = {row.key.text: row.key.factors for row in plan.rows}
    for row in plan.rows:
        selecting = [s for s in shards if s.spec.selection.selects(row.key.factors)]
        if len(selecting) > 1:
            raise MergeRefused(
                f"{selecting[0].name} and {selecting[1].name} both select run {row.key.text}: the selections of a "
                "campaign are disjoint"
            )
        if not selecting:
            missed = sum(1 for r in plan.rows if not any(s.spec.selection.selects(r.key.factors) for s in shards))
            raise MergeRefused(
                f"run {row.key.text} is in no selection: the selections do not cover the plan ({missed} of its "
                f"{len(plan.rows)} runs are in none); merge every selection of the campaign"
            )
    holder: dict[str, ShardFacts] = {}
    for shard in shards:
        for key, factors in sorted(shard.runs.items()):
            if key in holder:
                raise MergeRefused(f"run {key} is in both {holder[key].name} and {shard.name}: the selections overlap")
            holder[key] = shard
            if key not in planned_factors:
                raise MergeRefused(f"{shard.name}: run {key} is not in the plan")
            if dict(factors) != dict(planned_factors[key]):
                raise MergeRefused(f"{shard.name}: its store gives run {key} other factors than its run key has")
            if not shard.spec.selection.selects(planned_factors[key]):
                raise MergeRefused(f"{shard.name}: holds run {key}, which its selection does not select")
    planned = set(planned_factors)
    missing = sorted(planned - set(holder))
    if missing:
        raise MergeRefused(
            f"run {missing[0]} is selected but in no selection's store ({len(missing)} of the plan's {len(planned)} "
            "runs are missing)"
        )


def _after(shard: ShardFacts) -> list[Mapping[str, Any]]:
    """The earlier selections a sequential selection recorded at its start, refused when it recorded none."""
    shares = shard.shares if isinstance(shard.shares, Mapping) else {}
    after = shares.get("after")
    if not isinstance(after, list) or not all(isinstance(e, Mapping) and "label" in e for e in after):
        raise MergeRefused(f"{shard.name}: ran with --sequential but its shares record no list of earlier selections")
    return after


def chain_of(shards: Sequence[ShardFacts]) -> list[ShardFacts]:
    """Sequential selections in the order they ran: by how many earlier ones each recorded, then start and label."""
    return sorted(shards, key=lambda s: (len(_after(s)), s.started_utc, s.spec.label))


def _check_sequence(shards: Sequence[ShardFacts], pricing: Pricing | None) -> None:
    """4: one chain, each link's record of the earlier selections still true, no window overlapping the next."""
    chain = chain_of(shards)
    for position, shard in enumerate(chain):
        before = chain[:position]
        recorded = {entry["label"]: entry for entry in _after(shard)}
        if set(recorded) != {s.spec.label for s in before}:
            named = ", ".join(sorted(recorded)) or "none"
            expected = ", ".join(s.spec.label for s in before) or "none"
            raise MergeRefused(
                f"{shard.name}: ran with --sequential after {named}, but the selections given ran it after "
                f"{expected}: every later selection names each earlier one with --after, so that its budget is what "
                "they left"
            )
        for earlier in before:
            _check_record(shard, earlier, recorded[earlier.spec.label], pricing)
        if before and (shard.window is None or before[-1].window is None):
            raise MergeRefused(f"{shard.name}: its store records no start, so when it ran is unknown")
        if before and overlaps(before[-1].window, shard.window):
            raise MergeRefused(
                f"{before[-1].name} ran until {before[-1].window[1]} and {shard.name} started at {shard.window[0]}: "
                "sequential selections each have the full caps, so their runs must not overlap"
            )


def _check_record(shard: ShardFacts, earlier: ShardFacts, entry: Mapping[str, Any], pricing: Pricing | None) -> None:
    """What `shard` recorded of `earlier` at its start must be what `earlier`'s store says now."""
    if entry.get("started_utc") != earlier.started_utc or entry.get("select") != earlier.spec.selection.to_json():
        raise MergeRefused(
            f"{shard.name}: recorded an earlier selection {earlier.spec.label!r} that is not {earlier.name} as its "
            "store says (another start, or other levels)"
        )
    if pricing is None or earlier.spend is None:
        return
    now = spend_json(earlier.spend)
    then = {key: entry.get(key) for key in now}
    if then != now:
        raise MergeRefused(
            f"{shard.name}: recorded that {earlier.name} had spent {then['spent_usd']} USD when it started, and its "
            f"store now says {now['spent_usd']} USD: {earlier.spec.label} ran again after a later selection started"
        )


def _check_totals(shards: Sequence[ShardFacts], pricing: Pricing) -> None:
    """5: the spend of all the selections within `max_usd` and each `per_model_usd`, releases added.

    Compared exactly: the ceiling as written (decimal), plus every release, against the sum of the spends, each the
    float the budget gate itself computes; only `_FLOAT_NOISE` (a nano-dollar) is allowed over, the error of adding
    those floats, which is far below the micro-dollar every share is made in."""
    budget = shards[0].fingerprint.get("caps", {}).get("budget")
    if not isinstance(budget, Mapping):
        return
    unknown = next((s for s in shards if s.spend is None or s.spend.total is None), None)
    if unknown is not None:
        raise MergeRefused(f"{unknown.name}: what it spent is unknown (an attempt of a model without a price)")
    scopes = [(ALL_SCOPE, "[budget] max_usd", budget["max_usd"])]
    scopes += [(m, f"[budget.per_model_usd] {m}", c) for m, c in sorted(budget.get("per_model_usd", {}).items())]
    for scope, what, ceiling in scopes:
        spent = [s.spend.total if scope == ALL_SCOPE else s.spend.per_model.get(scope) for s in shards]
        if any(value is None for value in spent):
            raise MergeRefused(f"what the selections spent on {scope} is unknown")
        released = sum(Fraction(s.released.get(scope, 0.0)) for s in shards)
        limit = Fraction(Decimal(str(ceiling))) + released
        total = sum(Fraction(value) for value in spent)
        if total > limit + _FLOAT_NOISE:
            raise MergeRefused(
                f"the selections spent {float(total):.9f} USD together, over {what} = {ceiling:g} USD"
                + (f" and the {float(released):g} USD released" if released else "")
                + ": their shares did not keep the total"
            )


__all__ = ["chain_of", "check_selections", "is_selection", "numbered"]
