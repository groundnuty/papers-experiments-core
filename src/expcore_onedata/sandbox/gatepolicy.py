# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/gatepolicy.py, blob 79565813abc302773b3f2664848139108cc72333
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, day0/REPORT.md:345-358 (the listing's default and largest page, measured)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/evidence/docs_research.md:47-54,155-176 (the server's and client's code)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/evidence/changes.jsonl (the file ids of a real federation)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/gatepolicy.py:14,51,74-75 (a merged listing as a mode)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/evidence/docs_research.md:24-47 (the listing's paging options)
#   changes: see the history of this file from its import commit
"""Request policy of the sandbox gate: which Onedata REST calls pass (O05, O16), and how a listing is paged (O21).

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
body that is not one JSON object, which the gate could not check. A body nested deeper than `MAX_BODY_DEPTH` is refused
the same way before it is parsed, found by a linear scan, so no body can make the parser exhaust the stack and reset
the connection as an error of the gate's. A listing without a limit passes unchanged.

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

**Two listing modes (O21).** A REST data path is `paged` (the default) or `complete`, as `[sandbox.data_paths]` says
for the level (`settings.py`), and the gate is told which in its configuration. `paged` is everything above: a listing
is the server's page, passed on as it came. `complete` answers every listing with all of the directory's entries, in
the server's order, as one page (`isLast` true, `nextPageToken` null): the gate follows the pages upstream itself, so a
client that reads one page (the official one's `ls` does) sees the whole directory. The earlier study's gate had the
same mode (the first `merged:` line); this one bounds it and checks every page:

  * the gate drives the paging: it asks for pages of `MAX_LIST_LIMIT` and drops the agent's `limit` and
    `tuneForLargeContinuousListing` (`DROPPED_OPTIONS`; the second cannot go with the token the gate sends from the second
    page on). A listing that names where to start, `token`, `index` or `offset` (`START_OPTIONS`), could not be the
    directory's complete listing, so it is refused with a 400 the agent reads, as any policy refusal is. The options are
    the ones the API's own description of the call lists (the second `merged:` line above);
  * it continues while a page says `isLast` false and gives a token, whatever the page's length: the API says a page can
    be short and still not the last, and that `isLast` is the only sign of the end. A page that is not JSON with each
    key once, not an object with a list of children, an `isLast` that is not a boolean, `isLast` false without a
    token, a token the listing gave before (it would never end) or an entry it gave before (the directory changed) is
    the platform's malformed answer: a 502 of origin `upstream`, never a part of the listing taken for the whole
    (`ListingWalk`);
  * the merged answer, with every continuation token the listing gave, is at most `COMPLETE_LISTING_BYTES` (64 MiB),
    and the listing at most `COMPLETE_LISTING_PAGES` (10,000) pages; past either, the listing cannot be served whole
    and the gate refuses it for its own integrity (`listing_too_large`, so the run is set aside), never passing a part
    of it on. Of the tokens and the entries only a 16-byte digest each is kept to find a repeat. 64 MiB is about 220,000
    entries of 300 bytes, the size of an entry with a file id of 180 characters and the attributes the official client
    asks for; the gate's memory for it is worked out in `gate.py` (`BUFFER_SLOTS`).

**A known limit.** Onedata gives a listing no snapshot, so a complete listing is complete only for a directory that
does not change while the gate reads it, as a study's data should not. An entry added meanwhile shifts the ones after
it, one of them then comes twice, and the listing is refused; an entry removed meanwhile can shift another out of the
pages unseen, and the answer then lacks it with nothing to show. An entry that carries neither a file id nor a name
(a listing that asks for neither attribute) cannot be told from another, so no repeat of it is found.

The earlier study's gate had a whole-file mode too, a download stored before it is released; it is not carried over.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from urllib.parse import parse_qs, parse_qsl, urlencode

ONEZONE, ONEPROVIDER = "onezone", "oneprovider"
PAGED, COMPLETE = "paged", "complete"  # the listing modes of a REST data path (O21)
LISTINGS = (PAGED, COMPLETE)

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
MAX_BODY_DEPTH = 32  # the deepest a listing's JSON body may nest; the official client's nests two levels
_STRUCTURE = re.compile(
    r'["\\\[\]{}]'
)  # the characters that open or close a string, an array or an object, and escapes
START_OPTIONS = ("token", "index", "offset")  # where a listing starts: refused in a complete listing
DROPPED_OPTIONS = ("limit", "tuneForLargeContinuousListing")  # the gate's to set when it pages a complete listing
COMPLETE_LISTING_BYTES = 64 * 1024 * 1024  # the largest merged answer of a complete listing
COMPLETE_LISTING_PAGES = 10_000  # the most pages a complete listing follows
START_REFUSED = (
    "this data path answers every listing with all of the directory's entries, from its first: a listing may not name "
    "token, index or offset"
)


@dataclasses.dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    tag: str = ""
    status: int = 403  # HTTP status of a refusal
    merge_pages: bool = False  # a complete listing: the gate follows the pages upstream and answers with one (O21)


def decide(
    role: str, method: str, path: str, query: str = "", body: bytes | None = None, *, listing: str = PAGED
) -> Decision:
    """`path` is the request path without the query string; `query` is the query string, and `body` the request's body,
    which a listing's options are read from as well. `listing` is the data path's listing mode; one this module does
    not know raises, since the gate refuses to start with it."""
    if listing not in LISTINGS:
        raise ValueError("the listing mode is not one the gate knows")
    low = path.lower()
    if {".", ".."} & set(path.split("/")) or "%2e" in low or "%2f" in low or "%5c" in low or "\\" in path:
        return Decision(False, "dot segments and encoded separators are not allowed")
    for r_role, r_method, rx, tag in _RULES:
        if r_role == role and r_method == method and rx.match(path):
            merge = tag == "children" and listing == COMPLETE
            refused = _listing_refusal(query, body, complete=merge) if tag == "children" else None
            if refused is not None:
                return Decision(False, refused, tag, status=400)
            return Decision(True, "listing merged into one page" if merge else "allowed", tag, merge_pages=merge)
    return Decision(False, "not an allowed read call")  # the line has the method and the path, as the agent sent them


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object's members, refusing a key given twice: which of the two a server reads is not known."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is given twice")
    return dict(pairs)


def _nested_deeper(text: str, bound: int) -> bool:
    """Whether a JSON text nests arrays and objects deeper than `bound`, found in one linear pass that looks at no
    character twice and keeps no stack, so no body can make it recurse; a bracket inside a string is text, not nesting.
    It reads the decoded text, so a body in UTF-16 or UTF-32 is scanned as the parser reads it."""
    depth, in_string, skip = 0, False, -1
    for found in _STRUCTURE.finditer(text):
        at = found.start()
        if at == skip:  # the character after a backslash in a string is escaped, whatever it is
            continue
        char = text[at]
        if in_string:
            if char == "\\":
                skip = at + 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > bound:
                return True
        elif char in "]}":
            depth -= 1
    return False


BODY_REFUSED = "a listing's body must be one JSON object with each key once"


def _body_options(body: bytes | None) -> dict | str | None:
    """A listing's JSON body as its options, None for no body, or why it is refused: it is decoded as the parser decodes
    bytes (UTF-8, -16 or -32, found from the first bytes), its nesting is bounded before the parser runs (which
    recurses once per level), and it must be one JSON object with each key once."""
    if not body:
        return None
    try:
        text = body.decode(json.detect_encoding(body), "surrogatepass")
    except (UnicodeDecodeError, LookupError):
        return BODY_REFUSED
    if _nested_deeper(text, MAX_BODY_DEPTH):
        return f"a listing's body may nest at most {MAX_BODY_DEPTH} levels of arrays and objects"
    try:
        options = json.loads(text, object_pairs_hook=_unique_keys)
    except Exception:  # any failure of the parser, RecursionError included, is the agent's malformed request
        options = None
    return options if isinstance(options, dict) else BODY_REFUSED


def _listing_refusal(query: str, body: bytes | None, *, complete: bool = False) -> str | None:
    """Why a listing is refused, or None when it may pass.

    Paged: for the size of the page it asks for. Every limit is read, from the query (each time the parameter is given)
    and from a JSON body, since which one the server reads is not known; a listing with none passes, and the server
    answers its default page. Complete: for naming where it starts (`START_OPTIONS`, in either place); its limit is
    the gate's, and is not read."""
    options = _body_options(body)
    if isinstance(options, str):
        return options
    given = parse_qs(query, keep_blank_values=True)
    if complete:
        named = set(given) | set(options or {})
        return START_REFUSED if named & set(START_OPTIONS) else None
    limits: list[object] = list(given.get("limit", []))
    if options and "limit" in options:
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


