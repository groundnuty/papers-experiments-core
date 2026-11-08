# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/gatepolicy.py, blob 79565813abc302773b3f2664848139108cc72333
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""Request policy of the sandbox gate: which Onedata REST calls pass (O05, O16).

Pure functions, no I/O, so the policy is unit-testable. `gate.py` (which runs in the gate container, with the standard
library only) applies it to every request the sandbox makes.

Only calls the official REST client needs for listing, attribute lookup and reading are allowed, and only as reads (plus
the two POSTs that are lookups). Everything else is refused: writes, metadata, share and download-URL endpoints, other
hosts, dot segments and encoded separators. A content read with or without a Range header passes.

The earlier study's gate had two further modes (a whole-file download that is stored before it is released, and a
listing merged into one page). They were that study's manipulated variables and are not carried over: this gate is the
plain read-only path.
"""

from __future__ import annotations

import dataclasses
import re

ONEZONE, ONEPROVIDER = "onezone", "oneprovider"

_P = "/api/v3/"
_RULES = (
    # (role, method, regex, tag)
    (ONEZONE, "POST", re.compile(re.escape(_P) + r"onezone/tokens/infer_access_token_scope$"), "scope"),
    (ONEPROVIDER, "POST", re.compile(re.escape(_P) + r"oneprovider/lookup-file-id/[^/]+(/.*)?$"), "lookup"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+$"), "attrs"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+/children$"), "children"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+/content$"), "content"),
)
ALLOWED_TAGS = tuple(tag for *_, tag in _RULES)


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    tag: str = ""
    status: int = 403  # HTTP status of a refusal


def decide(role: str, method: str, path: str) -> Decision:
    """`path` is the request path without the query string."""
    low = path.lower()
    if {".", ".."} & set(path.split("/")) or "%2e" in low or "%2f" in low or "%5c" in low or "\\" in path:
        return Decision(False, "dot segments and encoded separators are not allowed")
    for r_role, r_method, rx, tag in _RULES:
        if r_role == role and r_method == method and rx.match(path):
            return Decision(True, "allowed", tag)
    return Decision(False, f"{method} {path} is not an allowed read call")
