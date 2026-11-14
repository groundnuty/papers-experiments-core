# Provenance: new
"""The start of a selection of a study's campaign (C35): its part of the plan, its shares, and every refusal first.

`run STUDY --select FACTOR=L1,L2 --shard-label L [--sequential [--after EARLIER_OUT ...]]` runs the selected runs of
the campaign as a shard of it (`campaign.selection`). Everything here is decided before the store is opened, from the
study and the fingerprint of the whole plan:

- the selection is held to the study's schema (an unknown factor or level is refused, naming it), and must select some
  of the plan's runs and not all of them;
- `[shards] per_model` is refused in this version: a selection and a layout by model do not combine yet;
- the shares (`campaign.selection_budget`): proportional by default; with `--sequential` the full caps and the budget
  the earlier selections left (`campaign.selection_after`). A share below one run's reservation of a model the
  selection runs is refused, naming the model, in either mode.
"""

import collections
import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore.campaign.manifest import Manifest, selection_manifest
from expcore.campaign.selection import SelectionSpec
from expcore.campaign.selection_after import earlier_selections, entry_of, spent_before
from expcore.campaign.selection_budget import proportional_budget, proportional_caps, refuse_short, remaining_budget
from expcore.campaign.selection_ledger import Pricing
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.shard import ShardError
from expcore.campaign.shard_budget import reservation_usd
from expcore.campaign.shard_run import ShardStart
from expcore.study.config import StudyConfig


def pricing_of(study: StudyConfig) -> Pricing:
    """The study's plan models and the caps every attempt runs under: what a selection's spend is counted with."""
    return Pricing({key: study.models.resolve(key) for key in study.campaign.models}, study.campaign.agent.limits)


def _reservations(pricing: Pricing) -> dict[str, float]:
    """One run's reservation per model of the plan; a model whose runs no share can reserve is refused."""
    out = {}
    for model, spec in sorted(pricing.models.items()):
        try:
            reserved = reservation_usd(spec, pricing.limits)
        except ValueError as error:  # a priced model with no context window: nothing bounds its runs
            raise ShardError(f"model {model}: {error}") from None
        if reserved is None:
            raise ShardError(
                f"model {model} has no price and is not marked free, so what its runs reserve is unknown and no "
                "selection's share of [budget] can be set; give it a price, or mark it free"
            )
        out[model] = reserved
    return out


def selection_start(
    study: StudyConfig,
    settings: SchedulerConfig,
    spec: SelectionSpec,
    *,
    fingerprint: Mapping[str, Any],
    environment: Mapping[str, str],
    after: Sequence[Path] = (),
) -> tuple[Manifest, SchedulerConfig, ShardStart]:
    """A selection's part of the plan, its settings with the shares applied, and the start its store pins.

    Raises ShardError for every refusal of the module's docstring, before anything is written.
    """
    if study.campaign.shards is not None:
        raise ShardError(
            "--select cannot be combined with [shards] per_model in this version: run the selection of a campaign "
            "that does not shard by model, or shard it with --shard"
        )
    if after and not spec.sequential:
        raise ShardError("--after names the selections this one runs after, so it needs --sequential")
    spec = SelectionSpec(spec.label, spec.selection.checked(study.schema), spec.sequential, wide_label=True)
    manifest = selection_manifest(study.manifest, spec.selection)
    if not manifest.rows:
        raise ShardError(f"{spec.name} selects no run of the plan")
    if len(manifest.rows) == len(study.manifest.rows):
        raise ShardError(f"{spec.name} selects every run of the plan: run the campaign without --select")
    selected = collections.Counter(str(row.key.factors["model"]) for row in manifest.rows)
    planned = collections.Counter(str(row.key.factors["model"]) for row in study.manifest.rows)
    budget, pricing = settings.budget, pricing_of(study)
    reserve = _reservations(pricing) if budget is not None else {}
    if spec.sequential:
        earlier = earlier_selections(
            after,
            selection=spec.selection,
            label=spec.label,
            fingerprint=fingerprint,
            environment=environment,
            plan=study.manifest,
            pricing=pricing,
        )
        caps = dict(sorted(settings.unit_caps.items()))
        parts, how = None, ""
        if budget is not None:
            spent, spent_per_model = spent_before(earlier)
            parts = remaining_budget(budget.max_usd, budget.per_model_usd, spent, spent_per_model)
            how = "what the earlier selections left" if earlier else "the whole budget, as the first selection"
        shares = {"unit_caps": caps, "budget": parts, "after": [entry_of(facts) for facts in earlier]}
        run_caps = caps
    else:
        unit_of = {model: endpoint.unit for model, endpoint in study.campaign.endpoints.items()}
        caps = proportional_caps(settings.unit_caps, selected, planned, unit_of)
        parts, how = None, "its proportion of the plan's reserved bound"
        if budget is not None:
            parts = proportional_budget(budget.max_usd, budget.per_model_usd, reserve, selected, planned)
        shares = {"unit_caps": caps, "budget": parts}
        run_caps = {unit: cap for unit, cap in caps.items() if cap > 0}
    shared = None
    if budget is not None and parts is not None:
        refuse_short(parts, budget.per_model_usd, reserve, selected, how)
        if parts["max_usd"] > 0:  # 0 only for a selection of free models, which no gate holds for cost
            shared = dataclasses.replace(budget, max_usd=parts["max_usd"], per_model_usd=parts["per_model_usd"])
    run = dataclasses.replace(settings, unit_caps=run_caps, budget=shared)
    return manifest, run, ShardStart(spec, dict(environment), shares)


__all__ = ["pricing_of", "selection_start"]
