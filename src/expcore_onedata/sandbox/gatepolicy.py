# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/gatepolicy.py, blob 79565813abc302773b3f2664848139108cc72333
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, day0/REPORT.md:345-358 (the listing's default and largest page, measured)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/evidence/docs_research.md:47-54,155-176 (the server's and client's code)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/evidence/changes.jsonl (the file ids of a real federation)
#   changes: see the history of this file from its import commit
"""Request policy of the sandbox gate: which Onedata REST calls pass (O05, O16).

Pure functions, no I/O, so the policy is unit-testable. `gate.py` (which runs in the gate container, with the standard
library only) applies it to every request the sandbox makes.

Only calls the official REST client needs for listing, attribute lookup and reading are allowed, and only as reads (plus
the two POSTs that are lookups). Everything else is refused: writes, metadata, share and download-URL endpoints, other
hosts, dot segments and encoded separators. A content read with or without a Range header passes.

**A listing's page is bounded by the request (O05, O16).** Every answer that is no file's content is buffered whole by
the gate, up to `gateboundary.BUFFER_CAP` (4 MiB), and an answer over it is refused as the gate's own (a 502,
`oversized_response`, which sets the run aside). The non-content answers are:

  * `scope`: the scope of the gate's token, one object naming the token's spaces and their supporting providers, each by
    a name and an id: bounded by the size of the federation, a few kilobytes;
  * `lookup`: one file id (`{"fileId": ...}`), under a kilobyte;
  * `attrs`: the attributes of one file, one entry of the bound below, plus whatever attributes the request names;
  * `children`: a page of a directory's entries, and the only answer whose size the agent sets, by its `limit`.

So a listing whose explicit `limit` is above `MAX_LIST_LIMIT` is refused before anything is forwarded, with a 400 the
agent reads (a policy refusal, the agent's own outcome, never the gate's), in the query string or in a JSON body (the
server takes its options from either since 21.02.5), and so is a limit that is not a plain whole number or a listing
body that is not one JSON object, which the gate could not check. A listing without a limit passes unchanged.

`MAX_LIST_LIMIT` is 1000. It is the server's own default page: the earlier study read the server's code
(`DEFAULT_LIST_ENTRIES 1000`, `MAX_LIST_ENTRIES 10000`, the second `merged:` line) and measured a listing without a
limit on a real federation answering 1000 entries, and one of 10000 answering 10000 (the first). It is also what the
official client sends on every listing (`limit=1000` by default), so no ordinary listing is refused, and a lower
maximum would bound nothing: a listing without a limit gets the default page. The worst case of a page of 1000:

  * a name is at most 255 bytes, and JSON writes a byte as at most six (`\\u00XX` for a control character): 1530 bytes
    (`NAME_JSON_BYTES`);
  * a file id was 180 characters in each of the 17 the earlier study logged on a real federation (the third
    `merged:` line); 512 is allowed (`FILE_ID_BYTES`), the bound the platform client puts on a file id;
  * the rest of an entry with the attributes the official client asks for (type, size, mtime, posixPermissions), keys,
    quotes and separators included, is about 140 bytes; 256 are allowed (`ENTRY_REST_BYTES`);
  * the page's own keys, `isLast` and the `nextPageToken`: 4096 bytes (`PAGE_ENVELOPE_BYTES`).

1000 x (1530 + 512 + 256) + 4096 = 2,302,096 bytes, under the cap of 4,194,304 by 1,892,208: about 1.9 KB per entry
more for any further attribute a request names, a path among them. The fake platform's widest page at the maximum
(every attribute it has, a path included, and names of 255 control characters, the fake's file id of 511 characters)
is about 3.7 MB, and the gate serves it (`tests/onedata/sandbox/test_gate.py`). An answer that still outgrows the cap,
a listing that names attributes as large as a file's extended attributes, is refused loudly as before, never passed.

The earlier study's gate had two further modes (a whole-file download that is stored before it is released, and a
listing merged into one page). They were that study's manipulated variables and are not carried over: this gate is the
plain read-only path.
"""

from __future__ import annotations

import dataclasses
import json
import re
from urllib.parse import parse_qs

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

MAX_LIST_LIMIT = 1000  # the largest page a listing may ask for: the server's default page (see above)
NAME_JSON_BYTES = 255 * 6  # a name of 255 bytes, each written by JSON as at most six
FILE_ID_BYTES = 512  # a file id: 180 characters measured, 512 allowed
ENTRY_REST_BYTES = 256  # type, size, mtime, posixPermissions, the keys and the separators of one entry
PAGE_ENVELOPE_BYTES = 4096  # the page's own keys, isLast and the nextPageToken
_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]{1,18}")


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    tag: str = ""
    status: int = 403  # HTTP status of a refusal


def decide(role: str, method: str, path: str, query: str = "", body: bytes | None = None) -> Decision:
    """`path` is the request path without the query string; `query` is the query string, and `body` the request's body,
    which a listing's limit is read from as well."""
    low = path.lower()
    if {".", ".."} & set(path.split("/")) or "%2e" in low or "%2f" in low or "%5c" in low or "\\" in path:
        return Decision(False, "dot segments and encoded separators are not allowed")
    for r_role, r_method, rx, tag in _RULES:
        if r_role == role and r_method == method and rx.match(path):
            refused = _listing_refusal(query, body) if tag == "children" else None
            if refused is not None:
                return Decision(False, refused, tag, status=400)
            return Decision(True, "allowed", tag)
    return Decision(False, "not an allowed read call")  # the line has the method and the path, as the agent sent them


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object's members, refusing a key given twice: which of the two a server reads is not known."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is given twice")
    return dict(pairs)


def _listing_refusal(query: str, body: bytes | None) -> str | None:
    """Why a listing is refused for the size of the page it asks for, or None when it may pass.

    Every limit is read, from the query (each time the parameter is given) and from a JSON body, since which one the
    server reads is not known; a listing with none passes, and the server answers its default page."""
    limits: list[object] = list(parse_qs(query, keep_blank_values=True).get("limit", []))
    if body:
        try:
            options = json.loads(body, object_pairs_hook=_unique_keys)
        except ValueError:
            options = None
        if not isinstance(options, dict):
            return "a listing's body must be one JSON object with each key once"
        if "limit" in options:
            limits.append(options["limit"])
    for limit in limits:
        if isinstance(limit, str) and _WHOLE_NUMBER.fullmatch(limit):
            limit = int(limit)
        if isinstance(limit, bool) or not isinstance(limit, int):
            return f"a listing's limit must be a whole number of entries, at most {MAX_LIST_LIMIT}"
        if limit > MAX_LIST_LIMIT:
            return (
                f"a listing may ask for at most {MAX_LIST_LIMIT} entries; read a larger directory page by page, "
                "passing each answer's nextPageToken as the next request's token"
            )
    return None
