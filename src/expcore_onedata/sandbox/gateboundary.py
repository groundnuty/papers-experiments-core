# Provenance: new
"""The data gate's response boundary (O05): no upstream answer hands the gate's token to the agent.

The gate adds its token to every request it forwards, so an answer can carry it back: an error body that echoes the
request, a redirect's Location, a header echoed back. The boundary detects the token and refuses the answer; it never
rewrites one, since no list of the token's encodings is complete and a partial rewrite can leave a piece behind.
Every answer passes it before it reaches the agent:

  * a redirect (`is_redirect`: a 3xx, but for 304 Not Modified, which has no body and points nowhere) is never passed
    on and never followed: the gate answers 502 with `REDIRECT_DETAILS`;
  * a body the boundary cannot read as it arrives (`readable_encoding`: a Content-Encoding other than identity, which
    the gate asks the upstream for, or a Transfer-Encoding other than chunked) is never passed on: 502 with
    `ENCODED_DETAILS`, before any byte of it;
  * a header whose name or value holds the token (`holds_token`) makes the answer a 502 with `CREDENTIAL_DETAILS`,
    before its body; a credential header without it (`CREDENTIAL_HEADERS`) is withheld and the answer passes;
  * the body streams through `BodyWatch`, which holds back the bytes that could still begin the token in its longest
    form (`held_back`); when the token appears, in any form, the gate stops forwarding and cuts the transfer, so the
    agent gets no byte of it, and the line says why (`CREDENTIAL_IN_RESPONSE`).

`holds_token` looks at the bytes as they are and at their decoded view: percent-escapes in either case of hex, and
JSON escapes (`\\uXXXX`, a surrogate pair as its one character, `\\/`, `\\\\`, `\\"` and the others). The token is
matched by its exact characters, as Onedata's tokens are case-sensitive.

Pure functions and one small class over bytes, with the standard library only: `gate.py` loads this file by its path,
as it does `gatepolicy.py`, since it runs alone in its container.
"""

from __future__ import annotations

import re

CREDENTIAL_HEADERS = frozenset({"x-auth-token", "authorization", "proxy-authorization", "set-cookie", "set-cookie2"})
REDIRECT_DETAILS = "the upstream answered with a redirect, which the gate does not pass on or follow"
ENCODED_DETAILS = "the upstream answered with an encoded body, which the gate cannot look into and does not pass on"
CREDENTIAL_DETAILS = "the upstream's answer held the gate's credential, so the gate does not pass it on"
CREDENTIAL_IN_HEADER = "credential_in_header"
CREDENTIAL_IN_RESPONSE = "credential_in_response"
ENCODED_RESPONSE = "encoded_response"

_ESCAPE = re.compile(rb"%[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}(?:\\u[0-9A-Fa-f]{4})?|\\[\"\\/bfnrt]")
_JSON_SIMPLE = {b'"': b'"', b"\\": b"\\", b"/": b"/", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t"}


def _decoded(escape: bytes) -> bytes:
    """The bytes one escape stands for."""
    if escape[:1] == b"%":
        return bytes([int(escape[1:3], 16)])
    if escape[1:2] != b"u":
        return _JSON_SIMPLE[escape[1:2]]
    units = [int(escape[i : i + 4], 16) for i in range(2, len(escape), 6)]
    if len(units) == 2 and 0xD800 <= units[0] < 0xDC00 and 0xDC00 <= units[1] < 0xE000:
        return chr(0x10000 + ((units[0] - 0xD800) << 10) + (units[1] - 0xDC00)).encode()
    return b"".join(chr(u).encode("utf-8", "surrogatepass") for u in units)


def decoded_view(data: bytes) -> tuple[bytes, list[tuple[int, int]]]:
    """`data` with every escape decoded, parsed left to right, and the (start, end) of each escape in `data`."""
    parts, spans, pos = [], [], 0
    for found in _ESCAPE.finditer(data):
        parts += [data[pos : found.start()], _decoded(found.group(0))]
        spans.append(found.span())
        pos = found.end()
    parts.append(data[pos:])
    return b"".join(parts), spans


def holds_token(data: bytes, token: str | None) -> bool:
    """Whether `data` holds the token, as it is or in its decoded view."""
    if not token:
        return False
    needle = token.encode()
    return needle in data or needle in decoded_view(data)[0]


def held_back(token: str | None) -> int:
    """The bytes the longest form of the token can take: six for a character as `\\uXXXX`, twelve for one outside the
    basic plane (a surrogate pair), three for each byte of it percent-escaped, whichever is longest."""
    if not token:
        return 0
    return sum(max(12 if ord(c) > 0xFFFF else 6, 3 * len(c.encode())) for c in token)


def readable_encoding(content_encoding: str | None, transfer_encoding: str | None) -> bool:
    """Whether the boundary can read a body as it arrives: no Content-Encoding but identity (the gate asks for that),
    and no Transfer-Encoding but chunked, which http.client undoes. Anything else, gzip included, is refused."""
    content = (content_encoding or "identity").strip().lower()
    transfer = (transfer_encoding or "chunked").strip().lower()
    return content == "identity" and transfer == "chunked"


def is_redirect(status: int) -> bool:
    """A 3xx the gate never passes on: every one but 304 Not Modified, which has no body and no target."""
    return 300 <= status < 400 and status != 304


class BodyWatch:
    """Watches a body read in pieces for the token: `feed` each piece, then `flush` at its end. Each returns the bytes
    that may go to the agent now, or None once the token has appeared, after which nothing more may go.

    The held bytes are the last `held_back(token)` of what has arrived, moved back to the start of an escape they cut
    into, so every look at the decoded view starts where a parse of the whole body would: a token that starts before
    the held bytes ends inside what has arrived, and is found before any byte of it is let through. Without a token
    every piece passes as it is."""

    def __init__(self, token: str | None) -> None:
        self._token, self._hold, self._tail = token, held_back(token), b""

    def feed(self, piece: bytes) -> bytes | None:
        if not self._token:
            return piece
        data = self._tail + piece
        view, spans = decoded_view(data)
        needle = self._token.encode()
        if needle in data or needle in view:
            return None
        cut = max(len(data) - self._hold, 0)
        # never split an escape between what goes and what is held: the held bytes start where a parse would
        cut = next((start for start, end in spans if start < cut < end), cut)
        self._tail = data[cut:]
        return data[:cut]

    def flush(self) -> bytes | None:
        """What is still held, at the body's end, or None when it completes the token."""
        tail, self._tail = self._tail, b""
        return None if holds_token(tail, self._token) else tail
