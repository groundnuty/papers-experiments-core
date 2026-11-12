# Provenance: new
"""Token usage counted the moment a reply arrives, and never invented (B10).

A reply is billed even when reading it later fails, so the tally is updated before any parsing can raise. A count
the reply did not carry stays unknown (None), never 0, so a mean over runs excludes it instead of being pulled down.

A provider may report more in its usage object than the two counts the budget uses: an endpoint's own credit count,
cache reads and writes, reasoning tokens. `extra_usage` keeps those numeric fields per reply, under the provider's own
names, for the record only. They change no budget arithmetic, a field the reply leaves out is absent and never 0, and
what is kept is bounded (a capped number of fields, each a plain name and a number) so an odd or hostile endpoint cannot
grow a record without limit.
"""

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

# The counts each wire format's usage object carries that a turn already holds in fields of its own: they are not
# repeated among the other fields. `total_tokens` is the sum of the two counts.
OPENAI_COUNTED = ("prompt_tokens", "completion_tokens", "total_tokens")
ANTHROPIC_COUNTED = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

USAGE_EXTRA_CAP = 32  # the most fields one reply adds to its turn; the rest are counted as cut, not kept
USAGE_EXTRA_NAME_MAX = 80  # characters in a field's name
USAGE_EXTRA_INT_LIMIT = 2**63  # an integer field lies in [-limit, limit): 64 bits, so a record's numbers stay small
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")  # one dot at most: parent.child


def token_count(usage: Any, name: str) -> int | None:
    """Read one non-negative integer count from a usage object; None when absent, null or not a count."""
    value = getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usage_name_ok(name: Any) -> bool:
    """Tell whether a name may stand for an extra usage field: a plain ASCII identifier, or two joined by one dot."""
    return isinstance(name, str) and len(name) <= USAGE_EXTRA_NAME_MAX and _NAME.fullmatch(name) is not None


def usage_number_ok(value: Any) -> bool:
    """Tell whether a value may be an extra usage field: an int of 64 bits or a finite float, never a bool."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -USAGE_EXTRA_INT_LIMIT <= value < USAGE_EXTRA_INT_LIMIT
    return isinstance(value, float) and math.isfinite(value)


def _fields_of(obj: Any) -> dict[str, Any] | None:
    """The named fields of a mapping, of an SDK model (its dump) or of a plain object's attributes; None otherwise."""
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        dumped = dump()
        return dumped if isinstance(dumped, dict) else None
    try:
        return dict(vars(obj))
    except TypeError:  # a number, text, a list or None has no attributes of its own
        return None


def _offered(name: str, value: Any) -> dict[str, Any]:
    """What one field offers: itself when it is a number, else the fields of the object it holds, one level down."""
    if usage_number_ok(value):
        return {name: value}
    inner = _fields_of(value) or {}
    return {f"{name}.{k}": v for k, v in inner.items() if isinstance(k, str)}


def extra_usage(usage: Any, skip: Collection[str] = ()) -> tuple[dict[str, int | float], int]:
    """Return the numeric fields of a usage object other than those in `skip`, and how many the cap cut.

    A field is kept under its own name when its value is a number (`usage_number_ok`) and its name a plain identifier
    (`usage_name_ok`); an object one level down is read the same way under `parent.child`, so a reasoning count that an
    endpoint nests under `completion_tokens_details` is `completion_tokens_details.reasoning_tokens`. Text, flags, null,
    lists, non-finite numbers and names that are not plain are left out, and are not counted as cut. More than
    `USAGE_EXTRA_CAP` fields keep the first by name, so the same reply always keeps the same ones, and the number left
    out is the second value. A usage that is absent, or holds no such field, gives `({}, 0)`.
    """
    fields = _fields_of(usage)
    if fields is None:
        return {}, 0
    found: dict[str, int | float] = {}
    for name in sorted(k for k in fields if isinstance(k, str)):
        for key, number in _offered(name, fields[name]).items():
            if key not in skip and usage_name_ok(key) and usage_number_ok(number):
                found.setdefault(key, number)
    names = sorted(found)
    kept = names[:USAGE_EXTRA_CAP]
    return {n: found[n] for n in kept}, len(names) - len(kept)


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
