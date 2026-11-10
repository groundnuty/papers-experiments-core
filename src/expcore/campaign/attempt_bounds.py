# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/attempt_bounds.py, blob aa745b2e86b2bd41637333a5a7ec1a9256542c1f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""What an attempt cost, and the bound charged for one that left no count (C05).

An attempt killed before its outcome was recorded is `interrupted`: no counts, no transcript. It may have spent anything
up to its token budget, so it is **charged its bound, one attempt's token budget at the model's dearest rate**, never 0.
The same goes for an attempt whose usage is known to be short (an abandoned request) or was never reported. A bounded
attempt is part of what has been spent, and no part of what a run costs: **the mean cost of a run leaves bounded
attempts out**, so a kill does not make a model look dear to the projection, and its bound does not make it look cheap.

The bound is for the budget the attempt ran under, which the caller names (`token_budget`, normally the campaign's
`Limits.token_budget`). A model whose cost is unknown (no price, not free) has no cost at all: `None`, never 0, and the
budget gate holds it (C12).

A measured attempt of a model with a flat price costs its totals at that price. One of a model with a tiered price
costs each of its requests at the tier its prompt fell in, from the per-turn usage the store keeps (C24); an attempt
whose per-turn usage is unknown is charged its bound, since its totals cannot say which tier each token fell in.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.campaign.store import Store, column_for
from expcore.config.models import ModelSpec
from expcore.config.prices import TieredPrice

# Attempts that spent tokens: every state but `running`, whose cost is not known yet.
BILLED_STATES = ("done", "infra_fail", "harness_error", "interrupted", "outage_discarded")


@dataclass(frozen=True)
class AttemptCost:
    """The cost of one attempt in dollars: `None` when the model's price is unknown; `bounded` when it is the bound."""

    attempt_id: int
    run_key: str
    model: str
    state: str
    usd: float | None
    bounded: bool


def bound_usd(spec: ModelSpec, token_budget: int) -> float | None:
    """Return the bound of one attempt: its token budget at the model's dearest rate; 0 for a free model, None unknown."""
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget <= 0:
        raise ValueError(f"the token budget of an attempt is a positive integer, not {token_budget!r}")
    if spec.free:
        return 0.0
    if spec.price is None:
        return None
    return token_budget * spec.price.dearest_per_token()


def measured_usd(spec: ModelSpec, row: Mapping[str, Any]) -> float | None:
    """Return the cost of an attempt from its counts, or None if the model's price is unknown or a count is missing.

    A count the endpoint did not report is unknown, never zero, so the caller bounds the attempt instead. A flat price
    is applied to the attempt's totals. A tiered price is applied to each request at the tier its prompt fell in, from
    the per-turn usage the store keeps (C24); without it, or with a turn that did not report its prompt or its reply,
    the cost is unknown, since totals cannot say which tier each token fell in.
    """
    if spec.free:
        return 0.0
    if isinstance(spec.price, TieredPrice):
        requests = _requests(row["turn_usage"])
        return None if requests is None else spec.price.requests_usd(requests)
    if spec.price is None or row["input_tokens"] is None or row["output_tokens"] is None:
        return None
    return spec.price.usd(
        row["input_tokens"],
        row["output_tokens"],
        row["cache_creation_tokens"] or 0,
        row["cache_read_tokens"] or 0,
    )


def _requests(stored: str | None) -> list[tuple[int, int, int, int]] | None:
    """Return the requests of an attempt from its stored per-turn usage; None when unknown or a turn's prompt or reply
    count is missing (a cache count left out is none)."""
    if stored is None:
        return None
    try:
        turns = json.loads(stored)
    except ValueError:
        return None
    requests = []
    for prompt, reply, written, read in turns:
        if prompt is None or reply is None:
            return None
        requests.append((prompt, reply, written or 0, read or 0))
    return requests


def attempt_costs(store: Store, models: Mapping[str, ModelSpec], token_budget: int) -> list[AttemptCost]:
    """Return the cost of every attempt that spent tokens, in attempt order, each charged as the module docstring says.

    `models` maps a run's model key to its table entry; a key it lacks is an unknown cost.
    """
    marks = ",".join("?" * len(BILLED_STATES))
    rows = store.q(
        "SELECT a.id, a.run_key, a.state, r." + column_for("model") + " AS model, a.input_tokens, a.output_tokens, "
        "a.cache_creation_tokens, a.cache_read_tokens, a.turn_usage, COALESCE(a.usage_complete,1) AS complete "
        f"FROM attempts a JOIN runs r USING(run_key) WHERE a.state IN ({marks}) ORDER BY a.id",
        BILLED_STATES,
    )
    costs = []
    for row in rows:
        spec = models.get(row["model"])
        if spec is None:
            costs.append(AttemptCost(row["id"], row["run_key"], row["model"], row["state"], None, False))
            continue
        measured = measured_usd(spec, row) if row["complete"] else None
        if measured is not None:
            costs.append(AttemptCost(row["id"], row["run_key"], row["model"], row["state"], measured, False))
            continue
        bound = bound_usd(spec, token_budget)
        costs.append(
            AttemptCost(
                row["id"], row["run_key"], row["model"], row["state"], bound, bound is not None and not spec.free
            )
        )
    return costs


def spent_usd(costs: list[AttemptCost], model: str | None = None) -> float | None:
    """Return the dollars spent, bounds included, on one model or on all; None if any attempt's cost is unknown."""
    chosen = [c for c in costs if model is None or c.model == model]
    if any(c.usd is None for c in chosen):
        return None
    return sum(c.usd for c in chosen if c.usd is not None)


def mean_run_usd(store: Store, costs: list[AttemptCost], model: str) -> float | None:
    """Return what a finished run of the model costs on average, from measured attempts only; None without a sample.

    A sample is a run with status `done` and at least one measured attempt. Bounded attempts, whose cost is a ceiling and
    not a measurement, are no part of the mean (C05).
    """
    done = {
        r["run_key"]
        for r in store.q(f"SELECT run_key FROM runs WHERE status='done' AND {column_for('model')}=?", (model,))
    }
    per_run: dict[str, float] = {}
    for c in costs:
        if c.model == model and c.run_key in done and not c.bounded and c.usd is not None:
            per_run[c.run_key] = per_run.get(c.run_key, 0.0) + c.usd
    return sum(per_run.values()) / len(per_run) if per_run else None
