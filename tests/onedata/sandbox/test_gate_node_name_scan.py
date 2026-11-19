"""O73: the gate counts, in memory and by channel, the node names and addresses in the answers it relays, and records
the count, never a body, a matched name or its context.

The gate runs in-process: a TLS listener on a loopback port, and a real TLS upstream whose certificate holds the served
name alone. The known node names are the gate's `scan_names`: the real side of the address map, which reaches the gate
as its own configuration and nothing a cell can read. Names are invented: `.test` for the layout's, `example.org` for
a node's, the documentation range for addresses.
"""

import http.client
import json
import re
import socket
import ssl
import threading
import time

import pytest
import rest_answers
from tls_peer import TlsPeer

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.hostops import DockerHost

SERVED = "depot.layout.test"  # the declared name: what the agent sees
NODE, SHORT, ADDRESS, V6 = "cn-r3c07-n12.cluster.example.org", "cn-r3c07-n12", "198.51.100.21", "2001:db8::2"
NAMES = [NODE, SHORT, ADDRESS, V6]
ATTRS = "/api/v3/oneprovider/data/f1"  # a file's attributes: an answer that is no content, buffered and checked
CONTENT = "/api/v3/oneprovider/data/f1/content"  # a file's content: the study's data, streamed


def _log_lines(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _requests(log, count) -> list[dict]:
    """The log's request lines, once `count` of them are there (the gate writes a line after it answers)."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        found = [r for r in _log_lines(log) if "method" in r]
        if len(found) >= count:
            return found
        time.sleep(0.05)
    raise AssertionError(f"the gate logged fewer than {count} requests")


@pytest.fixture
def world(tmp_path):
    """A gate serving SERVED and dialling a real TLS upstream by its mapped address, `scan_names` as the test gives."""
    started = []

    def make(upstream, *, scan=NAMES, **host):
        gate_tls = tmp_path / "gate-tls"
        DockerHost().make_ca(gate_tls, [SERVED])
        log = tmp_path / "requests.jsonl"
        log.touch()
        gate.CFG.clear()
        gate._CTX.clear()
        gate.CFG.update(
            run_key="run-test-1",
            token="tok-gate-0123456789",
            log=str(log),
            tls_dir=str(gate_tls),
            verify_ssl=True,
            upstream_ca_file=str(upstream.ca),
            hosts={
                SERVED: {
                    "role": "oneprovider",
                    "addr": SERVED,
                    "port": upstream.port,
                    "tls": True,
                    "dial": "127.0.0.1",
                    **host,
                }
            },
            listen_host="127.0.0.1",
            listen_port=0,
            **({"scan_names": scan} if scan is not None else {}),
        )
        httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
        httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd, gate_tls, log

    yield make
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()
    gate.CFG.clear()
    gate._CTX.clear()


def _get(httpd, tls, path=ATTRS) -> tuple[int, bytes]:
    context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", httpd.server_port), timeout=20)
    conn = http.client.HTTPConnection(SERVED, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=SERVED)
    conn.request("GET", path)
    answer = conn.getresponse()
    status, body = answer.status, answer.read()
    conn.close()
    return status, body


def _hits(log) -> int:
    return sum(r.get("node_name_hits", 0) for r in _log_lines(log))


def test_o73_an_answer_with_a_node_name_in_two_places_counts_two(tmp_path, world):
    """O73: a node's FQDN in `ctx.serviceDomain` and in a URL inside a description is two hits on the request's line."""
    body = json.dumps(
        {
            "error": {
                "id": "errorUnexpectedEndpointResponse",
                "details": {"ctx": {"serviceDomain": NODE}},
                "description": f"The peer redirected to https://{NODE}/api/v3/x.",
            }
        }
    )
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.answer = (503, body.encode())
        status, relayed = _get(httpd, tls)
        assert status == 503
        assert relayed == body.encode()  # the scan changes nothing the agent reads
        (line,) = _requests(log, 1)
        assert line["node_name_hits"] == 2


def test_o73_an_answer_with_only_declared_names_counts_nothing(tmp_path, world):
    """O73: the same answers registered under the layout's names hold no node's name, address or port: no hit, and no
    field on the line, in every channel of the REST answers."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        for channel, (status, body) in rest_answers.answers(SERVED, SERVED).items():
            upstream.answer = (status, body.encode())
            assert _get(httpd, tls)[0] == status, channel
        lines = _requests(log, len(rest_answers.CHANNELS))
        assert _hits(log) == 0
        assert not [line for line in lines if "node_name_hits" in line]


def test_o73_a_node_name_split_across_the_chunks_of_an_answer_still_counts(tmp_path, world):
    """O73: the gate buffers a non-content answer whole before it scans it, so a name that the upstream sent half in one
    chunk and half in the next is one hit."""
    whole = f'{{"error": {{"details": {{"providerDomain": "{NODE}"}}}}}}'.encode()
    cut = whole.index(b"example") + 3
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.chunks = [whole[:cut], whole[cut:]]
        status, relayed = _get(httpd, tls)
        assert (status, relayed) == (200, whole)
        (line,) = _requests(log, 1)
        assert line["node_name_hits"] == 1


def test_o73_the_log_holds_a_count_and_never_a_name_a_body_or_a_context(tmp_path, world):
    """O73: a request's line holds an integer; the log as a whole holds no node name, address or port, no piece of the
    answer's body and no text around a match."""
    body = json.dumps({"error": {"description": f"unreachable: {NODE}:8443 and [{V6}]:8443 (try {ADDRESS})"}})
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.answer = (503, body.encode())
        _get(httpd, tls)
        (line,) = _requests(log, 1)
    assert line["node_name_hits"] == 3
    text = log.read_text(encoding="utf-8").lower()
    for word in (*NAMES, "unreachable", "description"):
        assert word.lower() not in text, word
    for token in (
        "try",
        "8443",
    ):  # as whole tokens: the digits inside a timestamp or a duration, or "try" in "retry", are not the body's
        assert re.search(rf"(?<![0-9a-z]){token}(?![0-9a-z])", text) is None, token


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        (f"at {NODE}", 1),
        (f"at {NODE.upper()}:8443", 1),
        (f"at {SHORT}.", 1),
        (f"{SHORT} and {NODE}", 2),
        (f"[{V6}]:8443 and {V6}", 2),
        (f"{ADDRESS}:8443", 1),
        (f"{ADDRESS}2 and 1{ADDRESS} and x{SHORT} and {SHORT}-x", 0),
        (f"{SHORT}.other.example", 0),
        (f"{SERVED} and zone.layout.test", 0),
        ("", 0),
        # the same IPv6 address in the spellings an answer may print it in (the map holds 2001:db8::2)
        ("at 2001:0db8:0:0:0:0:0:2", 1),
        ("at 2001:DB8::2 and [2001:db8:0::2]:8443", 2),
        ("peer:2001:db8::2 unreachable", 1),
        ("2001:db8::2.", 1),
        ("2001:db8::2: refused", 1),
        ("2001:db8::2:1 and 2001:db8::20 and 12:30:45 and ::1", 0),
        # an IPv4 node address as an IPv4-mapped IPv6 address, dotted (counted once) or in hex
        (f"::ffff:{ADDRESS}", 1),
        ("::ffff:c633:6415", 1),
        ("::ffff:c633:6416", 0),
        # the text a parser or a library prints with escapes, and prose that ends in hex digits before an address
        (f"Bad request syntax ('GET /\\t{NODE} extra HTTP/1.1')", 1),
        (f"a\\n{SHORT} and b\\r{ADDRESS}", 2),
        ("node:2001:0db8::2 and de:2001:db8::2", 2),
        (f"peer={V6}", 1),
    ],
)
def test_o73_the_scan_counts_whole_names_with_or_without_a_port_in_any_case(text, hits):
    """O73: a name, an address or an IPv6 address counts once, bare, with its port or in brackets, in any case; a name
    inside a longer one, or a declared name, does not."""
    gate.CFG.clear()
    gate.CFG["scan_names"] = NAMES
    try:
        assert gate._node_name_hits(text.encode()) == hits
    finally:
        gate.CFG.clear()


