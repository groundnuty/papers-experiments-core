# Provenance: new
"""The data gate's response boundary (O05): no upstream answer hands the gate's token to the agent.

The gate adds its token to every request it forwards, so an answer can carry it back: an error body that echoes the
request, a redirect's Location, a header echoed back. The boundary detects the token and refuses the answer; it never
rewrites one, since a partial rewrite can leave a piece behind. Every answer passes it before it reaches the agent:

  * a redirect (`is_redirect`: a 3xx, but for 304 Not Modified, which has no body and points nowhere) is never passed
    on and never followed: the gate answers 502 with `REDIRECT_DETAILS` (`REDIRECT_REFUSED`);
  * a body the boundary cannot read as it arrives (`readable_encoding`: a Content-Encoding other than identity, which
    the gate asks the upstream for, or a Transfer-Encoding other than chunked) is never passed on: 502 with
    `ENCODED_DETAILS`, before any byte of it;
  * a header whose name or value holds the token (`holds_token`) makes the answer a 502 with `CREDENTIAL_DETAILS`,
    before its body; a credential header without it (`CREDENTIAL_HEADERS`) is withheld and the answer passes;
  * an answer that is no file's content (`streams`: any status but 200 or 206, any endpoint but a file's content,
    any content type but `DATA_CONTENT_TYPE`) is read whole, up to `BUFFER_CAP` (4 MiB), and checked with the full
    detector before any byte of it goes: a hit is a 502 (`CREDENTIAL_IN_RESPONSE`), and an answer over the cap a 502
    (`OVERSIZED_RESPONSE`). The way a token comes back is the platform echoing a request in an error or in metadata,
    and those answers are small; nested encodings outgrow any tail a stream can hold back, a whole answer has none;
  * a file's content streams through `BodyWatch`, which holds back the bytes a hit can take (`held_back`); on a hit
    the gate stops forwarding and cuts the transfer, so the agent gets no byte of it, and the line says why
    (`CREDENTIAL_IN_RESPONSE`). That is the second layer, and it suffices: the token is minted for the run after the
    study's data was provisioned, and the agent's data path is read-only, so a file's content cannot hold it; the
    watch is defence in depth, with the bound `held_back` states.

That a file's content comes as a 200 or 206 with `DATA_CONTENT_TYPE` is an assumption no source confirms: only the
earlier study's mock of a provider answers so. It is listed in `expcore_onedata.client.UNVERIFIED_ENDPOINTS` and pinned
by a live contract test (tests/live/test_contract.py). Should a deployment answer with another type, nothing leaks and
nothing is mis-scored: every read of a file is then buffered, one up to 4 MiB is checked whole and passes, and one over
4 MiB is refused loudly (`OVERSIZED_RESPONSE`), so its run ends as `data_gate_integrity` and is set aside.

Detection does not depend on knowing the encoding. The bytes are decoded again and again, until nothing changes or
`ROUNDS` rounds have run (`layers`): percent-escapes in either case of hex with `+` as a space, JSON escapes with a
surrogate pair combined into its character (a lone surrogate is kept as it is written, never paired with a
neighbour), and HTML numeric entities. The token and every layer, the raw bytes included, are projected to their ASCII
letters and digits, case kept, and a hit is any `RUN` consecutive characters of the token's projection (the whole of
it when it is shorter) in any layer's projection. So a nested or unforeseen escape, separators the token never had,
and a part of the token on its own are all found. The cost is the rare false hit, a cut transfer that ends the run as
infrastructure (`data_gate_integrity`), never a leak. A token with no ASCII letter or digit is looked for whole, in
every layer.

Pure functions and two small classes over bytes, with the standard library only: `gate.py` loads this file by its
path, as it does `gatepolicy.py`, since it runs alone in its container.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

CREDENTIAL_HEADERS = frozenset({"x-auth-token", "authorization", "proxy-authorization", "set-cookie", "set-cookie2"})
REDIRECT_DETAILS = "the upstream answered with a redirect, which the gate does not pass on or follow"
ENCODED_DETAILS = "the upstream answered with an encoded body, which the gate cannot look into and does not pass on"
CREDENTIAL_DETAILS = "the upstream's answer held the gate's credential, so the gate does not pass it on"
CREDENTIAL_IN_HEADER = "credential_in_header"
CREDENTIAL_IN_RESPONSE = "credential_in_response"
ENCODED_RESPONSE = "encoded_response"
REDIRECT_REFUSED = "redirect_refused"
OVERSIZED_RESPONSE = "oversized_response"
OVERSIZED_DETAILS = "the upstream's answer is larger than the gate checks whole, so the gate does not pass it on"
BUFFER_CAP = 4 * 1024 * 1024  # the most of an answer that is no file's content the gate buffers and checks whole
DATA_CONTENT_TYPE = "application/octet-stream"  # what the content endpoint answers a file's content with

RUN = 32  # characters of the token's projection a hit needs; its whole projection when that is shorter
ROUNDS = 4  # decoding rounds at most
_BLOCK = RUN // 2  # every RUN-long run of the projection holds one whole block of this length at a multiple of it
_ALNUM = frozenset(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_NOT_ALNUM = bytes(b for b in range(256) if b not in _ALNUM)
_ESCAPE = re.compile(
    rb"%[0-9A-Fa-f]{2}"
    rb"|\+"
    rb"|\\u[dD][89abAB][0-9A-Fa-f]{2}\\u[dD][c-fC-F][0-9A-Fa-f]{2}"  # a surrogate pair, high then low
    rb"|\\u[0-9A-Fa-f]{4}"
    rb"|\\[\"\\/bfnrt]"
    rb"|&#[xX][0-9A-Fa-f]{1,6};?"
    rb"|&#[0-9]{1,7};?"
)
_LONGEST_ESCAPE = 12  # the surrogate pair
_JSON_SIMPLE = {b'"': b'"', b"\\": b"\\", b"/": b"/", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t"}


def _character(code: int, written: bytes) -> bytes:
    """A code point as UTF-8; one that is no character (a lone surrogate, out of range, NUL) stays as written."""
    if code == 0 or code > 0x10FFFF or 0xD800 <= code < 0xE000:
        return written
    return chr(code).encode()


def _decoded(found: re.Match[bytes]) -> bytes:
    escape = found.group(0)
    if escape == b"+":
        return b" "
    if escape[:1] == b"%":
        return bytes([int(escape[1:3], 16)])
    if escape[:2] == b"&#":
        digits = escape[2:].rstrip(b";")
        return _character(int(digits[1:], 16) if digits[:1] in b"xX" else int(digits), escape)
    if escape[1:2] != b"u":
        return _JSON_SIMPLE[escape[1:2]]
    if len(escape) == 12:  # the pattern takes only a high surrogate followed by a low one
        high, low = int(escape[2:6], 16), int(escape[8:12], 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)).encode()
    return _character(int(escape[2:6], 16), escape)


def each_layer(data: bytes) -> Iterator[bytes]:
    """`data` and each round of its decoding, until a round changes nothing or `ROUNDS` have run, one at a time: a
    check holds no more than two layers at once."""
    layer = data
    yield layer
    for _ in range(ROUNDS):
        decoded = _ESCAPE.sub(_decoded, layer)
        if decoded == layer:
            return
        layer = decoded
        yield layer


def layers(data: bytes) -> list[bytes]:
    """`data` and each round of its decoding, all of them (`each_layer`)."""
    return list(each_layer(data))


def _maximal(character: str) -> int:
    """The most bytes one character of the token takes in one round of any encoding the layers undo."""
    units = 2 if ord(character) > 0xFFFF else 1
    return max(6 * units, 3 * len(character.encode()), len(f"&#{ord(character)};"))


def held_back(token: str | None) -> int:
    """The bytes the body watch holds back: twice what the token's widest hit takes in its longest single encoding.

    A hit is `RUN` letters and digits of the token (all of them when it has fewer), with the token's own characters
    between them, at most six bytes each as `\\uXXXX` (twelve for a pair): 6 x 32 = 192 bytes for a token of letters and
    digits. Twice that, 384, also holds a hit one layer deeper in most escapes, or with a few separators the token never
    had; a hit spread wider than that over the raw bytes is found only while it lies within one read and its held
    bytes. 6 x 32 x 2 is the bound the review called safe, and this is it for the usual token."""
    if not token:
        return 0
    letters = [i for i, c in enumerate(token) if c.isascii() and c.isalnum()]
    width = min(RUN, len(letters))
    if not width:
        return 2 * sum(_maximal(c) for c in token)
    spans = (token[letters[i] : letters[i + width - 1] + 1] for i in range(len(letters) - width + 1))
    return 2 * max(sum(_maximal(c) for c in span) for span in spans)


class Detector:
    """Whether bytes hold the token, by the projection of each of their layers (see the module's docstring)."""

    def __init__(self, token: str | None) -> None:
        self._raw = token.encode() if token else b""
        self._projection = self._raw.translate(None, _NOT_ALNUM)
        self._blocks = [
            (k, self._projection[k : k + _BLOCK]) for k in range(0, len(self._projection) - _BLOCK + 1, _BLOCK)
        ]

    def hit(self, data: bytes) -> bool:
        if not self._raw:
            return False
        for layer in each_layer(data):
            if not self._projection:
                if self._raw in layer:
                    return True
            elif self._run_in(layer.translate(None, _NOT_ALNUM)):
                return True
        return False

    def _run_in(self, projected: bytes) -> bool:
        token = self._projection
        if len(token) <= RUN:
            return token in projected
        for k, block in self._blocks:
            at = projected.find(block)
            while at != -1:
                left = 0
                while left < RUN and k > left and at > left and token[k - left - 1] == projected[at - left - 1]:
                    left += 1
                right, end = 0, k + _BLOCK
                while (
                    right < RUN
                    and end + right < len(token)
                    and at + _BLOCK + right < len(projected)
                    and token[end + right] == projected[at + _BLOCK + right]
                ):
                    right += 1
                if left + _BLOCK + right >= RUN:
                    return True
                at = projected.find(block, at + 1)
        return False


def holds_token(data: bytes, token: str | None) -> bool:
    """Whether `data` holds a run of the token, in any of its layers."""
    return Detector(token).hit(data)


def readable_encoding(content_encoding: str | None, transfer_encoding: str | None) -> bool:
    """Whether the boundary can read a body as it arrives: no Content-Encoding but identity (the gate asks for that),
    and no Transfer-Encoding but chunked, which http.client undoes. Anything else, gzip included, is refused."""
    content = (content_encoding or "identity").strip().lower()
    transfer = (transfer_encoding or "chunked").strip().lower()
    return content == "identity" and transfer == "chunked"


def streams(status: int, tag: str | None, content_type: str | None) -> bool:
    """Whether an answer is a file's content, the only answer the gate streams: a 200 or 206 from the content endpoint
    (gatepolicy's tag `content`) with the data content type. Every other answer is buffered whole, up to `BUFFER_CAP`,
    and checked before any byte of it goes."""
    media = (content_type or "").split(";")[0].strip().lower()
    return status in (200, 206) and tag == "content" and media == DATA_CONTENT_TYPE


def is_redirect(status: int) -> bool:
    """A 3xx the gate never passes on: every one but 304 Not Modified, which has no body and no target."""
    return 300 <= status < 400 and status != 304


class BodyWatch:
    """Watches a body read in pieces for the token: `feed` each piece, then `flush` at its end. Each returns the bytes
    that may go to the agent now, or None once a hit has appeared, after which nothing more may go.

    The held bytes are the last `held_back(token)` of what has arrived, moved back to the start of an escape they cut
    into, so a hit that starts before them ends inside what has arrived and is found before any byte of it is let
    through. Without a token every piece passes as it is."""

    def __init__(self, token: str | None) -> None:
        self._token, self._hold, self._tail = token, held_back(token), b""
        self._detector = Detector(token)

    def feed(self, piece: bytes) -> bytes | None:
        if not self._token:
            return piece
        data = self._tail + piece
        if self._detector.hit(data):
            return None
        cut = max(len(data) - self._hold, 0)
        for found in _ESCAPE.finditer(data, max(cut - _LONGEST_ESCAPE, 0)):
            if found.start() >= cut:
                break
            if found.end() > cut:  # never split an escape between what goes and what is held
                cut = found.start()
                break
        self._tail = data[cut:]
        return data[:cut]

    def flush(self) -> bytes | None:
        """What is still held, at the body's end, or None when it completes a hit."""
        tail, self._tail = self._tail, b""
        return None if self._detector.hit(tail) else tail
