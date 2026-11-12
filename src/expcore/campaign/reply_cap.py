# Provenance: new
"""Whether a model's replies are held to the reply maximum, which decides the bound its runs are reserved (C27).

A run's reservation counts at most the rounds times the reply maximum R as output, and its request past the token cap
at most one reply of R, only for a model whose every reply the endpoint holds to the `max_tokens` it is sent; for any
other the context window alone bounds a reply (`attempt_bounds`, C25). That is not assumed from the backend's name. A
model **holds its replies to R** in a campaign only when both are true:

- the core's own factory builds its backend (`default_backend_factory`), an OpenAI-compatible or Anthropic adapter,
  which sends R as `max_tokens` in every request; a study's plugin factory may wrap that adapter or replace it;
- the admission probe showed the endpoint applies it (`expcore.models.admission.reply_cap_unshown`): a request with a
  small `max_tokens` that asks for a long reply came back cut at that limit, its output count within it.

The probe is sent only with a budget, at each start of the campaign. A model it does not show, and a model one of
whose replies later reports more output than R (a `reply_over_maximum` event), is marked in the store (`unheld`),
and from then on its runs are reserved the bound by the window for the rest of the campaign, a resume included: the
attempts in flight, settled ones whose usage is unknown, whatever bound their rows record they were reserved, and
every top-up (C26). The reply that broke the rule is itself outside the guarantee; every request after it is covered.
Short of a mark, a settled attempt whose usage is unknown keeps the bound its row records, so a start whose probes
have not yet run, or that shows a model held which an attempt was not reserved as, charges it the same.
"""

import json
from collections.abc import Mapping
from typing import Any

from expcore.campaign.executor import BackendFactory, default_backend_factory
from expcore.campaign.store import Store
from expcore.config.models import ModelSpec

UNHELD_KEY = "reply_cap_unheld"  # model -> {"since_utc", "reason"}: models whose replies are not shown held to R
# The core's adapters, which send the reply maximum as `max_tokens` in every request; only for them can the probe show
# that the endpoint applies it.
REPLY_CAP_BACKENDS = ("openai_compat", "anthropic")


def probed(spec: ModelSpec, factory: BackendFactory, with_budget: bool) -> bool:
    """Tell whether admission sends the model the reply-maximum probe: only with a budget, which is all it serves, and
    for a backend of the core's own factory that sends the maximum."""
    return with_budget and factory is default_backend_factory and spec.backend in REPLY_CAP_BACKENDS


def unheld(store: Store) -> dict[str, Any]:
    """The models the campaign does not hold to the reply maximum, with when and why; one that cannot be read is
    taken as every model, so a damaged store reserves more, never less."""
    raw = store.get_meta(UNHELD_KEY)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"*": {"reason": f"the store's {UNHELD_KEY} cannot be read"}}
    return value if isinstance(value, dict) else {"*": {"reason": f"the store's {UNHELD_KEY} is not a mapping"}}


def is_unheld(marks: Mapping[str, Any], model: str) -> bool:
    """Tell whether the marks read from the store take the model off the reply maximum."""
    return model in marks or "*" in marks


def mark_unheld(store: Store, model: str, now: str, kind: str, detail: str) -> None:
    """Take the model off the reply maximum for the rest of the campaign, with an event; the first reason is kept."""

    def change(current: str | None) -> tuple[str | None, list[tuple[str, str | None, str]], None]:
        try:
            marks = json.loads(current) if current else {}
        except ValueError:
            marks = {}
        if not isinstance(marks, dict):
            marks = {}
        event = [(kind, model, detail)]
        if model in marks:
            return None, event, None
        marks[model] = {"since_utc": now, "reason": detail}
        return json.dumps(marks, sort_keys=True), event, None

    store.update_meta(UNHELD_KEY, change, now)
