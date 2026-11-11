"""O16, O05: the data gate makes no errors of its own, marks the transfers it cut, and passes only allowed reads.

The gate runs in-process here: a TLS listener on a loopback port, a plain HTTP upstream in a thread, and clients that
speak TLS to the gate with the run's test CA. Host names are invented (`.test`); nothing leaves the machine.
"""

import ast
import dataclasses
import gzip
import http.client
import json
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

ZONE, PROVIDER = "zone.test", "alpha.test"  # the zone and a provider of the test platform document: configured values
FILE = "/api/v3/oneprovider/data/f1/content"
BODY = bytes(range(256)) * 64  # 16 KiB
BIG = 12 * 1024 * 1024
RUN_HEADER_VALUE = "run-test-1"
GATE_TOKEN = "tok-gate-0123456789"  # an invented token value: the gate holds it, the client never does
SCOPE = "/api/v3/onezone/tokens/infer_access_token_scope"
SLASH_TOKEN = "tok/gate+0123"  # an invented token whose encoded forms differ from it
SPACE_TOKEN = "tok gate0123"  # an invented token with a space, which a form encodes as +
LONG_TOKEN = "tok" + "".join(f"{n:02d}q" for n in range(21)) + "z"  # invented: 67 letters and digits
SPLIT_AT = 4096  # the gate's read size in the tests that split a token between two reads of the upstream's body
OCTETS = "application/octet-stream"  # the content type of a file's content: the only answer the gate streams
CAP = 4 * 1024 * 1024  # the most the gate buffers of any other answer
HELD_BACK = 6  # bytes per character of the token the gate holds back: its longest form, \\uXXXX, is six


