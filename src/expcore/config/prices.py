# Provenance: new
"""What a model's tokens cost, so a campaign's budget can be enforced and not only estimated (C12).

Prices are US dollars per million tokens, read from the model table. A model is *priced* when it has a price, or when
its entry says it is free (a model the study hosts itself costs nothing per token). A model that is neither has an
unknown cost: the budget gate holds it exactly as it holds a model that is over budget, and a study run refuses to
start with one (`ModelTable.require_priced`).

A price is flat (`Price`), or tiered by the length of a request's prompt (`TieredPrice`, C24): each tier has a
prompt-token boundary and its own input, output and cache prices, and a request takes, for its prompt and its reply
alike, the prices of the tier its prompt fell in. A tiered price is therefore applied request by request, from each
request's own counts, never to an attempt's totals.
"""

import dataclasses
import math
from collections.abc import Iterable

TOKENS_PER_PRICE_UNIT = 1_000_000

# One request's counts: input tokens (the whole prompt, cache included), output tokens, and the cache writes and reads
# that are parts of the input.
RequestCounts = tuple[int, int, int, int]


@dataclasses.dataclass(frozen=True)
class Price:
    """The price of a model's tokens in dollars per million.

    Cache writes and reads are a part of the input tokens (the store records them that way). A cache price that the
    entry leaves out is the input price, so a cached token is never counted as cheaper than the entry says it is.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None

    def __post_init__(self) -> None:
        """Refuse a price that is negative, not a number or infinite."""
        for name in ("input_per_mtok", "output_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok"):
            value = getattr(self, name)
            if value is None and name.startswith("cache"):
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number of dollars, not below zero, got {value!r}")

    def usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Return the cost in dollars of the given counts; the cache counts are parts of `input_tokens`."""
        cached = cache_creation_tokens + cache_read_tokens
        if min(input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens) < 0 or cached > input_tokens:
            raise ValueError("token counts must not be negative, and the cache counts are parts of the input tokens")
        write = self.input_per_mtok if self.cache_write_per_mtok is None else self.cache_write_per_mtok
        read = self.input_per_mtok if self.cache_read_per_mtok is None else self.cache_read_per_mtok
        total = (
            (input_tokens - cached) * self.input_per_mtok
            + cache_creation_tokens * write
            + cache_read_tokens * read
            + output_tokens * self.output_per_mtok
        )
        return total / TOKENS_PER_PRICE_UNIT

    def request_usd(self, input_tokens: int, output_tokens: int, cache_creation: int = 0, cache_read: int = 0) -> float:
        """Return the cost of one request; a flat price is the same for every prompt."""
        return self.usd(input_tokens, output_tokens, cache_creation, cache_read)

    def dearest_per_token(self) -> float:
        """Return the highest cost in dollars of one token of any kind: the rate an unknown count is bounded at."""
        return max(self.input_ceiling_per_mtok(), self.output_ceiling_per_mtok()) / TOKENS_PER_PRICE_UNIT

    def input_ceiling_per_mtok(self) -> float:
        """Return the dearest rate an input token can be charged at: the input price or a cache price, whichever is
        higher (a cache price left out is the input price)."""
        return max(self.input_per_mtok, self.cache_write_per_mtok or 0.0, self.cache_read_per_mtok or 0.0)

    def output_ceiling_per_mtok(self) -> float:
        """Return the rate of an output token."""
        return self.output_per_mtok


@dataclasses.dataclass(frozen=True)
class PriceTier:
    """One tier of a tiered price: the prices of a request whose prompt is at most `up_to_prompt_tokens` tokens long
    and longer than the tier before's boundary; the last tier has no boundary and takes every longer prompt."""

    up_to_prompt_tokens: int | None
    price: Price


@dataclasses.dataclass(frozen=True)
class TieredPrice:
    """A price that depends on the prompt length of each request (C24): two or more tiers, by increasing boundary."""

    tiers: tuple[PriceTier, ...]

    def __post_init__(self) -> None:
        """Refuse tiers that do not price every prompt exactly once."""
        tiers = tuple(self.tiers)
        object.__setattr__(self, "tiers", tiers)
        if len(tiers) < 2:
            raise ValueError("a tiered price needs at least two tiers; one price for every prompt is a flat price")
        if not all(isinstance(tier, PriceTier) and isinstance(tier.price, Price) for tier in tiers):
            raise ValueError("every tier is a PriceTier holding a flat Price")
        *bounded, last = tiers
        if last.up_to_prompt_tokens is not None:
            raise ValueError("the last tier takes every longer prompt, so it has no up_to_prompt_tokens")
        previous = 0
        for tier in bounded:
            bound = tier.up_to_prompt_tokens
            if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
                raise ValueError(f"each tier but the last needs up_to_prompt_tokens, a positive integer, not {bound!r}")
            if bound <= previous:
                raise ValueError("the tiers' up_to_prompt_tokens must increase from one tier to the next")
            previous = bound

    def for_prompt(self, prompt_tokens: int) -> Price:
        """Return the prices of the tier a prompt of this many tokens falls in."""
        for tier in self.tiers:
            if tier.up_to_prompt_tokens is None or prompt_tokens <= tier.up_to_prompt_tokens:
                return tier.price
        raise AssertionError("unreachable: the last tier has no boundary")  # __post_init__ guarantees it

    def usd(self, *counts: int) -> float:
        """Refuse to price totals: which tier a token falls in depends on its own request's prompt."""
        raise ValueError("a tiered price is applied per request, from each request's own counts, never to totals")

    def request_usd(self, input_tokens: int, output_tokens: int, cache_creation: int = 0, cache_read: int = 0) -> float:
        """Return the cost of one request at the tier its prompt (its input tokens, cache included) fell in."""
        return self.for_prompt(input_tokens).usd(input_tokens, output_tokens, cache_creation, cache_read)

    def dearest_per_token(self) -> float:
        """Return the highest cost of one token of any kind in any tier."""
        return max(tier.price.dearest_per_token() for tier in self.tiers)

    def input_ceiling_per_mtok(self) -> float:
        """Return the dearest rate of an input token in any tier."""
        return max(tier.price.input_ceiling_per_mtok() for tier in self.tiers)

    def output_ceiling_per_mtok(self) -> float:
        """Return the dearest rate of an output token in any tier."""
        return max(tier.price.output_ceiling_per_mtok() for tier in self.tiers)

    def requests_usd(self, requests: Iterable[RequestCounts]) -> float:
        """Return the cost of a run's requests, each at the tier its own prompt fell in."""
        return sum(self.request_usd(*counts) for counts in requests)