@pytest.mark.parametrize(
    ("body", "hits"),
    [
        (r'{"description": "failed:\ncn-r3c07-n12.cluster.example.org"}', 1),  # an escape that ends in a letter
        (r'{"description": "\u0063n-r3c07-n12.cluster.example.org and \u0031\u0039\u0038.51.100.21"}', 2),
        (r'{"cn-r3c07-n12.cluster.example.org": ["a\tcn-r3c07-n12", {"k": "198\u002e51\u002e100\u002e21"}]}', 3),
        (r'{"description": "cn-r3c07-n12.cluster.exam\u0070le.org\/x"}', 1),
        (r'{"description": "fine", "n": 198}', 0),
    ],
)
def test_o73_a_name_hidden_by_json_escapes_is_counted_as_the_client_reads_it(tmp_path, world, body, hits):
    """O73: a JSON answer is scanned as its client decodes it: a name that follows `\\n` or `\\t`, whose characters are
    written as `\\uXXXX`, or that is a key, would be missed by the raw text and is not."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.answer = (503, body.encode())
        assert _get(httpd, tls)[0] == 503
        (line,) = _requests(log, 1)
    assert line.get("node_name_hits", 0) == hits


def test_o73_a_body_that_is_not_json_is_scanned_as_it_is(tmp_path, world):
    """O73: an HTML error page, or a JSON that does not parse, is scanned as it stands."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        for text in (f"<html>bad gateway: {NODE}</html>", f'{{"broken": "{NODE}'):
            upstream.answer = (502, text.encode())
            assert _get(httpd, tls)[0] == 502
        lines = _requests(log, 2)
    assert [line["node_name_hits"] for line in lines] == [1, 1]


