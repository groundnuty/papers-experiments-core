"""O05: the gate's response boundary finds its token in any form wherever a body is split, and lets none of it go."""

import json
from urllib.parse import quote

import pytest

from expcore_onedata.sandbox import gateboundary

TOKENS = ["tok-plain-0123456789", 'tok/gate+"0123', "tok gate é/1"]  # invented: plain, and with escaped characters


def forms_made_here(token: str) -> list[bytes]:
    """Raw, percent-encoded in both cases of hex, JSON-escaped (with an escaped solidus, and every character as
    \\uXXXX), made independently of the module under test."""
    percent = quote(token, safe="")
    every = "".join(f"\\u{ord(c):04x}" for c in token)
    return [
        token.encode(),
        percent.encode(),
        percent.lower().encode(),
        json.dumps(token)[1:-1].replace("/", "\\/").encode(),
        every.encode(),
    ]


def through(watch: gateboundary.BodyWatch, pieces: list[bytes]) -> tuple[bytes, bool]:
    """What the watch lets go of the pieces, and whether it stopped them."""
    sent = b""
    for piece in [*pieces, None]:
        out = watch.feed(piece) if piece is not None else watch.flush()
        if out is None:
            return sent, True
        sent += out
    return sent, False


@pytest.mark.parametrize("token", TOKENS)
def test_o05_a_token_in_any_form_split_at_any_byte_is_found_before_any_of_it_goes(token):
    """O05: each form of the token, cut between two reads at every position, stops the body, and what went before
    holds no byte of it."""
    for form in forms_made_here(token):
        body = b"head " + form + b" tail"
        for cut in range(len(body) + 1):
            sent, stopped = through(gateboundary.BodyWatch(token), [body[:cut], body[cut:]])
            assert stopped, (form, cut)
            assert len(sent) <= len(b"head "), (form, cut)


def test_o05_overlapping_tokens_stop_the_body_with_nothing_of_them_sent():
    """O05: token ababa, body abababa, one byte per read: stopped, and not one byte of it went."""
    sent, stopped = through(gateboundary.BodyWatch("ababa"), [bytes([b]) for b in b"abababa"])
    assert (sent, stopped) == (b"", True)


def test_o05_a_body_without_the_token_goes_whole_and_unchanged_whatever_its_escapes():
    """O05: the boundary never rewrites: a body with escapes and pieces of the token, but not the token, goes as it
    came, every byte, at its end."""
    token = TOKENS[1]
    body = b"%2F \\u0074ok/gate+ %zz \\" + token[:-1].encode() + b" \\uD83D\\uDE00 end"
    for size in (1, 3, 7, 64):
        sent, stopped = through(gateboundary.BodyWatch(token), [body[i : i + size] for i in range(0, len(body), size)])
        assert (sent, stopped) == (body, False)


def test_o05_without_a_token_the_boundary_passes_everything_as_it_is():
    """O05: a gate with no token has nothing to look for."""
    assert through(gateboundary.BodyWatch(None), [b"abc", b"def"]) == (b"abcdef", False)
    assert gateboundary.holds_token(b"anything", None) is False


def test_o05_a_header_holds_the_token_raw_or_decoded():
    """O05: the header check reads the decoded view: percent-escapes in either case of hex, and JSON escapes."""
    token = "tok/gate+0123"
    for text in (b"tok/gate+0123", b"tok%2Fgate%2b0123", b"tok\\/gate\\u002b0123", b"x=tok%2fgate\\u002B0123"):
        assert gateboundary.holds_token(b"X-Echo: " + text, token), text
    assert gateboundary.holds_token(b"X-Echo: tok%2Fgate%2b012", token) is False
    assert gateboundary.holds_token(b"X-Echo: TOK%2Fgate%2b0123", token) is False  # tokens are case-sensitive


def test_o05_the_held_back_bytes_fit_the_longest_form():
    """O05: six bytes a character (\\uXXXX), twelve for one outside the basic plane, three a byte percent-escaped."""
    assert gateboundary.held_back("ab") == 12
    assert gateboundary.held_back("\U0001f600") == 12
    assert gateboundary.held_back(None) == 0


@pytest.mark.parametrize(("status", "redirect"), [(300, True), (301, True), (302, True), (307, True), (304, False)])
def test_o05_every_3xx_but_not_modified_is_a_redirect_the_gate_refuses(status, redirect):
    """O05: a redirect is never passed on; 304 Not Modified has no body and no target, so it is none."""
    assert gateboundary.is_redirect(status) is redirect
    assert gateboundary.is_redirect(200) is False
