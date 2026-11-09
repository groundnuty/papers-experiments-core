"""O05: the gate's response boundary covers every form of its token wherever a body is split, and keeps the length."""

import json
from urllib.parse import quote

import pytest

from expcore_onedata.sandbox import gateboundary

TOKENS = ["tok-plain-0123456789", 'tok/gate+"0123', "tok gate é/1"]  # invented: plain, and with escaped characters


def forms_made_here(token: str) -> list[bytes]:
    """Raw, percent-encoded and JSON-escaped, made independently of the module under test."""
    return [token.encode(), quote(token, safe="").encode(), json.dumps(token)[1:-1].encode()]


def through(scrubber: gateboundary.Scrubber, pieces: list[bytes]) -> bytes:
    return b"".join(scrubber.feed(piece) for piece in pieces) + scrubber.flush()


@pytest.mark.parametrize("token", TOKENS)
def test_o05_a_token_split_at_any_byte_between_two_reads_is_covered_at_its_own_length(token):
    """O05: each form of the token, cut between two reads at every position, never comes out whole, and the body that
    comes out is exactly as long as the one that went in."""
    forms = gateboundary.token_forms(token)
    for form in forms_made_here(token):
        body = b"head " + form + b" tail"
        for cut in range(len(body) + 1):
            out = through(gateboundary.Scrubber(forms), [body[:cut], body[cut:]])
            assert form not in out, (form, cut)
            assert len(out) == len(body)
            assert out.startswith(b"head ")
            assert out.endswith(b" tail")


def test_o05_a_body_of_tiny_reads_with_tokens_back_to_back_is_covered():
    """O05: one byte per read, two tokens touching and one at the very end: all covered, nothing else changed."""
    token = TOKENS[1]
    raw, percent = token.encode(), quote(token, safe="").encode()
    body = b"x" + raw * 2 + b"y" + percent
    out = through(gateboundary.Scrubber(gateboundary.token_forms(token)), [body[i : i + 1] for i in range(len(body))])
    assert raw not in out
    assert percent not in out
    assert len(out) == len(body)
    assert out[0:1] + out[1 + 2 * len(raw) : 2 + 2 * len(raw)] == b"xy"  # the bytes around the tokens are untouched


def test_o05_without_a_token_the_boundary_passes_everything_as_it_is():
    """O05: a gate with no token has nothing to cover."""
    scrubber = gateboundary.Scrubber(gateboundary.token_forms(None))
    assert through(scrubber, [b"abc", b"def"]) == b"abcdef"


def test_o05_credential_headers_and_headers_holding_the_token_are_withheld():
    """O05: X-Auth-Token, Authorization, Proxy-Authorization and Set-Cookie never pass; any other header passes unless
    it holds a form of the token."""
    token = TOKENS[1]
    forms = gateboundary.token_forms(token)
    for name in ("X-Auth-Token", "Authorization", "Proxy-Authorization", "Set-Cookie"):
        assert gateboundary.header_passes(name, "anything", forms) is False
    assert gateboundary.header_passes("Location", f"/next?t={quote(token, safe='')}", forms) is False
    assert gateboundary.header_passes("X-Echo", token, forms) is False
    assert gateboundary.header_passes("Content-Type", "application/json", forms) is True


@pytest.mark.parametrize(("status", "redirect"), [(300, True), (301, True), (302, True), (307, True), (304, False)])
def test_o05_every_3xx_but_not_modified_is_a_redirect_the_gate_refuses(status, redirect):
    """O05: a redirect is never passed on; 304 Not Modified has no body and no target, so it is none."""
    assert gateboundary.is_redirect(status) is redirect
    assert gateboundary.is_redirect(200) is False
