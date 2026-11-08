# Provenance: new
"""Token usage counted the moment a reply arrives, and never invented (B10).

A reply is billed even when reading it later fails, so the tally is updated before any parsing can raise. A count
the reply did not carry stays unknown (None), never 0, so a mean over runs excludes it instead of being pulled down.
"""

from dataclasses import dataclass
from typing import Any


def token_count(usage: Any, name: str) -> int | None:
    """Read one non-negative integer count from a usage object; None when absent, null or not a count."""
    value = getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass
class UsageTally:
    """Totals of what the replies of one backend reported."""

    responses: int = 0
    input_tokens: int | None = None  # None until some reply reports it
    output_tokens: int | None = None
    missing: int = 0  # replies that carried no usage at all: their tokens are unknown

    def note(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Count one reply; a count the reply left out adds nothing and a reply with neither is counted missing."""
        self.responses += 1
        if input_tokens is None and output_tokens is None:
            self.missing += 1
        if input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + input_tokens
        if output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + output_tokens

    def as_dict(self) -> dict[str, int | None]:
        """Return the totals for the record."""
        return {
            "responses": self.responses,
            "input": self.input_tokens,
            "output": self.output_tokens,
            "missing": self.missing,
        }
