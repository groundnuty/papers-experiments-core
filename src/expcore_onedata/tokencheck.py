# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/tokencheck.py, blob 80f29a7b310adef07c5baf41fcb60aa05b133869
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Refuse a token that is more than read-only access to the study space (#6, science agent's addition).

`require_scoped_token(token, space_id)` asks Onezone to examine the token (`examine`, default: an admin-authenticated
POST /tokens/examine) and raises `TokenRefused` unless the examination shows ALL of:
  * a `data.readonly` caveat;
  * a `data.path` caveat whose whitelist is exactly [/<space_id>] (base64 of the path);
  * a `time` caveat that expires within `max_ttl_s` (default: one run's time cap + 15 min).
If the examination itself fails, the token is refused: unknown is not a pass. The sandbox and campaign call this function for
their run tokens too (`examine` is injectable, so a mock stands in for Onezone in tests).
"""
from __future__ import annotations

import base64
import time
from typing import Callable

from dataset_76.harness.config import load_config, required_token_ttl_s


def ttl_window(time_cap_s: float) -> tuple[int, int, int]:
    """(min, max, mint) lifetime in seconds for a run with this time cap: min = the shared rule (cap + margin),
    max = min + 10 min (not longer than needed), mint = min + 5 min (margin for the seconds before examination)."""
    mn = required_token_ttl_s(time_cap_s)
    return mn, mn + 600, mn + 300


# defaults for the cap in the frozen harness config; a run passes its own cap through time_cap_s
MIN_TTL_S, MAX_TTL_S, MINT_TTL_S = ttl_window(load_config().limits.time_cap_s)


class TokenRefused(RuntimeError):
    pass


def default_examine(token: str) -> dict:
    from . import live
    return live.examine(token)


def require_scoped_token(token: str, space_id: str, *, time_cap_s: float | None = None,
                         examine: Callable[[str], dict] | None = None, now: float | None = None) -> dict:
    """Returns {"token_id", "subject", "caveats"} (types only, never the value) when the token passes."""
    if not token:
        raise TokenRefused("empty token")
    min_ttl_s, max_ttl_s, _ = ttl_window(time_cap_s) if time_cap_s is not None else (MIN_TTL_S, MAX_TTL_S, MINT_TTL_S)
    try:
        info = (examine or default_examine)(token)
        caveats = list(info["caveats"])
        token_id = info["id"]
    except Exception as e:  # noqa: BLE001 -- any failure to examine is a refusal
        raise TokenRefused(f"token could not be examined ({type(e).__name__}); refusing it") from None
    if not any(c.get("type") == "data.readonly" for c in caveats):
        raise TokenRefused("token has no data.readonly caveat")
    paths = [c for c in caveats if c.get("type") == "data.path"]
    if len(paths) != 1:
        raise TokenRefused(f"token has {len(paths)} data.path caveats, expected exactly one")
    try:
        got = [base64.b64decode(w).decode() for w in paths[0]["whitelist"]]
    except Exception:  # noqa: BLE001
        raise TokenRefused("data.path caveat is malformed") from None
    if got != [f"/{space_id}"]:
        raise TokenRefused(f"data.path is {got}, expected ['/{space_id}']")
    times = [c for c in caveats if c.get("type") == "time"]
    if not times:
        raise TokenRefused("token has no time caveat")
    left = min(c["validUntil"] for c in times) - (time.time() if now is None else now)
    if left <= 0:
        raise TokenRefused("token already expired")
    if left < min_ttl_s:
        raise TokenRefused(f"token lifetime {left:.0f} s is shorter than the {min_ttl_s:.0f} s a run needs")
    if left > max_ttl_s:
        raise TokenRefused(f"token lifetime {left:.0f} s exceeds {max_ttl_s:.0f} s")
    return {"token_id": token_id, "subject": info.get("subject"), "caveats": sorted(c["type"] for c in caveats)}


def assert_distinct(plane_token_id: str, other_token_ids: list[str]) -> None:
    """The knowledge plane's principal is its own: its token id appears in no run token's id."""
    if plane_token_id in other_token_ids:
        raise TokenRefused("the knowledge plane shares a token with a run")


def assert_distinct_all(plane_token_ids: list[str], run_token_ids: list[str]) -> None:
    for t in plane_token_ids:
        assert_distinct(t, run_token_ids)
