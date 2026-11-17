# Provenance: new
"""What a selection's store says it spent and when it ran (C38): the figures its successors and the merge compare.

- **Spend** (`spend_of`) is the budget gate's own figure (`campaign.gate`): every attempt's cost by
  `attempt_bounds.attempt_costs` (measured, or the bound its row records it was reserved at, top-ups included), plus
  what admission's probes were charged (`campaign.probe_spend`) and what a kill took out of the store
  (`campaign.lost_spend`, O62), in total and per model. A later sequential selection
  is given the total minus it (`selection_budget.remaining_budget`), and the merge sums it over every selection. An
  attempt of a model whose cost is unknown makes the spend unknown, which a budget refuses.
- **Window** (`window_of`): from the selection's first start (`META_STARTED`) to the last time an attempt of it began
  or ended, from the store's own timestamps (whole seconds). Two sequential selections overlap when the later one
  started before the earlier one's last attempt ended; one that ends in the second the next one starts does not.
- **Released** (`released_of`): what releases added to each ceiling (`campaign.gate.RELEASED_KEY`), by scope.

Each reads a store through `query(sql, args)`, so the same code reads a live store's snapshot at a successor's start
and a merged batch's snapshot at its check.
"""

import datetime as dt
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.campaign import lost_spend, probe_spend, reply_cap
from expcore.campaign.attempt_bounds import attempt_costs, spent_usd
from expcore.campaign.gate import RELEASED_KEY
from expcore.campaign.shard import META_STARTED
from expcore.config.models import Limits, ModelSpec

Query = Callable[..., Iterable[Sequence[Any]]]  # query(sql, args=()) -> rows, indexable by position and name


@dataclass(frozen=True)
class Pricing:
    """The plan's models by key and the caps every attempt ran under: what bounds an attempt's cost.

    `digests` are the models' entries as the campaign fingerprint holds them and `agent` the loop's caps as its
    `caps.agent` holds them, so a reader can refuse prices that are not the ones the campaign ran under
    (`mismatch`)."""

    models: Mapping[str, ModelSpec]
    limits: Limits
    digests: Mapping[str, Any] | None = None
    agent: Mapping[str, Any] | None = None

    def mismatch(self, fingerprint: Mapping[str, Any]) -> str | None:
        """Why these prices are not the campaign's whose fingerprint is given; None when they are."""
        if self.digests is not None and dict(self.digests) != dict(fingerprint.get("models", {})):
            return "the study's models.toml entries are not the ones the campaign ran under"
        caps = fingerprint.get("caps")
        if self.agent is not None and (not isinstance(caps, Mapping) or dict(self.agent) != caps.get("agent")):
            return "the study's [agent] caps are not the ones the campaign ran under"
        return None


@dataclass(frozen=True)
class Spend:
    """What a store spent, in dollars: in total and per model; None where an attempt's cost is unknown."""

    total: float | None
    per_model: Mapping[str, float | None]


class _View:
    """A store read through a query: what `attempt_costs`, `probe_spend` and `reply_cap` ask of one."""

    def __init__(self, query: Query) -> None:
        self._query = query

    def q(self, sql: str, args: Iterable[object] = ()) -> list[Any]:
        return list(self._query(sql, tuple(args)))

    def get_meta(self, key: str) -> str | None:
        rows = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None


def spend_of(query: Query, pricing: Pricing) -> Spend:
    """What the store spent, as the budget gate counts it at its next assessment with nothing in flight."""
    view = _View(query)
    marks = reply_cap.unheld(view)
    taken_off = frozenset(m for m in pricing.models if reply_cap.is_unheld(marks, m))
    costs = attempt_costs(view, pricing.models, pricing.limits, taken_off=taken_off)
    probes, ledger = probe_spend.charged(view), lost_spend.read(view)
    if probes is None or ledger is None:
        return Spend(None, dict.fromkeys(pricing.models))
    costs += lost_spend.costs(ledger, pricing.models, pricing.limits, taken_off=taken_off)
    total = spent_usd(costs)
    per_model = {}
    for model in sorted(pricing.models):
        spent = spent_usd(costs, model)
        per_model[model] = None if spent is None else spent + probes.get(model, 0.0)
    return Spend(None if total is None else total + sum(probes.values()), per_model)


def _moment(text: Any) -> dt.datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def window_of(query: Query) -> tuple[str, str] | None:
    """The store's window, `(first start, last attempt time)` as ISO 8601 UTC text; None without a recorded start."""
    rows = list(query("SELECT value FROM meta WHERE key=?", (META_STARTED,)))
    start = _moment(rows[0][0]) if rows else None
    if start is None:
        return None
    end = start
    for row in query("SELECT started_utc, ended_utc FROM attempts"):
        for value in row:
            moment = _moment(value)
            if moment is not None and moment > end:
                end = moment
    return _text(start), _text(end)


def _text(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def overlaps(earlier: tuple[str, str], later: tuple[str, str]) -> bool:
    """Whether a later window began before an earlier one ended."""
    begun, ended = _moment(later[0]), _moment(earlier[1])
    return begun is None or ended is None or begun < ended


def released_of(query: Query) -> dict[str, float]:
    """What releases added to each ceiling, by scope (`ALL` for the total); empty when nothing was released."""
    rows = list(query("SELECT value FROM meta WHERE key=?", (RELEASED_KEY,)))
    try:
        value = json.loads(rows[0][0]) if rows else {}
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): float(v) for k, v in value.items() if isinstance(v, int | float) and not isinstance(v, bool)}


def spend_json(spend: Spend) -> dict[str, Any]:
    """The spend as text compares it: a figure rounded to whole micro-dollars, so two reads of one store agree."""
    return {
        "spent_usd": _rounded(spend.total),
        "per_model_spent_usd": {model: _rounded(usd) for model, usd in sorted(spend.per_model.items())},
    }


def _rounded(usd: float | None) -> float | None:
    return None if usd is None else round(usd, 6)


__all__ = ["Pricing", "Query", "Spend", "overlaps", "released_of", "spend_json", "spend_of", "window_of"]
