"""O16, O05: the data gate makes no errors of its own, marks the transfers it cut, and passes only allowed reads.

The gate runs in-process here: a TLS listener on a loopback port, a plain HTTP upstream in a thread, and clients that
speak TLS to the gate with the run's test CA. Host names are invented (`.test`); nothing leaves the machine.
"""

import ast
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
SPECIAL_TOKEN = 'tok/gate+"0123'  # an invented token whose percent-encoded and JSON-escaped forms differ from it
SPLIT_AT = 4096  # the gate's read size in the tests that split a token between two reads of the upstream's body


def token_forms(token: str) -> list[bytes]:
    """The forms a body can hold a token in, made here independently of the gate: raw, percent-encoded, JSON-escaped."""
    return [token.encode(), quote(token, safe="").encode(), json.dumps(token)[1:-1].encode()]


def straddling(forms: list[bytes]) -> bytes:
    """A body with each form across a boundary between two reads of SPLIT_AT bytes, and filler between them."""
    body = b""
    for k, form in enumerate(forms, 1):
        body += b"a" * (k * SPLIT_AT - len(form) // 2 - len(body)) + form
    return body + b"b" * 64


class Upstream(ThreadingHTTPServer):
    """A plain-HTTP stand-in for Onezone and a Oneprovider, recording what reached it."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.seen: list[tuple[str, str, dict]] = []
        self.bodies: list[bytes] = []
        self.mode = "ok"


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
        if self.server.mode in ("echo-headers", "redirect", "split"):
            self._echo(self.headers.get("X-Auth-Token", ""))
            return
        if self.server.mode == "unavailable":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = BIG if self.server.mode == "big" else len(BODY)
        self.send_response(200)
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

    def _echo(self, token: str) -> None:
        """A platform that hands back the token it was sent: in headers, in a redirect, in a body across two reads."""
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
        else:
            body = straddling(token_forms(token))
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


def test_o05_the_gate_takes_its_token_from_its_environment_and_removes_it(tmp_path, monkeypatch):
    """O05: the token is read from the environment, never from the configuration file, and removed from it."""
    config = tmp_path / "gate.json"
    config.write_text(
        json.dumps({"log": str(tmp_path / "r.jsonl"), "hosts": {}, "listen_host": "127.0.0.1", "listen_port": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["gate.py", str(config)])
    monkeypatch.setenv(gate.TOKEN_ENV, GATE_TOKEN)
    gate.CFG.clear()
    monkeypatch.setattr(gate, "_tls_context", lambda: type("Plain", (), {"wrap_socket": lambda self, s, **k: s})())

    def stop_serving(httpd):
        httpd.server_close()
        raise SystemExit("stop here")

    monkeypatch.setattr(gate, "serve", stop_serving)
    with pytest.raises(SystemExit, match="stop here"):
        gate.main()
    assert gate.CFG["token"] == GATE_TOKEN
    assert gate.TOKEN_ENV not in __import__("os").environ
    monkeypatch.delenv(gate.TOKEN_ENV, raising=False)
    gate.CFG.clear()
    with pytest.raises(SystemExit, match="no token"):
        gate.main()
    gate.CFG.clear()


def test_o05_a_token_the_upstream_echoes_in_a_header_never_reaches_the_agent(world):
    """O05: an answer's credential headers (X-Auth-Token, Authorization, Set-Cookie) and any header whose value holds
    the token are withheld at the gate's boundary; the other headers pass."""
    world.upstream.mode = "echo-headers"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    answer.read()
    headers = {name.lower(): value for name, value in answer.getheaders()}
    assert answer.status == 200
    assert GATE_TOKEN not in json.dumps(answer.getheaders())
    assert {"x-auth-token", "authorization", "set-cookie", "x-request-echo"} & set(headers) == set()
    assert headers["x-plain"] == "kept"
    assert world.upstream.seen[0][2]["X-Auth-Token"] == GATE_TOKEN  # the gate did send it
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


@pytest.mark.parametrize("token", [GATE_TOKEN, SPECIAL_TOKEN], ids=["plain", "special"])
def test_o05_a_token_split_between_two_reads_of_the_body_never_reaches_the_agent(world, monkeypatch, token):
    """O05: the body is scrubbed as it streams: each form of the token (raw, percent-encoded, JSON-escaped) across the
    boundary between two reads of the upstream's body is covered, and the body keeps the length it declared."""
    monkeypatch.setattr(gate, "UPSTREAM_READ_BYTES", SPLIT_AT, raising=False)
    gate.CFG["token"] = token
    world.upstream.mode = "split"
    conn = connect(world)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    body = answer.read()
    sent = straddling(token_forms(token))
    assert answer.status == 200
    assert [form for form in token_forms(token) if form in body] == []
    assert len(body) == len(sent) == int(answer.getheader("Content-Length"))
    assert body.endswith(b"b" * 64)
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