def complete_listing_request(query: str, body: bytes | None) -> tuple[list[tuple[str, str]], bytes | None]:
    """The query options and the body every page of a complete listing is asked for with: the agent's, without the
    options the gate sets (`DROPPED_OPTIONS`), in either place. A body that names none of them goes as it came. Call it
    only on a listing `decide` allowed, whose body is then one JSON object or empty."""
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in DROPPED_OPTIONS]
    options = _body_options(body)
    if isinstance(options, dict) and set(options) & set(DROPPED_OPTIONS):
        body = json.dumps({k: v for k, v in options.items() if k not in DROPPED_OPTIONS}).encode()
    return pairs, body


def page_target(path: str, pairs: list[tuple[str, str]], token: str | None) -> str:
    """The target of one page of a complete listing: the agent's options, the gate's page size and, from the second page
    on, the token the page before gave."""
    paging = [("limit", str(MAX_LIST_LIMIT))] + ([("token", token)] if token is not None else [])
    return f"{path}?{urlencode(pairs + paging)}"


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON value")


def _digest(kind: str, value: object) -> bytes:
    """A fixed-size digest of a token or an entry's identity: what a listing keeps of each, whatever its length."""
    return hashlib.blake2b(f"{kind}\0{value}".encode("utf-8", "surrogatepass"), digest_size=16).digest()


