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

import pytest

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

ZONE, PROVIDER = "zone.test", "provider.test"
FILE = "/api/v3/oneprovider/data/f1/content"
BODY = bytes(range(256)) * 64  # 16 KiB
BIG = 12 * 1024 * 1024
RUN_HEADER_VALUE = "run-test-1"


class Upstream(ThreadingHTTPServer):
    """A plain-HTTP stand-in for Onezone and a Oneprovider, recording what reached it."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.seen: list[tuple[str, str, dict]] = []
        self.mode = "ok"


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
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


def test_the_gate_imports_nothing_but_the_standard_library_and_its_policy():
    """It runs alone in its container: nothing of the platform layer, no third-party package."""
    tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    import sys

    assert names - set(sys.stdlib_module_names) == set()
    policy = ast.parse((Path(gate.__file__).parent / "gatepolicy.py").read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
        for n in ast.walk(policy)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert imported <= set(sys.stdlib_module_names)
