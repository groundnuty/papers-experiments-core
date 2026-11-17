"""O77: a gate whose upstream is a declared name that resolves nowhere dials the mapped address, and asks TLS, the
certificate check and `Host` for the declared name; no answer it gives its client holds the address or the port.

The gate runs in-process: a TLS listener on a loopback port, and a TLS upstream on another whose certificate names the
declared name alone. Names are invented (`.test` resolves nowhere); nothing leaves the machine.
"""

import http.client
import json
import socket
import ssl
import threading

import pytest
from tls_peer import TlsPeer

from expcore_onedata.sandbox import gate
from expcore_onedata.sandbox.hostops import DockerHost

SERVED = "depot.layout.test"  # the declared name: the URL's host, the name the gate serves and its certificate holds
DIAL = "127.0.0.1"  # the mapped address: where the node is, never its client's to see
FILE = "/api/v3/oneprovider/data/f1/content"


def _gate(tmp_path, upstream, **host):
    """A gate serving SERVED, its upstream the declared name with `host`'s keys over it; returns it and its TLS."""
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
        hosts={SERVED: {"role": "oneprovider", "addr": SERVED, "port": upstream.port, "tls": True, **host}},
        listen_host="127.0.0.1",
        listen_port=0,
    )
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, gate_tls


def _get(httpd, gate_tls) -> tuple[int, dict]:
    context = ssl.create_default_context(cafile=str(gate_tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", httpd.server_port), timeout=20)
    conn = http.client.HTTPConnection(SERVED, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=SERVED)
    conn.request("GET", FILE)
    answer = conn.getresponse()
    status, body = answer.status, json.loads(answer.read())
    conn.close()
    return status, body


@pytest.fixture
def stage(tmp_path):
    started = []

    def make(upstream, **host):
        httpd, tls = _gate(tmp_path, upstream, **host)
        started.append(httpd)
        return httpd, tls

    yield make
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()
    gate.CFG.clear()
    gate._CTX.clear()


def test_o77_the_gate_dials_the_mapped_address_and_asks_tls_and_host_for_the_declared_name(tmp_path, stage):
    """O77: the upstream is reached though its name resolves nowhere: the handshake asked for the declared name, which
    its certificate holds alone, and the request's `Host` is the name the client used."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        status, _ = _get(*stage(upstream, dial=DIAL))
        assert status == 200
        assert upstream.sni == [SERVED]
        assert upstream.requests == [{"method": "GET", "path": FILE, "host": SERVED}]


def test_o77_without_a_dial_address_the_declared_name_reaches_nothing(tmp_path, stage):
    """O77: the same upstream with no `dial` fails: it is the address that connects, and nothing of the gate's answer
    names the address or the port."""
    with TlsPeer(tmp_path / "up", SERVED) as upstream:
        status, body = _get(*stage(upstream))
        assert status == 502
        assert upstream.requests == []
        assert not [word for word in (DIAL, str(upstream.port)) if word in json.dumps(body)]


def test_o77_the_certificate_is_still_checked_against_the_declared_name(tmp_path, stage):
    """O77: an upstream whose certificate names another host is refused although the gate dials its address, and the
    refusal names the served name, never the address or the port."""
    with TlsPeer(tmp_path / "up", "other.layout.test") as upstream:
        status, body = _get(*stage(upstream, dial=DIAL))
        assert status == 502
        assert upstream.requests == []
        text = json.dumps(body)
        assert SERVED in text
        assert DIAL not in text
        assert str(upstream.port) not in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("connect to 192.0.2.10:8443 failed", f"connect to {SERVED}:443 failed"),
        ("no route to [192.0.2.10]:8443 or 192.0.2.10", f"no route to {SERVED}:443 or {SERVED}"),
        ("not valid for 'depot.layout.test'.", "not valid for 'depot.layout.test'."),
    ],
)
def test_o77_the_dialled_address_is_named_by_the_served_name_in_a_failure(text, expected):
    """O77: a failure's text that quotes the address the gate connected to, with or without the port, is given to the
    client with the served name and 443 in its place; a text that holds only the served name is unchanged."""
    hcfg = {"role": "oneprovider", "addr": SERVED, "dial": "192.0.2.10", "port": 8443, "tls": True}
    assert gate._as_served(text, SERVED, hcfg) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Content-Length: depot.layout.test:8443", "Content-Length: depot.layout.test:443"),
        (
            "refused at DEPOT.layout.test:8443 and [depot.layout.test]:8443",
            "refused at depot.layout.test:443 and depot.layout.test:443",
        ),
        ("not valid for 'depot.layout.test'.", "not valid for 'depot.layout.test'."),
    ],
)
def test_o77_a_declared_name_with_the_upstream_port_is_given_on_443(text, expected):
    """O77: the URL's host is the served name itself on a mapped federation, so the gate's failure text may quote
    `name:8443` (a malformed answer's header, a library's repr): the port of the nodes never reaches the client, and the
    bare name, which needs no replacement, is left as it is."""
    hcfg = {"role": "oneprovider", "addr": SERVED, "dial": "192.0.2.10", "port": 8443, "tls": True}
    assert gate._as_served(text, SERVED, hcfg).lower() == expected.lower()


def test_o77_a_host_with_a_node_name_and_an_address_names_both_by_the_served_name():
    """O77: a gate whose URL host is a node name and which also dials an address (a map of the node's own) names both
    by the served name, the longer form first."""
    hcfg = {"role": "oneprovider", "addr": "node7.site.test", "dial": "192.0.2.10", "port": 8443, "tls": True}
    text = "node7.site.test:8443 then 192.0.2.10:8443 then node7.site.test"
    assert gate._as_served(text, SERVED, hcfg) == f"{SERVED}:443 then {SERVED}:443 then {SERVED}"
