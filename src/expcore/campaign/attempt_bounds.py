# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/attempt_bounds.py, blob aa745b2e86b2bd41637333a5a7ec1a9256542c1f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""What an attempt cost, and the bound charged for one that left no count (C05, C25, C26, C27).

An attempt killed before its outcome was recorded is `interrupted`: no counts, no transcript. It may have spent anything
up to what its caps allow, so it is **charged its bound**, never 0. The same goes for an attempt whose usage is known to
be short (an abandoned request) or was never reported. A bounded attempt is part of what has been spent, and no part of
what a run costs: **the mean cost of a run leaves bounded attempts out**, so a kill does not make a model look dear to
the projection, and its bound does not make it look cheap. The budget gate reserves the same bound for every run it
starts or has in flight.

**The bound is the most the caps let one attempt cost** (C25), from the limits it ran under (`Limits`, the campaign's):
- The token cap counts each reply's input and output tokens, the whole prompt with its cached part, summed over the
  attempt's requests. The loop checks it before each request and after each reply, so a request goes out whenever the
  tokens used are below the cap, and its prompt and reply then count: a run can pass its cap by one request. The
  requests before that one used less than the cap, and that one's prompt is within the model's context window W and
  its reply within the reply maximum R (the study's `max_output_tokens`, or the model's own when lower).
- The loop makes at most one request more than its tool rounds: each reply with tool calls is a round, and the reply
  after the last round ends the run. So the requests before the last carry at most O = rounds x R output tokens.
- For a model whose replies are **held to R** (`reply_capped`), the requests within the cap cost at most the worst of
  `input + output <= cap`, `output <= O` at the dearest rates of any tier (input at the dearest of the input and cache
  prices): `O x p_out + (cap - O) x p_in` when output is the dearer and `O <= cap`, `cap x max(p_in, p_out)` otherwise.
  That is never above the whole cap at the dearest rate. For any other model, the whole cap at the dearest rate.
- The request past the cap adds at most `W x p_in + R x p_out` for a model held to R, and `W x max(p_in, p_out)` for
  any other, whose prompt and reply together only the window holds. Both need the model's `context_tokens`: a run with
  a budget does not start or resume with a priced model without one (`ModelTable.require_context_window`).

Whether a model is held to R is the caller's to say, and the default is no: the backend's name says what the entry
asks for, not what serves it. The campaign says yes only for a backend of the core's own factory whose endpoint the
admission probe showed applies the `max_tokens` it is sent, and no again for the rest of the campaign once a reply
reports more than R (`expcore.campaign.reply_cap`, C27). The bound holds while the endpoint refuses a request beyond
its window, and, for a model held to R, applies that `max_tokens`; a reply the core finds past R is itself outside the
guarantee. A stub's scripted usage beyond the model's context window, or a backend of the study's own whose request
outgrows it, is outside it too. A free model's bound is 0. A model whose cost is unknown (no price, not free) has no
cost at all: `None`, never 0, and the budget gate holds it (C12).

**An attempt whose usage is unknown is bounded as its run was reserved** (C27): by the budget gate's decision when it
let the run start, recorded on the attempt's row at dispatch, so a restart charges it the same before admission has
shown any model held to R. The row records the decision, not the dollars: the bound follows from it, the model's
table entry and the limits, which the campaign's fingerprint fixes, and a model the store takes off R afterwards takes
its attempts to the bound by the window too, since the reply past R may have been one of theirs. A row from before
the record follows the caller's view of the model now, which can only charge more before admission has run.

A measured attempt of a model with a flat price costs its totals at that price. One of a model with a tiered price
costs each of its requests at the tier its prompt fell in, from the per-turn usage the store keeps (C24); an attempt
whose per-turn usage is unknown is charged its bound, since its totals cannot say which tier each token fell in.

**A send the token cap cannot account for is charged a top-up** (C26): a send that failed after it may have been
billed, and every request after a reply that left a count out. Each top-up is the bound of one request, the same as
the request past the cap (`request_bound_usd`), charged when it is reserved and never refunded, since what that send
cost is unknown. An attempt costs what is said above plus its top-ups, so a run's true cost is never more than its
reservation and its top-ups together.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from expcore.campaign.export import RunState
from expcore.campaign.store import Store, column_for
from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import TOKENS_PER_PRICE_UNIT, TieredPrice

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


def _check_limits(limits: Limits) -> None:
    """Refuse limits that bound nothing: the cap and the reply maximum positive, the rounds a count."""
    for name in ("token_budget", "max_output_tokens", "max_tool_rounds"):
        value = getattr(limits, name)
        least = 0 if name == "max_tool_rounds" else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            raise ValueError(f"the {name} of an attempt is an integer of at least {least}, not {value!r}")


def _worst_within(cap: int, most_output: int, input_rate: float, output_rate: float) -> float:
    """The most `input + output <= cap` tokens with `output <= most_output` cost, in tokens times dollars per million.

    The cost is linear in the two counts, so its maximum over that region is at a corner: all of the cap as input, or
    as much of it as output as the replies allow and the rest as input (all output when they allow the whole cap).
    """
    output = min(most_output, cap)
    return max(cap * input_rate, (cap - output) * input_rate + output * output_rate)


def _rates(spec: ModelSpec, limits: Limits) -> tuple[float, float]:
    """The dearest input and output rates of a priced model in any tier, in dollars per million tokens."""
    _check_limits(limits)
    if spec.price is None:
        raise ValueError(f"model {spec.key!r} has no price, so nothing bounds its cost")
    return spec.price.input_ceiling_per_mtok(), spec.price.output_ceiling_per_mtok()


def _within_cap(spec: ModelSpec, limits: Limits, reply_capped: bool) -> float:
    """The most the requests within the token cap can cost, in tokens times dollars per million."""
    input_rate, output_rate = _rates(spec, limits)
    cap = limits.token_budget
    if not reply_capped:  # nothing is shown to hold a reply: the whole cap at the dearest rate
        return cap * max(input_rate, output_rate)
    most_output = limits.max_tool_rounds * spec.output_tokens(limits.max_output_tokens)
    return _worst_within(cap, most_output, input_rate, output_rate)


def _past_cap(spec: ModelSpec, limits: Limits, reply_capped: bool) -> float:
    """The most the request a run sends below its cap and ends past it can cost, in tokens times dollars per million:
    for a model held to the reply maximum, a prompt within the context window and a reply within that maximum; for any
    other, prompt and reply together within the window, at the dearer rate."""
    input_rate, output_rate = _rates(spec, limits)
    if spec.context_tokens is None:
        raise ValueError(
            f"model {spec.key!r} has no context_tokens, so nothing bounds the request a run can make past its token "
            "cap; give it the endpoint's context window in models.toml"
        )
    if not reply_capped:  # nothing is shown to hold the reply: only the window holds the request
        return spec.context_tokens * max(input_rate, output_rate)
    return spec.context_tokens * input_rate + spec.output_tokens(limits.max_output_tokens) * output_rate


def cap_bound_usd(spec: ModelSpec, limits: Limits, *, reply_capped: bool = False) -> float:
    """The most a priced model's requests within the token cap can cost, in dollars (the module docstring says how)."""
    return _within_cap(spec, limits, reply_capped) / TOKENS_PER_PRICE_UNIT


def overshoot_usd(spec: ModelSpec, limits: Limits, *, reply_capped: bool = False) -> float:
    """The most the one request past the token cap can cost, in dollars, at the dearest rates of any tier."""
    return _past_cap(spec, limits, reply_capped) / TOKENS_PER_PRICE_UNIT


def request_bound_usd(spec: ModelSpec, limits: Limits, *, reply_capped: bool = False) -> float | None:
    """Return what one request of the model can cost, in dollars: the bound of a top-up (C26), which is the request
    past the token cap's; 0 for a free model, None when the price is unknown."""
    _check_limits(limits)
    if spec.free:
        return 0.0
    if spec.price is None:
        return None
    return overshoot_usd(spec, limits, reply_capped=reply_capped)


def probe_usd(spec: ModelSpec, limits: Limits, input_tokens: int, output_tokens: int) -> float:
    """Return what a reply that reported its counts cost, in dollars, at the dearest rates of any tier: an admission
    probe's reply says neither how much of its prompt was cached nor which tier priced it, so the dearest rates bound
    it (C26). The model must be priced."""
    input_rate, output_rate = _rates(spec, limits)
    return (input_tokens * input_rate + output_tokens * output_rate) / TOKENS_PER_PRICE_UNIT


def bound_usd(spec: ModelSpec, limits: Limits, *, reply_capped: bool = False) -> float | None:
    """Return the bound of one attempt: the requests within its token cap and the one past it at their worst (C25),
    by the reply maximum only for a model held to it (C27); 0 for a free model, None when the price is unknown. A
    priced model with no context window is refused."""
    _check_limits(limits)
    if spec.free:
        return 0.0
    if spec.price is None:
        return None
    return (_within_cap(spec, limits, reply_capped) + _past_cap(spec, limits, reply_capped)) / TOKENS_PER_PRICE_UNIT


def measured_usd(spec: ModelSpec, row: Mapping[str, Any]) -> float | None:
    """Return the cost of an attempt from its counts, or None if the model's price is unknown or a count is missing.

    A count the endpoint did not report is unknown, never zero, so the caller bounds the attempt instead. A flat price
    is applied to the attempt's totals. A tiered price is applied to each request at the tier its prompt fell in, from
    the per-turn usage the store keeps (C24); without it, or with a turn that did not report its prompt or its reply,
    the cost is unknown, since totals cannot say which tier each token fell in. An attempt whose per-turn usage is
    known to hold no turn (a budget hold or a failed send before any reply, a run that found an outage before it
    started) costs 0 at either price: no request was answered, and a send that may have been billed is a top-up.
    """
    if spec.free:
        return 0.0
    if isinstance(spec.price, TieredPrice):
        requests = _requests(row["turn_usage"])
        return None if requests is None else spec.price.requests_usd(requests)
    if spec.price is None:
        return None
    if row["input_tokens"] is None and row["output_tokens"] is None and _requests(row["turn_usage"]) == []:
        return 0.0  # no request was answered: no turn, so nothing reported and nothing billed beyond its top-ups
    if row["input_tokens"] is None or row["output_tokens"] is None:
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


def attempt_costs(
    store: RunState,
    models: Mapping[str, ModelSpec],
    limits: Limits,
    *,
    reply_capped: Collection[str] = (),
    taken_off: Collection[str] = (),
) -> list[AttemptCost]:
    """Return the cost of every attempt that spent tokens, in attempt order, each charged as the module docstring says.

    `models` maps a run's model key to its table entry; a key it lacks is an unknown cost. `limits` are the caps the
    attempts ran under, which bound one whose usage is unknown, by the bound its row records it was reserved at
    dispatch (C27). A row without that record, written before it existed, is bounded by the reply maximum for the
    models in `reply_capped` (the caller's view now) and by the window for any other. Either way an attempt of a model
    in `taken_off`, which the store has taken off the reply maximum, is bounded by the window. Each attempt's top-ups
    are added to its cost (C26), and a running attempt with top-ups is listed with them alone, since they were charged
    when they were reserved; an attempt with a top-up is `bounded`, its cost being in part a ceiling.
    """
    states = (*BILLED_STATES, "running")
    marks = ",".join("?" * len(states))
    # A read-only snapshot of a store made before a column existed cannot be migrated: read it without the column.
    columns = {row["name"] for row in store.q("PRAGMA table_info(attempts)")}
    turn_usage = "a.turn_usage" if "turn_usage" in columns else "NULL AS turn_usage"
    topup_usd = "a.topup_usd" if "topup_usd" in columns else "0 AS topup_usd"
    reserved = "a.reserved_capped" if "reserved_capped" in columns else "NULL AS reserved_capped"
    rows = store.q(
        "SELECT a.id, a.run_key, a.state, r." + column_for("model") + " AS model, a.input_tokens, a.output_tokens, "
        f"a.cache_creation_tokens, a.cache_read_tokens, {turn_usage}, COALESCE(a.usage_complete,1) AS complete, "
        f"{topup_usd}, {reserved} FROM attempts a JOIN runs r USING(run_key) WHERE a.state IN ({marks}) ORDER BY a.id",
        states,
    )
    costs = []
    for row in rows:
        spec, topped = models.get(row["model"]), float(row["topup_usd"] or 0.0)
        if row["state"] == "running":
            if topped:
                costs.append(AttemptCost(row["id"], row["run_key"], row["model"], "running", topped, True))
            continue
        usd, bounded = _base_cost(spec, row, limits, _reserved_capped(row, reply_capped, taken_off))
        costs.append(
            AttemptCost(
                row["id"], row["run_key"], row["model"], row["state"],
                None if usd is None else usd + topped, bounded or topped > 0,
            )
        )  # fmt: skip
    return costs


def _reserved_capped(row: Mapping[str, Any], reply_capped: Collection[str], taken_off: Collection[str]) -> bool:
    """Tell whether an attempt is bounded by the reply maximum: as its row records it was reserved at dispatch, else as
    the caller holds its model now, and never once the store has taken its model off the maximum (C27)."""
    recorded = row["reserved_capped"]
    capped = row["model"] in reply_capped if recorded is None else bool(recorded)
    return capped and row["model"] not in taken_off


def _base_cost(
    spec: ModelSpec | None, row: Mapping[str, Any], limits: Limits, reply_capped: bool
) -> tuple[float | None, bool]:
    """An attempt's cost without its top-ups, and whether it is its bound: measured when its counts are known and
    complete, else its bound; None for a model the table lacks or whose price is unknown."""
    if spec is None:
        return None, False
    measured = measured_usd(spec, row) if row["complete"] else None
    if measured is not None:
        return measured, False
    bound = bound_usd(spec, limits, reply_capped=reply_capped)
    return bound, bound is not None and not spec.free


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
