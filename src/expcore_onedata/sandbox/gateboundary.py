# Provenance: new
"""The data gate's response boundary (O05): no upstream answer hands the gate's token to the agent.

The gate adds its token to every request it forwards, so an answer can carry it back: an error body that echoes the
request, a redirect's Location, a header echoed back. Every answer passes here before it reaches the agent:

  * a header that is a credential (`CREDENTIAL_HEADERS`), or whose name or value holds the token in any of its forms,
    is withheld (`header_passes`);
  * a redirect (`is_redirect`: a 3xx, but for 304 Not Modified, which has no body and points nowhere) is never passed
    on and never followed: the gate answers 502 with `REDIRECT_DETAILS`;
  * every form of the token in the body (`token_forms`: raw, percent-encoded, JSON-escaped) is covered by the marker
    as the body streams (`Scrubber`): each read's last len(longest form) - 1 bytes are held back and scanned with the
    next read, so a token split between two reads is still found. A cover is exactly as long as what it covers, so the
    body keeps its length: the Content-Length the upstream declared, and a Content-Range, stay true, and the framing of
    the response is untouched.

Pure functions and one small class over bytes, with the standard library only: `gate.py` loads this file by its path,
as it does `gatepolicy.py`, since it runs alone in its container.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

MARKER = b"[withheld-by-gate]"  # repeated, and cut, to the length of what it covers
CREDENTIAL_HEADERS = frozenset({"x-auth-token", "authorization", "proxy-authorization", "set-cookie", "set-cookie2"})
REDIRECT_DETAILS = "the upstream answered with a redirect, which the gate does not pass on or follow"
_HEX = re.compile(rb"%[0-9A-F]{2}")


@functools.cache
def token_forms(token: str | None) -> tuple[bytes, ...]:
    """Every form a body or a header can hold the token in, longest first: raw, percent-encoded (either case of hex,
    with `+` or `%20` for a space), JSON-escaped (with and without an escaped solidus, non-ASCII escaped or not).
    Empty when there is no token."""
    if not token:
        return ()
    escaped = json.dumps(token)[1:-1]
    forms = {
        token.encode(),
        quote(token, safe="").encode(),
        quote_plus(token, safe="").encode(),
        escaped.encode(),
        escaped.replace("/", "\\/").encode(),
        json.dumps(token, ensure_ascii=False)[1:-1].encode(),
    }
    forms |= {_HEX.sub(lambda m: m.group(0).lower(), f) for f in forms}
    return tuple(sorted(forms, key=lambda f: (-len(f), f)))


def cover(n: int) -> bytes:
    """The marker as long as `n` bytes."""
    return (MARKER * (n // len(MARKER) + 1))[:n]


def header_passes(name: str, value: str, forms: Iterable[bytes]) -> bool:
    """Whether an answer's header may reach the agent: not a credential, and holding no form of the token. http.client
    gives header text decoded as latin-1, so it is compared as those bytes."""
    if name.lower() in CREDENTIAL_HEADERS:
        return False
    line = f"{name}: {value}".encode("latin-1", "replace")
    return not any(form in line for form in forms)


def is_redirect(status: int) -> bool:
    """A 3xx the gate never passes on: every one but 304 Not Modified, which has no body and no target."""
    return 300 <= status < 400 and status != 304


class Scrubber:
    """Covers every form of the token in a body read in pieces (`feed` each piece, then `flush` at its end).

    A form that starts in one piece and ends in the next is found because each `feed` holds back the bytes that could
    begin one: up to len(longest form) - 1, never fewer. What `feed` returns is final; the held bytes come out with the
    next piece, or from `flush`. Without forms (no token) every piece passes as it is. The output is as long as the
    input, held bytes aside."""

    def __init__(self, forms: Iterable[bytes]) -> None:
        ordered = sorted({f for f in forms if f}, key=lambda f: (-len(f), f))  # longest first: it wins a tie
        self._pattern = re.compile(b"|".join(re.escape(f) for f in ordered)) if ordered else None
        self._keep = max((len(f) for f in ordered), default=1) - 1
        self._tail = b""

    def feed(self, piece: bytes) -> bytes:
        if self._pattern is None:
            return piece
        data = self._tail + piece
        # a form that starts before `safe` ends inside `data`, so it is found now; one that starts after may not be
        safe, pos, out = max(len(data) - self._keep, 0), 0, []
        for found in self._pattern.finditer(data):
            if found.start() >= safe:
                break
            out += [data[pos : found.start()], cover(found.end() - found.start())]
            pos = found.end()
            safe = max(safe, pos)
        out.append(data[pos:safe])
        self._tail = data[safe:]
        return b"".join(out)

    def flush(self) -> bytes:
        """The held bytes, covered: the body has ended."""
        tail, self._tail = self._tail, b""
        if self._pattern is None:
            return tail
        return self._pattern.sub(lambda found: cover(len(found.group(0))), tail)


def scrub(data: bytes, forms: Iterable[bytes]) -> bytes:
    """A whole text with every form of the token covered."""
    scrubber = Scrubber(forms)
    return scrubber.feed(data) + scrubber.flush()
