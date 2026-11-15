# Provenance: new
"""What an abandoned request can cost: the tight bound, from the entry the record keeps (B19).

When the loop gives up a request already in flight, the record says `backend_info.abandoned_request`, now with the
bound the adapter took when it built that request (`expcore.models.request_bound`, which derives the prompt bound and
lists its assumptions): `prompt_bound_tokens` and how it was reached (`prompt_bound_by`: `bytes`, `window` or `none`),
the `reply_max_tokens` the request carried, and what the conversation tested (`bound_checked`, `bound_exceeded`,
`bound_unreported`, `bound_overlapped`). This module prices it. **It is recorded only: the budget gate never reads
it.** The gate's reservation is the bound of C25 and C27, which already covers every request of an attempt, this one
included; a study that charges an abandoned run for the request it gave up (a cost scorer, an adverse bound on a cost
contrast) calls `abandoned_request_bound` instead of `W x p_in + R x p_out`, and reports which basis it used.

**The bound, P the prompt bound and W the context window** (`ModelSpec.context_tokens`), at the dearest rates of the
tiers a prompt of P tokens can fall in (a tier takes its prompt's whole request, reply included, C24):
- For an attempt **held to its reply maximum** (C27: `held_to_reply_maximum` reads it from the record's totals), the
  reply is at most R, the `max_tokens` the request carried: `P x p_in + R x p_out`, and P + R tokens.
- For any other, nothing holds the reply but the window, which holds the prompt and the reply together: at most W
  tokens, and `max over a prompt of at most P of (prompt x p_in + (W - prompt) x p_out)`, which is
  `W x p_out + P x max(0, p_in - p_out)`.
Where the entry's basis is `window`, `none`, or absent (a record from before the bound, a backend of a study's own), P
is W, and the answer is the loose bound: for a flat price and a model held to R it is `attempt_bounds.overshoot_usd`.
P is never taken above W. The dollars are `None` for a model whose price is unknown (C12) and 0 for a free one.

The prompt bound is sound only under the assumptions `expcore.models.request_bound` states (a token covers at least a
byte, a reply's reported prompt is the whole prompt, history only grows); the entry's `bound_exceeded` is the tripwire,
and a conversation that tripped it is bounded by the window from then on. The bound on the reply is sound only where
the campaign showed the endpoint applies `max_tokens` (C27), which is why a caller says whether the attempt was held.

**What the tight bound rests on.** The basis returned also carries the tripwire's account of the run so far, for the
model of the entry: the requests it had checked (`tripwire_checked`) and the three counts that mean the tripwire was
not clean, kept apart: `tripwire_exceeded` (an earlier reply reported a prompt above its bound), `tripwire_unreported`
(a reply left a count out) and `tripwire_overlapped` (a request was built while another was in flight, the
abandoned one included).
`tripwire_fired_before` is true when any of the three is above zero. The entry is one model's, in one run, since the
adapters keep one tracker per backend and start it over with each conversation, so the account is per model per run and
nothing of another model's is in it. A count the entry lacks (a record from before the bound, a backend of a study's
own) is None and never 0: a run that was never tested has not passed every test, and `tripwire_fired_before` is None
unless a nonzero count answers it or all three are present. An entry that fired `bound_exceeded` or `bound_overlapped`
arrives bounded by the window, so there the account says why.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.config.models import Limits, ModelSpec
from expcore.config.prices import TOKENS_PER_PRICE_UNIT, Price, TieredPrice
from expcore.models.request_bound import BOUND_BASES, BY_BYTES

REPLY_BY_MAXIMUM = "reply_maximum"  # the reply is bounded by the max_tokens the request carried
REPLY_BY_WINDOW = "window"  # the prompt and the reply together are bounded by the window


@dataclass(frozen=True)
class AbandonedBound:
    """The most an abandoned request can cost: its tokens and its dollars, and which bound said so.

    `request_bound_usd` is None for a model whose price is unknown, 0.0 for a free one. `prompt_by` is `bytes` when the
    prompt was bounded by the last reported prompt and the bytes appended since, and `window` when by the model's
    window; `reply_by` is `reply_maximum` when the reply was bounded by the `max_tokens` the request carried and
    `window` when only the window held the request.

    The `tripwire_*` fields say what a tight bound rests on, for the model and the run the entry belongs to.
    `tripwire_checked` counts the earlier requests bounded by bytes whose reply was tested against its bound. The other
    three count what was not clean: `exceeded`, replies that reported a prompt above their bound; `unreported`, replies
    that left a count out; `overlapped`, requests built while another was in flight, the abandoned one included. Each
    is None ("not recorded") when the entry lacks it, and never 0.
    """

    request_bound_tokens: int
    request_bound_usd: float | None
    prompt_by: str
    reply_by: str
    tripwire_checked: int | None = None
    tripwire_exceeded: int | None = None
    tripwire_unreported: int | None = None
    tripwire_overlapped: int | None = None

    @property
    def tripwire_fired_before(self) -> bool | None:
        """Whether the tripwire had fired earlier in the run: True when an earlier reply exceeded its bound, left a
        count out, or a request overlapped another. False when all three counts are recorded and zero; None when
        none is above zero and one is not recorded, since an untested run has not passed."""
        counts = (self.tripwire_exceeded, self.tripwire_unreported, self.tripwire_overlapped)
        if any(count for count in counts if count is not None):
            return True
        return None if None in counts else False


def held_to_reply_maximum(totals: Mapping[str, Any]) -> bool:
    """Tell whether an attempt was held to its reply maximum, from the totals of its record (C27): the budget gate
    reserved it by the reply maximum and the totals say the campaign had not taken its model off it when the loop
    ended. A record that says less (no budget, one written before the fields, one with only the first) is not held,
    which can only charge more."""
    return totals.get("reserved_capped") is True and totals.get("reply_cap_unheld") is False


def _count(entry: Mapping[str, Any], name: str) -> int | None:
    """Read one count of the entry (tokens, requests, replies): a non-negative integer, or None when absent or null."""
    value = entry.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"abandoned_request.{name} is a non-negative integer or null, not {value!r}")
    return value


def _prompt_bound(entry: Mapping[str, Any], window: int | None, key: str) -> tuple[int, str]:
    """The prompt bound the entry gives, clamped to the window, and its basis; the window where the entry has none."""
    by = entry.get("prompt_bound_by", "window")
    if by not in BOUND_BASES:
        raise ValueError(f"abandoned_request.prompt_bound_by is one of {', '.join(BOUND_BASES)}, not {by!r}")
    claimed = _count(entry, "prompt_bound_tokens")
    if by == BY_BYTES:
        if claimed is None:
            raise ValueError("abandoned_request.prompt_bound_tokens is missing from an entry bounded by bytes")
        return (claimed if window is None else min(claimed, window)), "bytes"
    if window is None:
        raise _no_window(key)
    return window, "window"


def _no_window(key: str) -> ValueError:
    return ValueError(
        f"model {key!r} has no context_tokens, so nothing bounds the prompt of an abandoned request that is not "
        "bounded by bytes; give it the endpoint's context window in models.toml"
    )


def _reachable(price: Price | TieredPrice, prompt: int) -> list[tuple[float, float, int]]:
    """The (input rate, output rate, longest prompt) of each tier a prompt of at most `prompt` tokens can fall in.

    A tier takes the prompts above the one before's boundary and up to its own, so it is reachable when the one before
    ends below `prompt` (the first always is), and the longest prompt it can see is `prompt` or its own boundary.
    Rates are the dearest in the tier: the input or a cache price, whichever is higher.
    """
    if isinstance(price, Price):
        return [(price.input_ceiling_per_mtok(), price.output_ceiling_per_mtok(), prompt)]
    found, below = [], 0
    for tier in price.tiers:
        if not found or below < prompt:
            longest = prompt if tier.up_to_prompt_tokens is None else min(prompt, tier.up_to_prompt_tokens)
            found.append((tier.price.input_ceiling_per_mtok(), tier.price.output_ceiling_per_mtok(), longest))
        below = tier.up_to_prompt_tokens or prompt
    return found


def _usd(price: Price | TieredPrice, prompt: int, reply: int | None, window: int | None) -> float:
    """The most a request costs: held to a reply of `reply` tokens, or (`reply` None) with the prompt and the reply
    together within `window`; at the worst tier a prompt of at most `prompt` can fall in."""
    worst = 0.0
    for rate_in, rate_out, longest in _reachable(price, prompt):
        if reply is not None:
            cost = longest * rate_in + reply * rate_out
        else:
            assert window is not None  # the caller asked for the window
            cost = window * rate_out + longest * max(0.0, rate_in - rate_out)
        worst = max(worst, cost)
    return worst / TOKENS_PER_PRICE_UNIT


def abandoned_request_bound(
    spec: ModelSpec, limits: Limits, entry: Mapping[str, Any], *, reply_capped: bool = False
) -> AbandonedBound:
    """Return the most the request an attempt gave up can cost, from the `abandoned_request` entry of its record.

    `reply_capped` says whether the attempt was held to its reply maximum: `held_to_reply_maximum(record.totals)`.
    An entry without the bound fields (an older record, a backend of a study's own) is bounded by the window. The
    basis carries the entry's account of the tripwire (`tripwire_checked`, `tripwire_exceeded`, `tripwire_unreported`,
    `tripwire_overlapped`, `tripwire_fired_before`): None for what the entry lacks. Raises ValueError for an entry that
    is not what the adapters write, naming the field, and for a model with no `context_tokens` where the window is
    needed: a bound by bytes with a reply held to its maximum needs none.
    """
    if not isinstance(entry, Mapping):
        raise ValueError(f"abandoned_request is the record's entry for the request given up, not {entry!r}")
    window = spec.context_tokens
    prompt, prompt_by = _prompt_bound(entry, window, spec.key)
    if reply_capped:
        reply = _count(entry, "reply_max_tokens")
        reply = spec.output_tokens(limits.max_output_tokens) if reply is None else reply
        tokens, reply_by, held = prompt + reply, REPLY_BY_MAXIMUM, reply
    else:
        if window is None:
            raise _no_window(spec.key)
        tokens, reply_by, held = window, REPLY_BY_WINDOW, None
    if spec.free:
        usd: float | None = 0.0
    elif spec.price is None:
        usd = None
    else:
        usd = _usd(spec.price, prompt, held, window)
    return AbandonedBound(
        tokens,
        usd,
        prompt_by,
        reply_by,
        tripwire_checked=_count(entry, "bound_checked"),
        tripwire_exceeded=_count(entry, "bound_exceeded"),
        tripwire_unreported=_count(entry, "bound_unreported"),
        tripwire_overlapped=_count(entry, "bound_overlapped"),
    )