def _identity(entry: object) -> tuple[str, object] | None:
    """What tells an entry of a directory from the others: its file id, else its name (one per directory); None for an
    entry that carries neither, which nothing then tells from another."""
    if isinstance(entry, dict):
        for key in ("fileId", "name"):
            if isinstance(entry.get(key), str):
                return key, entry[key]
    return None


class ListingWalk:
    """What a complete listing has read so far, page by page (O21): a fixed-size digest of every token it gave and of
    every entry's identity, and the bytes of its tokens, which count towards `COMPLETE_LISTING_BYTES` with its entries.

    Onedata gives a listing no snapshot, so a directory another client changes while it is read can be listed wrong: an
    entry added shifts the ones after it into the next page, which then shows one of them twice, and that is refused
    here; an entry removed shifts one out of the pages unseen, and nothing shows it. A complete listing is complete only
    for a directory that does not change while it is read, as a study's data should not."""

    def __init__(self) -> None:
        self._tokens: set[bytes] = set()
        self._entries: set[bytes] = set()
        self.token_bytes = 0  # the bytes of the tokens the listing gave, held while it is read

    def page(self, data: bytes) -> tuple[list, str | None]:
        """One page as (its entries, the token of the next page, None after the last). ValueError for a page the
        listing cannot go on from: not JSON with each key once (a value JSON does not have, NaN or Infinity, included),
        not an object with a list of children, an `isLast` that is not a boolean, `isLast` false without a token, a
        token given before (the listing would never end), or an entry given before (the directory changed while it was
        read). A short page with `isLast` false is not the end."""
        try:
            page = json.loads(data, parse_constant=_no_constant, object_pairs_hook=_unique_keys)
        except ValueError:
            raise ValueError("a listing page is not JSON with each key once") from None
        if not isinstance(page, dict) or not isinstance(page.get("children"), list):
            raise ValueError("a listing page is not an object with a list of children")
        last, token = page.get("isLast"), page.get("nextPageToken")
        if last is not True and last is not False:
            raise ValueError("a listing page's isLast is not a boolean")
        if last is False and (not isinstance(token, str) or not token):
            raise ValueError("a listing page that is not the last gives no token for the next")
        for entry in page["children"]:
            identity = _identity(entry)
            if identity is None:
                continue
            digest = _digest(*identity)
            if digest in self._entries:
                raise ValueError("a listing page gave an entry the listing gave before: the directory changed")
            self._entries.add(digest)
        if last is True:
            return page["children"], None
        digest = _digest("token", token)
        if digest in self._tokens:
            raise ValueError("a listing page gave a token the listing gave before: it would never end")
        self._tokens.add(digest)
        self.token_bytes += len(token.encode("utf-8", "surrogatepass"))
        return page["children"], token
