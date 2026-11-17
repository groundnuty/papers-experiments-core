"""O69: a gate whose names are not the hosts it dials never gives its client those hosts: the 502 of a failed upstream
call names the host it dialled by the name it serves, on 443, and the log's line keeps what was dialled.

The gate runs in-process: a TLS listener on a loopback port, and a TLS upstream in a thread whose certificate names
another host than the one the gate dials, so the upstream call fails with the name mismatch that quotes the dialled
host. Host names are invented (`.test`); nothing leaves the machine.
"""

import http.client
import json
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the test CAs")

SERVED = "alpha.layout.test"  # the name the gate serves, as a layout declares it
DIALLED = "localhost"  # the host the gate dials, standing in for a node's name: never its client's to see
CERTIFIED = "other.test"  # the only name the upstream's certificate holds
FILE = "/api/v3/oneprovider/data/f1/content"


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _TlsUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # the gate refuses its certificate: expected, and quiet
        pass


@pytest.fixture
def world(tmp_path):
    gate_tls, upstream_tls = tmp_path / "gate-tls", tmp_path / "upstream-tls"
    DockerHost().make_ca(gate_tls, [SERVED])
    DockerHost().make_ca(upstream_tls, [CERTIFIED])
    upstream = _TlsUpstream(("127.0.0.1", 0), _Quiet)
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(upstream_tls / f"{CERTIFIED}.pem", upstream_tls / f"{CERTIFIED}.key")
    upstream.socket = serving.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "requests.jsonl"
    log.touch()
    port = upstream.server_port
    gate.CFG.clear()
    gate._CTX.clear()  # a context another test built holds another CA
    gate.CFG.update(
        run_key="run-test-1",
        token="tok-gate-0123456789",
        log=str(log),
        tls_dir=str(gate_tls),
        verify_ssl=True,
        upstream_ca_file=str(upstream_tls / "ca.pem"),
        hosts={SERVED: {"role": "oneprovider", "addr": DIALLED, "port": port, "tls": True}},
        listen_host="127.0.0.1",
        listen_port=0,
    )
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield type("World", (), {"gate_port": httpd.server_port, "log": log, "tls": gate_tls, "port": port})
    httpd.shutdown()
    httpd.server_close()
    upstream.shutdown()
    upstream.server_close()
    gate.CFG.clear()
    gate._CTX.clear()


def _get(world) -> tuple[int, dict]:
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", world.gate_port), timeout=20)
    conn = http.client.HTTPConnection(SERVED, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=SERVED)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    status, body = answer.status, json.loads(answer.read())
    conn.close()
    return status, body


def _line(world) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        lines = [json.loads(text) for text in world.log.read_text().splitlines() if text.strip()]
        found = [line for line in lines if line.get("status") == 502]
        if found:
            return found[-1]
        time.sleep(0.05)
    raise AssertionError("the gate logged no 502")


def test_o69_an_upstream_failure_names_the_served_name_never_the_dialled_host(world):
    """O69: the upstream's certificate does not hold the host the gate dialled, and the failure's text quotes that
    host; the gate's answer names it by the name it serves, and the log's line keeps the host it dialled."""
    status, body = _get(world)
    assert status == 502
    details = body["error"]["details"]
    assert DIALLED not in details
    assert str(world.port) not in details
    assert SERVED in details
    line = _line(world)
    assert line["error_origin"] == "upstream"
    assert DIALLED in line["error"]  # the evidence keeps what failed, as before


@pytest.mark.parametrize(
    ("text", "addr", "expected"),
    [
        ("not valid for 'node7.site.test'.", "node7.site.test", f"not valid for '{SERVED}'."),
        ("refused at NODE7.site.test:8443", "node7.site.test", f"refused at {SERVED}:443"),
        ("refused at [fd00::7]:8443 and fd00::7", "fd00::7", f"refused at {SERVED}:443 and {SERVED}"),
        ("Connection refused", "node7.site.test", "Connection refused"),
    ],
)
def test_o69_the_dialled_host_and_port_are_named_by_the_served_name_and_443(text, addr, expected):
    """O69: the host alone, the host with its port (an IPv6 address in brackets too) and any case, in one pass, so a
    served name that holds the dialled host is never named twice; a text with neither is unchanged."""
    hcfg = {"role": "oneprovider", "addr": addr, "port": 8443, "tls": True}
    assert gate._as_served(text, SERVED, hcfg) == expected


def test_o69_a_gate_whose_name_is_the_host_it_dials_gives_the_text_unchanged():
    """O69: Docker's gate serves the names it dials, so its answers' text is as it always was."""
    hcfg = {"role": "oneprovider", "addr": SERVED, "port": 8443, "tls": True}
    text = f"not valid for '{SERVED}' at {SERVED}:8443"
    assert gate._as_served(text, SERVED, hcfg) == text
    contained = {"role": "oneprovider", "addr": "alpha", "port": 8443, "tls": True}
    assert gate._as_served("alpha:8443 and alpha", SERVED, contained) == f"{SERVED}:443 and {SERVED}"
