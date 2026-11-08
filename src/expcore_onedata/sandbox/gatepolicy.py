# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/gatepolicy.py, blob 79565813abc302773b3f2664848139108cc72333
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Request policy of the sandbox gate: which Onedata REST calls each option allows.

Pure functions, no I/O, so the policy is unit-testable. `gate.py` (which runs in
the gate container) applies it to every request the sandbox makes.

Only calls the official fsspec/REST client needs for listing, attribute lookup
and reading are allowed, and only as reads (plus the two POSTs that are lookups).
Everything else is refused: writes, metadata, share/download-URL endpoints,
other hosts. The option-specific rules are the manipulated variable:

  T, condition_a  content GET with or without Range is allowed. Turning T into
                    a download is not prevented here (heuristics), it is bounded
                    by the small disk quota and recorded by the instruments.
  condition_a     differs from T only in `merge_pages` on the children listing.
  D                 content GET is allowed only WITHOUT a Range header, so the
                    only way to read a file is to download it whole. The gate stores
                    the whole file before releasing a byte, so even a range-less
                    request that the client reads 1 KB of and closes costs the complete transfer.
  M, M+             no Onedata REST at all (the data comes from the mount).
"""
from __future__ import annotations

import dataclasses
import re

T, condition_c, D, M, condition_d = "T", "condition_a", "D", "M", "condition_b"
REST_OPTIONS = (T, condition_c, D)
ALL_OPTIONS = (T, condition_c, D, M, condition_d)

ONEZONE, ONEPROVIDER = "onezone", "oneprovider"
D_MESSAGE = ("In this sandbox, files cannot be read remotely in place; "
             "download whole files with fs.get(remote_path, local_path) first.")

_P = "/api/v3/"
_RULES = (
    # (role, method, regex, tag)
    (ONEZONE, "POST", re.compile(re.escape(_P) + r"onezone/tokens/infer_access_token_scope$"), "scope"),
    (ONEPROVIDER, "POST", re.compile(re.escape(_P) + r"oneprovider/lookup-file-id/[^/]+(/.*)?$"), "lookup"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+$"), "attrs"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+/children$"), "children"),
    (ONEPROVIDER, "GET", re.compile(re.escape(_P) + r"oneprovider/data/[^/]+/content$"), "content"),
)


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    tag: str = ""
    store_and_forward: bool = False  # D content: fetch the whole file upstream before releasing any byte
    merge_pages: bool = False  # follow pagination upstream, answer with one complete page
    status: int = 403          # HTTP status of a refusal


def decide(option: str, role: str, method: str, path: str, headers: dict[str, str]) -> Decision:
    """`path` is the request path without the query string; header names lower-case."""
    if option not in ALL_OPTIONS:
        return Decision(False, f"unknown option {option!r}")
    low = path.lower()
    if {".", ".."} & set(path.split("/")) or "%2e" in low or "%2f" in low or "%5c" in low or "\\" in path:
        return Decision(False, "dot segments and encoded separators are not allowed")
    if option not in REST_OPTIONS:
        return Decision(False, f"option {option} has no REST data path")
    for r_role, r_method, rx, tag in _RULES:
        if r_role == role and r_method == method and rx.match(path):
            break
    else:
        return Decision(False, f"{method} {path} is not an allowed read call")
    if tag == "content" and option == D and "range" in headers:
        # 400, not 403: the shipped client's 403 path raises an unrelated TypeError (it takes len() of the
        # empty request body while logging), which would hide the reason from the agent
        return Decision(False, D_MESSAGE, tag,
                        status=400)
    if tag == "children" and option == condition_c:
        return Decision(True, "listing merged into one page", tag, merge_pages=True)
    if tag == "content" and option == D:
        return Decision(True, "whole-file download, stored and forwarded", tag, store_and_forward=True)
    return Decision(True, "allowed", tag)
