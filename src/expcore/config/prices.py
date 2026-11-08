# Provenance: new
"""What a model's tokens cost, so a campaign's budget can be enforced and not only estimated (C12).

Prices are US dollars per million tokens, read from the model table. A model is *priced* when it has a price, or when
its entry says it is free (a model the study hosts itself costs nothing per token). A model that is neither has an
unknown cost: the budget gate holds it exactly as it holds a model that is over budget, and a study run refuses to
start with one (`ModelTable.require_priced`).
"""

import dataclasses
import math

TOKENS_PER_PRICE_UNIT = 1_000_000


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

    def dearest_per_token(self) -> float:
        """Return the highest cost in dollars of one token of any kind: the rate an unknown count is bounded at."""
        rates = (
            self.input_per_mtok,
            self.output_per_mtok,
            self.cache_write_per_mtok or 0.0,
            self.cache_read_per_mtok or 0.0,
        )
        return max(rates) / TOKENS_PER_PRICE_UNIT