def test_o73_a_deeply_nested_body_is_scanned_without_failing_the_answer(tmp_path, world):
    """O73: JSON nested beyond what the decoder allows falls back to the raw text; the answer still reaches the
    client."""
    nested = "[" * 5000 + f'"{NODE}"' + "]" * 5000
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.answer = (503, nested.encode())
        status, relayed = _get(httpd, tls)
        (line,) = _requests(log, 1)
    assert (status, relayed) == (503, nested.encode())
    assert line.get("node_name_hits") == 1


def _raw_request(httpd, tls, data: bytes) -> bytes:
    """Bytes sent to the gate as they are, over its TLS, and what it answers (a request the parser refuses)."""
    context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", httpd.server_port), timeout=20)
    wrapped = context.wrap_socket(raw, server_hostname=SERVED)
    wrapped.sendall(data)
    answer = b""
    while chunk := wrapped.recv(65536):
        answer += chunk
    wrapped.close()
    return answer


def test_o73_the_error_pages_of_the_request_parser_are_scanned_and_the_log_never_holds_a_node_name(tmp_path, world):
    """O73: an error the parser answers before the gate's own code runs (a bad version) quotes what the client sent in
    its page: the page is scanned, and neither the line's `reason` nor its `request_line` keeps a known name, whoever
    wrote it."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        page = _raw_request(httpd, tls, f"GET / HTTP/{NODE}\r\n\r\n".encode())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not [r for r in _log_lines(log) if r.get("event") == "request_error"]:
            time.sleep(0.05)
        (line,) = [r for r in _log_lines(log) if r.get("event") == "request_error"]
    assert NODE.encode() in page  # the parser's page echoes it: this is the path the scan must see
    assert line["node_name_hits"] >= 1
    text = log.read_text(encoding="utf-8").lower()
    for word in NAMES:
        assert word.lower() not in text, word


def test_o73_the_fields_that_echo_the_clients_request_hold_no_known_name(tmp_path, world):
    """O73: a refused request whose path and query carry a known name is logged without it: `path`, `query`, `reason`
    and `request_line` are what the client wrote, and the log is published."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        status, _ = _get(httpd, tls, f"/api/v3/onezone/{NODE}?probe={ADDRESS}")
        assert status in (400, 403, 404)
        _requests(log, 1)
    text = log.read_text(encoding="utf-8").lower()
    for word in NAMES:
        assert word.lower() not in text, word
    assert "<node>" in text