def across_reads(form: bytes) -> bytes:
    """A body with `form` across the boundary between the gate's first two reads, and filler round it."""
    return b"a" * (SPLIT_AT - len(form) // 2) + form + b"b" * 64


class Upstream(ThreadingHTTPServer):
    """A plain-HTTP stand-in for Onezone and a Oneprovider, recording what reached it."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.seen: list[tuple[str, str, dict]] = []
        self.bodies: list[bytes] = []
        self.mode = "ok"
        self.answer_headers: list[tuple[str, str]] = []  # mode "fixed": the answer's status, extra headers and body
        self.answer_body = b"{}"
        self.answer_status = 200
        self.release = threading.Event()  # mode "stall": the second half of each answer waits for it


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        self.server.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        answer = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def do_GET(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.server.mode == "stall":
            self._stall()
            return
        if self.server.mode in ("echo-headers", "redirect", "gzip", "fixed"):
            self._echo(self.headers.get("X-Auth-Token", ""))
            return
        if self.server.mode == "unavailable":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = BIG if self.server.mode == "big" else len(BODY)
        self.send_response(200)
        self.send_header("Content-Type", OCTETS)  # a file's content, as the content endpoint answers it
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            if self.server.mode == "big":
                for _ in range(size // 65536):
                    self.wfile.write(b"x" * 65536)
            else:
                self.wfile.write(BODY)
        except OSError:
            pass  # the gate went away mid-body

    def _stall(self) -> None:
        """A small answer that is no file content, its second half held back until the test releases it."""
        half = b'{"pad": "' + b"p" * 1000 + b'", '
        rest = b'"end": 1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(half) + len(rest)))
        self.end_headers()
        self.wfile.write(half)
        self.wfile.flush()
        self.server.release.wait(30)
        self.wfile.write(rest)

    def _echo(self, token: str) -> None:
        """A platform that hands back the token it was sent (in headers, in a redirect, compressed), or the answer the
        test set."""
        mode, status, headers = self.server.mode, 200, []
        body = b"{}"
        if mode == "echo-headers":
            headers = [
                ("X-Auth-Token", token),
                ("Authorization", f"Bearer {token}"),
                ("Set-Cookie", "session=invented"),
                ("X-Request-Echo", f"you sent {token}"),
                ("X-Plain", "kept"),
            ]
        elif mode == "redirect":
            status, headers = 302, [("Location", f"https://elsewhere.test/next?token={quote(token, safe='')}")]
            body = b'{"moved": "' + token.encode() + b'"}'
        elif mode == "gzip":  # whatever the gate asked for: a compressed body the gate cannot look into
            headers, body = [("Content-Encoding", "gzip")], gzip.compress(b'{"echo": "' + token.encode() + b'"}')
        else:  # "fixed": what the test set
            status, headers, body = self.server.answer_status, self.server.answer_headers, self.server.answer_body
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    DockerHost().make_ca(directory, [ZONE, PROVIDER])
    return directory


@pytest.fixture
def world(tls, tmp_path):
    upstream = Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "requests.jsonl"
    hosts = {
        ZONE: {"role": "onezone", "addr": "127.0.0.1", "port": upstream.server_port, "tls": False},
        PROVIDER: {"role": "oneprovider", "addr": "127.0.0.1", "port": upstream.server_port, "tls": False},
    }
    gate.CFG.clear()
    gate.CFG.update(
        run_key=RUN_HEADER_VALUE,
        token=GATE_TOKEN,
        log=str(log),
        tls_dir=str(tls),
        hosts=hosts,
        listen_host="127.0.0.1",
        listen_port=0,
    )
    log.touch()
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield type("World", (), {"gate_port": httpd.server_port, "upstream": upstream, "log": log, "tls": tls})
    httpd.shutdown()
    httpd.server_close()
    upstream.shutdown()
    upstream.server_close()
    gate.CFG.clear()


def connect(world, host=PROVIDER) -> http.client.HTTPConnection:
    """A keep-alive HTTP connection to the gate over TLS, with the server name and Host header of `host`."""
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", world.gate_port), timeout=20)
    conn = http.client.HTTPConnection(host, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=host)
    return conn


def records(world) -> list[dict]:
    return [json.loads(line) for line in world.log.read_text().splitlines() if line.strip()]


def wait_for(world, predicate, seconds=10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for record in records(world):
            if predicate(record):
                return record
        time.sleep(0.05)
    raise AssertionError(f"no log line matched in {seconds}s: {records(world)[-3:]}")


def test_o05_an_allowed_read_reaches_the_upstream_with_the_gates_run_attribution(world):
    """O05: a read passes; the run header is the gate's, and one the agent sent under that name is dropped."""
    conn = connect(world)
    conn.request("GET", FILE, headers={"X-Run-Key": "agent-chosen"})
    answer = conn.getresponse()
    assert answer.status == 200
    assert answer.read() == BODY
    method, path, headers = world.upstream.seen[0]
    assert (method, path) == ("GET", FILE)
    assert headers["X-Run-Key"] == RUN_HEADER_VALUE
    assert "authorization" not in {k.lower() for k in headers}  # the gate adds no credential and logs none
    conn.close()


def test_o05_the_gate_adds_its_token_and_drops_every_credential_the_agent_sent(world):
    """O05: the token is the gate's; the agent holds none, and whatever it sends as one never reaches the upstream."""
    conn = connect(world)
    sent = {"X-Auth-Token": "agent-made-up", "Authorization": "Bearer agent-made-up", "Cookie": "session=agent-made-up"}
    conn.request("GET", FILE, headers=sent)
    conn.getresponse().read()
    _, _, headers = world.upstream.seen[0]
    assert headers["X-Auth-Token"] == GATE_TOKEN
    assert {k.lower() for k in headers} & {"authorization", "cookie"} == set()
    assert "agent-made-up" not in json.dumps(headers)
    conn.close()
    assert GATE_TOKEN not in world.log.read_text()  # and the gate never logs it


def test_o05_a_token_scope_inference_carries_the_gates_token_in_its_body(world):
    """O05: the call names its token in the body, and the agent has none, so the gate puts its own there."""
    conn = connect(world, ZONE)
    for body in (b"{}", b'{"token": "agent-made-up", "other": 1}'):
        conn.request("POST", SCOPE, body=body, headers={"Content-Type": "application/json"})
        assert conn.getresponse().read() == b"{}"
    assert [json.loads(b).get("token") for b in world.upstream.bodies] == [GATE_TOKEN, GATE_TOKEN]
    assert json.loads(world.upstream.bodies[1])["other"] == 1
    conn.close()


def test_o05_a_token_scope_inference_that_is_not_a_json_object_is_refused_and_not_forwarded(world):
    """O05: the gate cannot put its token into a body that is not an object, so it does not forward the call."""
    conn = connect(world, ZONE)
    conn.request("POST", SCOPE, body=b"[1, 2]", headers={"Content-Type": "application/json"})
    answer = conn.getresponse()
    answer.read()
    assert answer.status == 400
    assert world.upstream.bodies == []
    conn.close()


def test_o05_the_gate_takes_its_token_from_the_file_its_configuration_names_and_never_from_its_environment(
    tmp_path, monkeypatch
):
    """O05: the gate reads its token from the file its configuration names (bound into its container read-only), so
    the token is never in docker's configuration of the container; a token in its environment is not used, and a
    missing or empty file refuses the start."""
    token_file = tmp_path / "gate-token"
    config = tmp_path / "gate.json"
    document = {"log": str(tmp_path / "r.jsonl"), "hosts": {}, "listen_host": "127.0.0.1", "listen_port": 0}
    config.write_text(json.dumps({**document, "token_file": str(token_file)}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["gate.py", str(config)])
    monkeypatch.setenv("GATE_ACCESS_TOKEN", "tok-from-the-environment")
    monkeypatch.setattr(gate, "_tls_context", lambda: type("Plain", (), {"wrap_socket": lambda self, s, **k: s})())

    def stop_serving(httpd):
        httpd.server_close()
        raise SystemExit("stop here")

    monkeypatch.setattr(gate, "serve", stop_serving)
    for absent in (None, ""):  # no file, then an empty one
        if absent is not None:
            token_file.write_text(absent, encoding="utf-8")
        gate.CFG.clear()
        with pytest.raises(SystemExit, match="no token"):
            gate.main()
    token_file.write_text(GATE_TOKEN, encoding="utf-8")
    gate.CFG.clear()
    with pytest.raises(SystemExit, match="stop here"):
        gate.main()
    assert gate.CFG["token"] == GATE_TOKEN
    gate.CFG.clear()


def read_until_cut(answer) -> bytes:
    """What the client received of a body, whether the gate finished it or cut it."""
    received = b""
    try:
        while piece := answer.read(1024):
            received += piece
    except (http.client.IncompleteRead, ConnectionError, ssl.SSLError) as cut:
        received += getattr(cut, "partial", b"")
    return received


@pytest.mark.parametrize(
    ("token", "header"),
    [
        (GATE_TOKEN, ("X-Auth-Token", GATE_TOKEN)),
        (GATE_TOKEN, ("X-Request-Echo", f"you sent {GATE_TOKEN}")),
        (SLASH_TOKEN, ("X-Request-Echo", "you sent tok%2Fgate%2b0123")),  # percent-encoded, hex in both cases
        (SLASH_TOKEN, ("X-Request-Echo", "you sent tok\\/gate\\u002b0123")),  # JSON-escaped
        (SLASH_TOKEN, ("X-Request-Echo", "you sent tok%252Fgate%252b0123")),  # percent-encoded twice
        (LONG_TOKEN, ("X-Request-Echo", f"part {LONG_TOKEN[20:52]} only")),  # 32 characters of it
    ],
    ids=["credential", "echoed", "percent-mixed-case", "json", "nested", "fragment"],
)
def test_o05_a_token_in_any_header_of_an_answer_is_a_502_before_the_body(world, token, header):
    """O05: a header whose name or value, decoded (percent-escapes in either case of hex, JSON escapes), holds the
    gate's token makes the whole answer a 502 with a fixed message, before any byte of its body."""
    gate.CFG["token"] = token
    world.upstream.mode = "fixed"
    world.upstream.answer_headers, world.upstream.answer_body = [header], b'{"body": "plain"}'
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    body = answer.read()
    assert answer.status == 502
    assert b"plain" not in body
    assert header[1] not in json.dumps(answer.getheaders())
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert (line.get("gate_refusal"), line["upstream_status"], line["client_saw"]) == (
        "credential_in_header",
        200,
        "5xx",
    )
    conn.close()


def test_o05_credential_headers_without_the_token_are_withheld_and_the_rest_pass(world):
    """O05: an answer's Authorization, Proxy-Authorization and Set-Cookie never reach the agent, token or not; the
    other headers pass, and so does the answer."""
    world.upstream.mode = "fixed"
    world.upstream.answer_headers = [("Set-Cookie", "session=invented"), ("Authorization", "Basic invented")]
    world.upstream.answer_headers += [("X-Plain", "kept")]
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    answer.read()
    headers = {name.lower(): value for name, value in answer.getheaders()}
    assert answer.status == 200
    assert {"set-cookie", "authorization"} & set(headers) == set()
    assert headers["x-plain"] == "kept"
    conn.close()


def test_o05_a_redirect_is_never_passed_on_nor_followed(world):
    """O05: a 3xx whose Location and body hold the token reaches the agent as a 502 with a fixed message, and the gate
    does not follow it."""
    world.upstream.mode = "redirect"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    body = answer.read()
    assert answer.status == 502
    assert answer.getheader("Location") is None
    assert GATE_TOKEN.encode() not in body
    assert GATE_TOKEN not in json.dumps(answer.getheaders())
    assert len(world.upstream.seen) == 1  # asked once: the redirect was not followed
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert (line["upstream_status"], line["error_origin"], line["client_saw"]) == (302, "upstream", "5xx")
    conn.close()


def test_o05_o16_a_platform_redirect_is_a_gate_refusal_and_its_run_is_set_aside_never_scored(world):
    """O05, O16, C01: the gate's 502 for a redirect is its own refusal (redirect_refused), so the run ends as
    data_gate_integrity: set aside, never scored, even under a condition that treats sandbox failures."""
    from expcore.campaign.attempts import classify
    from expcore.campaign.executor import failure_result
    from expcore_onedata.sandbox import gatelog, kinds

    world.upstream.mode = "redirect"
    conn = connect(world)
    conn.request("GET", FILE)
    conn.getresponse().read()
    conn.close()
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert line.get("gate_refusal") == "redirect_refused"
    error = kinds.gate_error(gatelog.refusals(world.log.read_text(encoding="utf-8").splitlines()))
    assert error is not None
    assert error.kind == "data_gate_integrity"
    result = failure_result(error, ended_utc="2026-01-01T00:00:00Z", wall_s=1.0, treatment_kinds=("sandbox",))
    assert classify(dataclasses.replace(result, model_turns=3), ("sandbox",)) == ("infra_fail", True, False)


@pytest.mark.parametrize(
    ("token", "form"),
    [
        (GATE_TOKEN, GATE_TOKEN.encode()),
        (SLASH_TOKEN, b"tok%2Fgate%2b0123"),  # percent-encoded, hex in both cases
        (SLASH_TOKEN, b'{"t": "tok\\/gate\\u002b0123"}'),  # JSON-escaped
        (SLASH_TOKEN, b"tok%2fgate\\u002B0123"),  # both at once
        (SLASH_TOKEN, b"tok%252Fgate%252b0123"),  # percent-encoded twice
        (SPACE_TOKEN, b"q=tok+gate0123&x=1"),  # a form's + for the space
        (LONG_TOKEN, LONG_TOKEN[10:42].encode()),  # 32 characters of it, on their own
    ],
    ids=["raw", "percent-mixed-case", "json", "mixed", "nested", "plus", "fragment"],
)
def test_o05_a_token_in_the_body_split_between_two_reads_cuts_the_transfer(world, monkeypatch, token, form):
    """O05, O16: the body streams through a detector over its decoded view, which holds back six bytes per character
    of the token: a token in any form across the boundary between two reads is found before any byte of it is sent,
    the transfer is cut (the client's connection reset), and its line says so, with the reason."""
    monkeypatch.setattr(gate, "UPSTREAM_READ_BYTES", SPLIT_AT, raising=False)
    gate.CFG["token"] = token
    world.upstream.mode = "fixed"
    world.upstream.answer_headers = [("Content-Type", OCTETS)]  # file content: it streams through the detector
    world.upstream.answer_body = body = across_reads(form)
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    received = read_until_cut(answer)
    assert answer.status == 200  # the head went out before the body showed the token
    assert len(received) < len(body)
    assert received == body[: len(received)]
    assert len(received) <= body.index(form)  # nothing of the token's form, not a piece of it
    line = wait_for(world, lambda r: r.get("cut") is True)
    assert (line.get("cut_reason"), line.get("gate_refusal")) == ("credential_in_response", "credential_in_response")
    assert (line["client_saw"], line["unread_bytes"]) == ("reset", len(body) - line["bytes"])
    conn.close()


@pytest.mark.parametrize("cut", [1, 2, 3, 5, 7])
def test_o05_overlapping_tokens_are_found_and_nothing_of_them_is_sent(world, monkeypatch, cut):
    """O05: with the token ababa, a body abababa holds it twice, overlapping: the transfer is cut, and the agent gets no
    piece of either, wherever the reads split it."""
    monkeypatch.setattr(gate, "UPSTREAM_READ_BYTES", cut, raising=False)
    gate.CFG["token"] = "ababa"
    world.upstream.mode = "fixed"
    world.upstream.answer_headers = [("Content-Type", OCTETS)]
    world.upstream.answer_body = b"abababa"
    conn = connect(world)
    conn.request("GET", FILE)
    received = read_until_cut(conn.getresponse())
    assert received == b""
    assert wait_for(world, lambda r: r.get("cut") is True)["cut_reason"] == "credential_in_response"
    conn.close()


def percent_encoded(data: bytes) -> bytes:
    """Every byte as %XX."""
    return "".join(f"%{b:02X}" for b in data).encode()


def test_o05_an_error_body_with_a_token_encoded_three_times_across_reads_is_refused_with_nothing_forwarded(
    world, monkeypatch
):
    """O05: an answer that is no file content is buffered whole and checked before any byte of it goes: an error body
    echoing the token percent-encoded three times, longer than any held-back tail and split between two reads, is a
    502 with a fixed message, and the agent gets nothing of it."""
    monkeypatch.setattr(gate, "UPSTREAM_READ_BYTES", SPLIT_AT, raising=False)
    gate.CFG["token"] = LONG_TOKEN
    encoded = percent_encoded(percent_encoded(percent_encoded(LONG_TOKEN.encode())))
    world.upstream.mode, world.upstream.answer_status = "fixed", 400
    world.upstream.answer_headers = [("Content-Type", "application/json")]
    world.upstream.answer_body = across_reads(b'{"error": "bad token ' + encoded + b'"}')
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    body = answer.read()
    assert answer.status == 502
    assert b"bad token" not in body
    assert encoded[:16] not in body
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert (line.get("gate_refusal"), line["upstream_status"], line["client_saw"]) == (
        "credential_in_response",
        400,
        "5xx",
    )
    conn.close()


def test_o05_an_answer_over_the_buffer_cap_that_is_no_file_content_is_refused(world):
    """O05: an answer the gate must buffer that is larger than its cap is a 502 gate refusal, oversized_response."""
    world.upstream.mode, world.upstream.answer_status = "fixed", 500
    world.upstream.answer_headers = [("Content-Type", "application/json")]
    world.upstream.answer_body = b"x" * (CAP + 1)
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    body = answer.read()
    assert answer.status == 502
    assert b"xxxx" not in body
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert (line.get("gate_refusal"), line["upstream_status"]) == ("oversized_response", 500)
    conn.close()


def test_o05_a_file_content_read_larger_than_the_cap_still_streams(world):
    """O05: a successful read of a file's content, with the data content type, streams through the detector whatever
    its size: no cap, nothing refused."""
    world.upstream.mode = "big"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    assert answer.status == 200
    assert len(answer.read()) == BIG
    line = wait_for(world, lambda r: r.get("status") == 200)
    assert line["bytes"] == BIG
    assert "gate_refusal" not in line
    conn.close()


class Slots:
    """The gate's buffer slots with a count of the held and the waiting, for the budget test."""

    def __init__(self, n: int) -> None:
        self._slots, self._lock = threading.BoundedSemaphore(n), threading.Lock()
        self.held = self.most = self.waiting = 0

    def __enter__(self):
        with self._lock:
            self.waiting += 1
        self._slots.acquire()
        with self._lock:
            self.waiting -= 1
            self.held += 1
            self.most = max(self.most, self.held)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self.held -= 1
        self._slots.release()


def test_o05_buffered_answers_share_a_budget_and_a_third_waits_for_a_free_slot(world, monkeypatch):
    """O05: with two buffer slots, two answers that must be buffered are read at once and a third waits for a slot to
    free before it is read; the gate never holds more than two buffers, and all three answers arrive whole."""
    slots = Slots(2)
    monkeypatch.setattr(gate, "_BUFFER_SLOTS", slots, raising=False)
    world.upstream.mode = "stall"
    answers: list[int] = []

    def client():
        conn = connect(world)
        conn.request("GET", FILE)
        answer = conn.getresponse()
        answer.read()
        answers.append(answer.status)
        conn.close()

    threads = [threading.Thread(target=client) for _ in range(3)]
    [t.start() for t in threads]
    deadline = time.monotonic() + 10
    while (slots.held, slots.waiting) != (2, 1) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (slots.held, slots.waiting) == (2, 1)  # two read their answers, the third waits for a slot
    world.upstream.release.set()
    [t.join(timeout=30) for t in threads]
    assert answers == [200, 200, 200]
    assert slots.most == 2


def test_o05_a_compressed_answer_is_refused_before_any_byte_reaches_the_agent(world):
    """O05: the gate asks the upstream for an unencoded body, whatever the agent asked for, and refuses an answer with
    any other Content-Encoding with a 502 and a fixed message, since it cannot look into it: a gzip body that echoes
    the token never reaches the agent."""
    world.upstream.mode = "gzip"
    conn = connect(world)
    conn.request("GET", FILE, headers={"Accept-Encoding": "gzip"})
    answer = conn.getresponse()
    body = answer.read()
    assert world.upstream.seen[0][2].get("Accept-Encoding") == "identity"
    assert answer.status == 502
    assert answer.getheader("Content-Encoding") is None
    assert not body.startswith(b"\x1f\x8b")
    assert GATE_TOKEN.encode() not in body
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert (line["upstream_status"], line["gate_refusal"], line["client_saw"]) == (200, "encoded_response", "5xx")
    conn.close()


def test_o12_the_gate_log_names_a_host_by_its_role_never_by_its_name(world):
    """O12: a request's line names the host it was for by the role the gate serves it in (the zone, the pinned
    provider), or as not configured, never by the configured name the agent's request carried."""
    for host, path in ((PROVIDER, FILE), (ZONE, SCOPE)):
        conn = connect(world, host)
        conn.request("GET" if host == PROVIDER else "POST", path, body=None if host == PROVIDER else b"{}")
        conn.getresponse().read()
        conn.close()
    lines = [wait_for(world, lambda r, n=n: r.get("conn_request") == 1 and r.get("path") == n) for n in (FILE, SCOPE)]
    assert [line["host"] for line in lines] == ["oneprovider", "onezone"]
    text = world.log.read_text(encoding="utf-8")
    assert PROVIDER not in text
    assert ZONE not in text


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE", "PATCH"])
def test_o05_a_write_is_refused_at_the_gate_and_never_reaches_the_upstream(world, method):
    """O05: only read calls pass, so a write the agent attempts is answered 403 by the gate itself."""
    conn = connect(world)
    conn.request(method, FILE, body=b"x" if method != "DELETE" else None)
    answer = conn.getresponse()
    answer.read()
    assert answer.status == 403
    assert world.upstream.seen == []
    refusal = wait_for(world, lambda r: r.get("decision") == "deny")
    assert refusal["status"] == 403
    assert "error_origin" not in refusal  # a refusal is the enforcement, not an error
    conn.close()


def test_o05_a_host_the_gate_was_not_configured_for_is_refused(world, tls):
    """O05: other hosts are refused; the sandbox has no other peer."""
    context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", world.gate_port), timeout=20)
    conn = http.client.HTTPConnection(ZONE, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=ZONE)
    conn.request("GET", "/api/v3/oneprovider/data/f1/content", headers={"Host": "elsewhere.test"})
    answer = conn.getresponse()
    answer.read()
    assert answer.status == 403
    assert world.upstream.seen == []
    assert wait_for(world, lambda r: r.get("status") == 403)["host"] == "not_configured"


def test_o16_concurrent_reads_give_no_gate_origin_5xx(world):
    """O16: 20 clients, 4 reads each over keep-alive connections: every body is whole and the gate made no error."""
    failures: list[str] = []

    def client():
        try:
            conn = connect(world)
            for _ in range(4):
                conn.request("GET", FILE)
                answer = conn.getresponse()
                if answer.status != 200 or answer.read() != BODY:
                    failures.append(f"status {answer.status}")
            conn.close()
        except Exception as error:  # recorded and asserted below
            failures.append(repr(error))

    threads = [threading.Thread(target=client) for _ in range(20)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert failures == []
    time.sleep(0.3)
    lines = records(world)
    reads = [r for r in lines if r.get("decision") == "allow"]
    assert len(reads) == 80
    assert [r for r in lines if r.get("error_origin") == "gate"] == []
    assert [r for r in lines if (r.get("status") or 0) >= 500] == []
    assert {r["bytes"] for r in reads} == {len(BODY)}


def test_o16_a_client_closing_mid_body_leaves_a_line_that_marks_the_cut_transfer(world):
    """O16: a client that closes mid-body yields a close line with the bytes it never read; the gate is not blamed."""
    world.upstream.mode = "big"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    assert answer.status == 200
    assert len(answer.read(1024)) == 1024
    conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # reset on close
    conn.close()
    cut = wait_for(world, lambda r: r.get("cut") is True)
    assert cut["client_aborted"] is True
    assert cut["unread_bytes"] > 0
    assert cut["bytes"] + cut["unread_bytes"] == BIG
    assert "error_origin" not in cut  # the client's own doing, not an error of the gate or of the platform


def test_o16_an_error_the_platform_made_is_marked_upstream_and_seen_as_5xx(world):
    """O16: every failure is logged with its origin, so a run can tell the platform's error from the gate's."""
    world.upstream.mode = "unavailable"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    answer.read()
    assert answer.status == 503
    line = wait_for(world, lambda r: r.get("status") == 503)
    assert line["error_origin"] == "upstream"
    assert line["client_saw"] == "5xx"


def test_o16_a_dead_upstream_is_the_upstreams_error_not_the_gates(world):
    """O16: when nothing answers at the upstream, the log names the upstream as the origin."""
    gate.CFG["hosts"][PROVIDER]["port"] = 1  # nothing listens there
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    answer.read()
    assert answer.status == 502
    line = wait_for(world, lambda r: r.get("status") == 502)
    assert line["error_origin"] == "upstream"
    assert line["client_saw"] == "5xx"


def test_the_gate_imports_nothing_but_the_standard_library_and_its_siblings():
    """It runs alone in its container: nothing of the platform layer, no third-party package, in it or in the siblings
    it loads by path (its policy and its response boundary)."""
    tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    import sys

    assert names - set(sys.stdlib_module_names) == set()
    for sibling in ("gatepolicy.py", "gateboundary.py"):
        tree = ast.parse((Path(gate.__file__).parent / sibling).read_text(encoding="utf-8"))
        imported = {
            a.name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            for a in (n.names if isinstance(n, ast.Import) else [n])
        }
        assert imported <= set(sys.stdlib_module_names), sibling


def test_o05_a_client_that_stops_reading_a_buffered_answer_frees_its_slot(world, monkeypatch):
    """O05: an agent that asks for an answer the gate buffers and then holds its socket open without reading cannot
    keep the buffer slot: the write is bounded, the stalled connection is closed and logged, and the next buffered
    answer is served."""
    slots = Slots(1)
    monkeypatch.setattr(gate, "_BUFFER_SLOTS", slots, raising=False)
    monkeypatch.setattr(gate, "CLIENT_WRITE_TIMEOUT_S", 0.5)
    world.upstream.mode = "fixed"
    world.upstream.answer_status = 200
    world.upstream.answer_headers = [("Content-Type", "application/json")]
    world.upstream.answer_body = b'{"pad": "' + b"p" * (CAP - 64) + b'"}'
    stalled = connect(world)
    stalled.request("GET", FILE)  # and never reads the answer
    time.sleep(0.2)
    served: list[int] = []

    def client():
        conn = connect(world)
        conn.request("GET", FILE)
        answer = conn.getresponse()
        answer.read()
        served.append(answer.status)
        conn.close()

    other = threading.Thread(target=client)
    other.start()
    other.join(timeout=20)
    assert served == [200]
    line = wait_for(world, lambda r: r.get("client_stalled") is True)
    assert line["client_aborted"] is True
    stalled.close()


LISTING = "/api/v3/oneprovider/data/f1/children"


@pytest.mark.parametrize(
    ("query", "body"),
    [("?limit=1001", None), ("?limit=10000", None), ("", b'{"limit": 5000}')],
    ids=["query", "server-maximum", "json-body"],
)
def test_o05_a_listing_above_the_maximum_is_the_agents_4xx_and_nothing_is_forwarded(world, query, body):
    """O05, O16: a listing whose limit is above MAX_LIST_LIMIT is refused before anything goes upstream, with a 400
    whose reason the agent reads; the line is a policy refusal (the agent's own outcome), never a refusal of the
    gate's integrity, so the run is not set aside for it."""
    from expcore_onedata.sandbox import gatelog, gatepolicy, kinds

    conn = connect(world)
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request("GET", LISTING + query, body=body, headers=headers)
    answer = conn.getresponse()
    details = json.loads(answer.read())["error"]["details"]
    assert answer.status == 400
    assert str(gatepolicy.MAX_LIST_LIMIT) in details
    assert "nextPageToken" in details
    assert world.upstream.seen == []
    line = wait_for(world, lambda r: r.get("path") == LISTING)
    assert (line["decision"], line["status"], line["tag"]) == ("deny", 400, "children")
    assert "gate_refusal" not in line
    assert "error_origin" not in line
    lines = world.log.read_text().splitlines()
    assert kinds.gate_error(gatelog.refusals(lines)) is None
    assert gatelog.summarize(lines)["refused_requests"] == 1
    conn.close()


def test_o05_a_listing_without_a_limit_passes_unchanged(world):
    """O05: the official client and an agent may leave the page size to the server; such a listing is forwarded as it
    came, and so is one at the maximum."""
    world.upstream.mode = "fixed"
    world.upstream.answer_headers = [("Content-Type", "application/json")]
    world.upstream.answer_body = b'{"children": [], "isLast": true, "nextPageToken": null}'
    conn = connect(world)
    for target in (LISTING, LISTING + "?limit=1000"):
        conn.request("GET", target)
        answer = conn.getresponse()
        answer.read()
        assert answer.status == 200
    assert [path for _, path, _ in world.upstream.seen] == [LISTING, LISTING + "?limit=1000"]
    conn.close()


def _widest_names(count: int) -> list[str]:
    """`count` distinct names of 255 control characters each: JSON writes each as six bytes, the widest a name gets."""
    digits = [chr(c) for c in range(1, 32)]
    names = []
    for index in range(count):
        tail, n = "", index
        while True:
            tail, n = digits[n % 31] + tail, n // 31
            if n == 0:
                break
        names.append(tail.rjust(255, "\x01"))
    return names


def test_o05_the_fakes_widest_listing_at_the_maximum_stays_under_the_cap_and_is_served(world):
    """O05, O16: a page of MAX_LIST_LIMIT entries, each with every attribute the fake has (a path included), names of
    255 control characters and the fake's longest top-level file id, is under the gate's buffer cap, so the gate checks
    it whole and serves it, and the next page is reached by its token."""
    from fake_onedata import MockOnedata

    from expcore_onedata.sandbox import gateboundary, gatepolicy

    mock = MockOnedata(
        dict.fromkeys(_widest_names(gatepolicy.MAX_LIST_LIMIT + 1), b""),
        space_name="invented-space",
        token=GATE_TOKEN,
    ).start(host="127.0.0.1")
    try:
        gate.CFG["hosts"][PROVIDER]["port"] = mock.port
        root = f"/api/v3/oneprovider/data/{MockOnedata.SPACE_ID}/children"
        conn = connect(world)
        conn.request("GET", f"{root}?limit={gatepolicy.MAX_LIST_LIMIT}")
        answer = conn.getresponse()
        body = answer.read()
        assert answer.status == 200
        page = json.loads(body)
        assert len(page["children"]) == gatepolicy.MAX_LIST_LIMIT
        assert page["isLast"] is False
        assert set(page["children"][0]) >= {"fileId", "name", "path", "type", "size", "mtime", "posixPermissions"}
        assert 3_000_000 < len(body) < gateboundary.BUFFER_CAP
        conn.close()
    finally:
        mock.stop()


def test_o05_every_other_non_content_answer_is_one_bounded_object_far_under_the_cap(world):
    """O05, O16: the scope of the token, a file id looked up and one file's attributes are each one object whose size
    does not grow with a request; the fake's widest (a name of 255 control characters, every attribute) is a few
    kilobytes, served whole after the check."""
    from fake_onedata import MockOnedata

    from expcore_onedata.sandbox import gateboundary, gatepolicy

    name = _widest_names(1)[0]
    plain = "n" * 255  # a name the lookup takes as it is written: the fake's longest top-level file id
    mock = MockOnedata({name: b"x", plain: b"x"}, space_name="invented-space", token=GATE_TOKEN).start(host="127.0.0.1")
    try:
        for host in (ZONE, PROVIDER):
            gate.CFG["hosts"][host]["port"] = mock.port
        file_id = "f" + name.encode().hex()
        calls = [
            (ZONE, "POST", SCOPE, b"{}"),
            (PROVIDER, "POST", f"/api/v3/oneprovider/lookup-file-id/{MockOnedata.SPACE_ID}/{plain}", None),
            (PROVIDER, "GET", f"/api/v3/oneprovider/data/{file_id}", None),
        ]
        per_entry = gatepolicy.NAME_JSON_BYTES + gatepolicy.FILE_ID_BYTES + gatepolicy.ENTRY_REST_BYTES
        for host, method, path, body in calls:
            conn = connect(world, host)
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
            answer = conn.getresponse()
            served = answer.read()
            assert answer.status == 200, path
            assert len(served) < 2 * per_entry < gateboundary.BUFFER_CAP
            conn.close()
    finally:
        mock.stop()


def test_o05_a_listing_body_nested_100000_deep_is_the_agents_400_logged_as_a_policy_refusal(world):
    """O05, O16: a listing whose JSON body is nested 100,000 levels deep is refused before anything is forwarded, with
    a 400 the agent reads; the line is a policy refusal, never an error of the gate (which a parser that runs out of
    stack would make, resetting the connection)."""
    from expcore_onedata.sandbox import gatelog, kinds

    body = b"[" * 100_000 + b"]" * 100_000
    conn = connect(world)
    conn.request("GET", LISTING, body=body, headers={"Content-Type": "application/json"})
    answer = conn.getresponse()
    error = json.loads(answer.read())["error"]
    assert answer.status == 400
    assert error["id"] == "badRequest"
    assert world.upstream.seen == []
    line = wait_for(world, lambda r: r.get("path") == LISTING)
    assert (line["decision"], line["status"], line["tag"]) == ("deny", 400, "children")
    assert "gate_refusal" not in line
    assert "error_origin" not in line
    lines = world.log.read_text().splitlines()
    assert kinds.gate_error(gatelog.refusals(lines)) is None
    assert gatelog.errors([json.loads(x) for x in lines])["policy_refusals"] == 1
    conn.close()
