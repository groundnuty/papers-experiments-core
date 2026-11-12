# Provenance: new
"""The budget of a campaign that shards by model (`expcore-shard/3`, #154, C30, C31): who gets which dollars.

Under `expcore-shard/2` every shard runs every model, so an equal share of each ceiling is fair. Under
`expcore-shard/3` a shard runs one model, and the models cost very different amounts, so:

- **`per_model_usd`**: a model's ceiling is split equally over that model's own shards, in whole micro-dollars rounded
  down (`shard.part_usd`); a ceiling of 0 holds the model entirely and is 0 for each of its shards. A shard keeps only
  its own model's ceiling.
- **`max_usd`** is split over the shards of the **priced** models only, in proportion to each shard's reserved bound:
  its planned runs times what the budget gate reserves for one run of its model, in whole micro-dollars rounded down,
  so the shares sum to at most the total. The shards of a model that costs nothing (free, or priced at 0) get 0.
- **What one run reserves** (`reservation_usd`) is the larger of the model's bound with and without the reply maximum
  (`attempt_bounds.bound_usd`, C25, C27). Which of the two the gate uses is known only after admission, and neither is
  always the larger: the window bounds the request past the token cap without the reply maximum, and a prompt that
  fills the window plus a reply within that maximum with it. The larger one is what a run can be reserved either way.

**Refused**, naming the shard, the model and the figures: a model with no price that is not free (its runs cannot be
reserved, so no share of the money can be set); `max_usd` smaller than one run's reservation for every priced shard
together; a priced shard whose share of `max_usd`, or of a ceiling above 0, is below one run's reservation, which would
hold that shard before its first run. A study is refused at load (`study.config`), and a shard again at its start.
"""

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from expcore.campaign.attempt_bounds import bound_usd
from expcore.campaign.gate import Budget
from expcore.campaign.manifest import Manifest
from expcore.campaign.shard import ModelShards, ShardError, assign_by_model, micro_usd, pair_of, part_usd, usd_of
from expcore.config.models import Limits, ModelSpec

_MICRO = 1_000_000


def reservation_usd(spec: ModelSpec, limits: Limits) -> float | None:
    """What the budget gate can reserve for one run of the model, with or without the reply maximum; 0 for a model
    that costs nothing, None when its price is unknown."""
    bounds = [bound_usd(spec, limits, reply_capped=capped) for capped in (False, True)]
    return None if bounds[0] is None or bounds[1] is None else max(bounds[0], bounds[1])


def planned_runs(manifest: Manifest, layout: ModelShards) -> dict[int, int]:
    """How many runs of the whole plan each shard plans under `expcore-shard/3`, by index; 0 for a shard with none."""
    assignment = assign_by_model({pair_of(row.key.factors) for row in manifest.rows}, layout)
    runs = dict.fromkeys(range(1, layout.count + 1), 0)
    for row in manifest.rows:
        runs[assignment[pair_of(row.key.factors)]] += 1
    return runs


def shard_budgets(
    budget: Budget, layout: ModelShards, manifest: Manifest, models: Mapping[str, ModelSpec], limits: Limits
) -> dict[int, dict[str, Any]]:
    """Every shard's part of the budget under `expcore-shard/3`, by index: `{"max_usd": x, "per_model_usd": {...}}`.

    `manifest` is the whole plan, `models` the plan's models and `limits` the caps every attempt runs under, from
    which a run's reservation is bounded. Raises ShardError for each refusal of the module's docstring.
    """
    reserve = {model: _reservation(model, models[model], limits) for model, _ in layout.per_model}
    runs = planned_runs(manifest, layout)
    owner = {index: layout.at(index) for index in runs}
    # a shard with no run needs nothing; its start is refused for having no run (`study.pipeline`)
    weights = {i: runs[i] * Fraction(reserve[owner[i][0]]) for i in runs if runs[i] and reserve[owner[i][0]] > 0}
    total = micro_usd(budget.max_usd)
    need = sum(Fraction(reserve[owner[i][0]]) for i in weights)
    if need > Fraction(total, _MICRO):
        raise ShardError(
            f"[budget] max_usd {budget.max_usd:g} is less than one run's reservation for each of the {len(weights)} "
            f"shards of priced models together ({float(need):.6f} USD), so some shard could never run; raise it"
        )
    whole = sum(weights.values())
    shares = {i: (int(total * weight / whole) if whole else 0) for i, weight in weights.items()}
    parts = {}
    for index in runs:
        model, k, count = owner[index]
        max_usd = usd_of(shares.get(index, 0))
        per_model = {}
        if model in budget.per_model_usd:
            what = f"[budget.per_model_usd] {model}"
            per_model[model] = part_usd(budget.per_model_usd[model], k, count, what=what, allow_zero=True)
        if index in weights:
            _refuse_short(index, model, reserve[model], "[budget] max_usd", max_usd)
            if per_model.get(model):
                _refuse_short(index, model, reserve[model], f"[budget.per_model_usd] {model}", per_model[model])
        parts[index] = {"max_usd": max_usd, "per_model_usd": per_model}
    return parts


def _reservation(model: str, spec: ModelSpec, limits: Limits) -> float:
    """One run's reservation of a model that shards by model; a model whose price is unknown is refused."""
    try:
        reserved = reservation_usd(spec, limits)
    except ValueError as error:  # a priced model with no context window: nothing bounds its runs
        raise ShardError(f"model {model}: {error}") from None
    if reserved is None:
        raise ShardError(
            f"model {model} has no price and is not marked free, so what its runs reserve is unknown and no part of "
            "[budget] max_usd can be set for its shards; give it a price, or mark it free"
        )
    return reserved


def _refuse_short(index: int, model: str, reserved: float, what: str, share_usd: float) -> None:
    """Refuse a priced shard whose share of `what` cannot pay for one run of its model."""
    if Fraction(share_usd) < Fraction(reserved):
        raise ShardError(
            f"shard {index} (model {model}) gets {share_usd:.6f} USD of {what}, less than one run's reservation of "
            f"{reserved:.6f} USD, so it could never run; raise {what}, or give the model fewer shards"
        )
