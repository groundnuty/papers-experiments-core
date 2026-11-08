# Provenance: new
"""One canonical form for hashing plain values, shared by the recorder and the replay so both hash the same bytes."""

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    """Return the canonical JSON text of a value: sorted keys, no spaces, ASCII only (an unpaired surrogate hashes)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    """Return the SHA-256 of a value's canonical form."""
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def plain(value: Any) -> Any:
    """Return a copy of a value reduced to what JSON holds, so it compares and hashes the same after a round trip."""
    return json.loads(json.dumps(value, default=str))