@pytest.mark.parametrize(
    ("body", "hits"),
    [
        (rb'"\u0063n-r3c07-n12.cluster.example.org"', 1),  # a JSON string is a document too
        (rb' "\u0031\u0039\u0038.51.100.21" ', 1),
        (rb'"fine"', 0),
        (rb'[ "\u0063n-r3c07-n12" ]', 1),
    ],
)
def test_o73_a_json_string_is_decoded_like_an_object_or_an_array(body, hits):
    """O73: a body that is a single JSON string is read as its client decodes it."""
    gate.CFG.clear()
    gate.CFG["scan_names"] = NAMES
    try:
        assert gate._node_name_hits(body) == hits
    finally:
        gate.CFG.clear()


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        (f"::ffff:{ADDRESS}", 1),  # the dotted tail and the whole address are one occurrence
        (f"{ADDRESS} and ::ffff:{ADDRESS}", 2),
        ("::ffff:c633:6415", 1),
        (f"[::ffff:{ADDRESS}]:8443", 1),
    ],
)
def test_o73_an_address_and_its_ipv4_mapped_form_are_one_occurrence_each(text, hits):
    """O73: with both the address and its mapped IPv6 form among the names, one occurrence is one hit, however many of
    the names it matches."""
    gate.CFG.clear()
    gate.CFG["scan_names"] = [ADDRESS, f"::ffff:{ADDRESS}", V6]
    try:
        assert gate._node_name_hits(text.encode()) == hits
    finally:
        gate.CFG.clear()


def test_o73_the_redaction_covers_the_spellings_the_scan_finds():
    """O73: what the scan counts, the log's echoing fields do not keep: escapes before a name, an address after
    prose and an address in another spelling are replaced by the mark."""
    gate.CFG.clear()
    gate.CFG["scan_names"] = NAMES
    try:
        for text in (
            f"GET /\\t{NODE} x",
            "node:2001:0db8::2",
            f"GET /{NODE.upper()}:8443/{ADDRESS}",
            "[2001:DB8:0:0:0:0:0:2]:8443",
        ):
            redacted = gate._redact_node_names(text)
            assert gate.NODE_MARK in redacted
            assert gate._node_name_hits(redacted.encode()) == 0
        assert gate._redact_node_names("GET /nothing") == "GET /nothing"
    finally:
        gate.CFG.clear()


def test_o73_the_gates_own_answers_are_scanned_too(tmp_path, world):
    """O73: what the gate itself answers the agent (a refusal, a 502) passes the same scan as an upstream's answer: here
    the served name is the one `scan_names` holds, and the 502 of a certificate the name is not on quotes it."""
    with TlsPeer(tmp_path / "up", "other.layout.test") as upstream:
        httpd, tls, log = world(upstream, scan=[SERVED])
        status, body = _get(httpd, tls)
        assert status == 502
        (line,) = _requests(log, 1)
    assert line["node_name_hits"] >= 1
    assert SERVED.encode() in body


def test_o73_a_files_content_is_the_studys_data_and_is_not_scanned(tmp_path, world):
    """O73: the streamed content of a file is data, not an answer of the platform: a name in it is no leak and is not
    counted, and the count says `rest_answers` only."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.content_type = "application/octet-stream"
        upstream.answer = (200, f"a file that names {NODE} and {ADDRESS}".encode())
        status, body = _get(httpd, tls, CONTENT)
        assert status == 200
        assert NODE.encode() in body
        (line,) = _requests(log, 1)
    assert "node_name_hits" not in line


def test_o73_an_error_answered_at_a_content_route_is_an_answer_of_the_platform_and_is_scanned(tmp_path, world):
    """O73: the gate streams a content route's answer only when it is a file's bytes; the platform's JSON error at the
    same route is read whole and checked, and so is scanned."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream)
        upstream.answer = (404, json.dumps({"error": {"details": {"providerDomain": NODE}}}).encode())
        assert _get(httpd, tls, CONTENT)[0] == 404
        (line,) = _requests(log, 1)
    assert line["node_name_hits"] == 1


def test_o73_without_scan_names_nothing_is_scanned_or_recorded(tmp_path, world):
    """O73: a gate given no names (a Compose federation's: the list is empty there) scans nothing and adds no field."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        httpd, tls, log = world(upstream, scan=None)
        upstream.answer = (503, json.dumps({"error": {"description": NODE}}).encode())
        _get(httpd, tls)
        (line,) = _requests(log, 1)
    assert "node_name_hits" not in line
