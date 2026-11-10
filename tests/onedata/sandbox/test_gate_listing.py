"""O19: in the complete listing mode the data gate answers a listing with every entry of the directory, or with none.

The gate runs in-process, as in test_gate.py: a TLS listener on a loopback port, a plain HTTP upstream in a thread, and
clients that speak TLS to the gate with the run's test CA. The upstream is the fake platform, which pages a directory
by an index token, or a stand-in that answers pages the test writes. Every name is invented; nothing leaves the machine.
"""

import http.client
import json
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fake_onedata import MockOnedata

from expcore_onedata.sandbox import gate, gatelog, kinds
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

ZONE, PROVIDER = "zone.test", "alpha.test"  # invented hosts the gate serves
GATE_TOKEN = "tok-gate-0123456789"  # an invented token value: the gate holds it, the client never does
ROOT = f"/api/v3/oneprovider/data/{MockOnedata.SPACE_ID}/children"
PAGES = "/api/v3/oneprovider/data/dir0001/children"  # the directory of the stand-in that answers written pages


class WrittenPages(ThreadingHTTPServer):
    """A stand-in provider that answers the n-th listing request with the n-th written page (status, body)."""

    daemon_threads = True

    def __init__(self, pages):
        super().__init__(("127.0.0.1", 0), PageHandler)
        self.pages, self.seen = list(pages), []


class PageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(self.path)
        status, body = self.server.pages.pop(0) if self.server.pages else (500, b"{}")
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def page(names, last, token=None):
    """A listing page of the written stand-in: entries named `names`."""
    return 200, {"children": [{"name": n, "fileId": f"id-{n}"} for n in names], "isLast": last, "nextPageToken": token}


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    DockerHost().make_ca(directory, [ZONE, PROVIDER])
    return directory


def start_gate(tls, tmp_path, port, listing):
    """The gate in-process, serving PROVIDER from the upstream at `port`, in the listing mode given."""
    log = tmp_path / "requests.jsonl"
    log.touch()
    gate.CFG.clear()
    gate.CFG.update(
        run_key="run-test-1",
        token=GATE_TOKEN,
        log=str(log),
        tls_dir=str(tls),
        listing=listing,
        hosts={PROVIDER: {"role": "oneprovider", "addr": "127.0.0.1", "port": port, "tls": False}},
    )
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, log


@pytest.fixture
def served(tls, tmp_path):
    """Start a gate in front of an upstream; everything started is stopped at the end."""
    started = []

    def make(upstream, listing="complete"):
        if isinstance(upstream, MockOnedata):
            upstream.start(host="127.0.0.1")
            port = upstream.port
        else:
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            port = upstream.server_port
        httpd, log = start_gate(tls, tmp_path, port, listing)
        started.append((httpd, upstream))
        return type("Served", (), {"port": httpd.server_port, "log": log, "tls": tls})

    yield make
    for httpd, upstream in started:
        httpd.shutdown()
        httpd.server_close()
        if isinstance(upstream, MockOnedata):
            upstream.stop()
        else:
            upstream.shutdown()
            upstream.server_close()
    gate.CFG.clear()


def request(world, target, body=None) -> tuple[int, bytes]:
    """One request to the gate over TLS: (status, the body as received)."""
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    conn = http.client.HTTPConnection(PROVIDER, timeout=30)
    raw = socket.create_connection(("127.0.0.1", world.port), timeout=30)
    conn.sock = context.wrap_socket(raw, server_hostname=PROVIDER)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request("GET", target, body=body, headers=headers)
    answer = conn.getresponse()
    data = answer.read()
    conn.close()
    return answer.status, data


def get(world, target, body=None):
    """One request to the gate over TLS: (status, the body parsed, or as received when it is not JSON)."""
    status, data = request(world, target, body)
    try:
        return status, json.loads(data)
    except ValueError:
        return status, data


def line(world, seconds=10.0) -> dict:
    """The log line of the one listing request."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = [json.loads(x) for x in world.log.read_text().splitlines() if x.strip() and '"method"' in x]
        if found:
            return found[-1]
        time.sleep(0.05)
    raise AssertionError("no request line was logged")


def test_o19_a_complete_listing_merges_every_page_in_the_servers_order_and_a_paged_one_does_not(served):
    """O19: a directory of 2,500 entries asked for as the official client asks (one page of 1000) comes back whole, in
    the server's order, as the last page; the gate read three pages of 1000 upstream, each with the token before. The
    same directory through a gate in the paged mode is the server's first page and its token."""
    tree = {f"file-{n:05d}.bin": b"x" for n in range(2500)}
    mock = MockOnedata(tree, space_name="invented-space", token=GATE_TOKEN)
    world = served(mock)
    status, data = request(world, f"{ROOT}?limit=1000")
    answer = json.loads(data)
    assert status == 200
    assert [entry["name"] for entry in answer["children"]] == sorted(tree)
    assert (answer["isLast"], answer["nextPageToken"]) == (True, None)
    asked = [r.query for r in mock.requests if r.path == ROOT]
    assert [q.get("limit") for q in asked] == ["1000"] * 3
    assert [q.get("token") for q in asked] == [None, "1000", "2000"]
    logged = line(world)
    assert (logged["complete_listing"], logged["pages"], logged["entries"]) == (True, 3, 2500)
    assert (logged["status"], logged["body"], logged["decision"]) == (200, "listing", "allow")
    assert logged["body_bytes"] == len(data)  # the merged answer, as the agent received it
    paged = served(MockOnedata(tree, space_name="invented-space", token=GATE_TOKEN), listing="paged")
    status, answer = get(paged, f"{ROOT}?limit=1000")
    assert status == 200
    assert len(answer["children"]) == 1000
    assert (answer["isLast"], answer["nextPageToken"]) == (False, "1000")
    assert "complete_listing" not in line(paged)


def test_o19_a_short_page_that_is_not_the_last_is_followed(served):
    """O19: a page may be short and still not the last (the API says `isLast` is the only sign of the end), so the gate
    goes on from it."""
    upstream = WrittenPages([page(["a", "b", "c"], False, "next-a"), page([], False, "next-b"), page(["d"], True)])
    world = served(upstream)
    status, answer = get(world, PAGES)
    assert status == 200
    assert [entry["name"] for entry in answer["children"]] == ["a", "b", "c", "d"]
    tokens = [parse_qs(urlsplit(path).query).get("token") for path in upstream.seen]
    assert tokens == [None, ["next-a"], ["next-b"]]


@pytest.mark.parametrize(
    "second",
    [
        (200, {"children": [{"name": "z"}], "nextPageToken": None}),  # no isLast
        (200, {"children": [{"name": "z"}], "isLast": "false", "nextPageToken": "next-b"}),  # isLast not a boolean
        (200, {"children": [{"name": "z"}], "isLast": False, "nextPageToken": None}),  # not the last, and no token
        (200, {"children": [{"name": "z"}], "isLast": False, "nextPageToken": "next-a"}),  # a token given before
        (200, {"children": {"name": "z"}, "isLast": True}),  # children not a list
        (200, b"not json"),
        (404, {"error": {"id": "posix", "details": "enoent"}}),  # a page after the first that is not a 200
        (503, b""),
    ],
    ids=["no-is-last", "is-last-text", "no-token", "token-again", "children-object", "not-json", "404", "503"],
)
def test_o19_a_page_the_listing_cannot_go_on_from_is_the_platforms_502_and_no_entry_is_passed(served, second):
    """O19: after a first page that is not the last, a second page that does not end the listing or continue it, or
    that is not a 200, is the platform's failure: the agent gets a 502 of origin `upstream` and no entry of the first
    page, never a part of the listing as if it were the whole, and the gate refuses nothing for its own integrity."""
    world = served(WrittenPages([page(["a", "b"], False, "next-a"), second]))
    status, answer = get(world, PAGES)
    assert status == 502
    assert b'"a"' not in json.dumps(answer).encode()
    logged = line(world)
    assert (logged["error_origin"], logged["client_saw"], logged["pages"]) == ("upstream", "5xx", 1)
    assert "gate_refusal" not in logged
    assert kinds.gate_error(gatelog.refusals(world.log.read_text().splitlines())) is None


def test_o19_the_first_pages_answer_that_is_not_a_200_is_passed_on_as_the_paged_mode_passes_it(served):
    """O19: a listing of a directory that does not exist gets the platform's 404 to it, as it came."""
    world = served(WrittenPages([(404, {"error": {"id": "posix", "details": "enoent"}})]))
    status, answer = get(world, PAGES)
    assert (status, answer) == (404, {"error": {"id": "posix", "details": "enoent"}})
    logged = line(world)
    assert (logged["status"], logged["pages"], logged["body"]) == (404, 0, "other")


@pytest.mark.parametrize(("bound", "value"), [("bytes", 600), ("pages", 2)])
def test_o19_a_listing_past_its_bound_is_refused_for_the_gates_integrity_and_its_run_set_aside(
    served, monkeypatch, bound, value
):
    """O19: a complete listing larger than the gate serves whole (bytes) or longer (pages) is refused with a 502 and
    `listing_too_large`, a refusal for the gate's integrity, so the run ends as `data_gate_integrity` and is set
    aside; no entry is passed on."""
    monkeypatch.setattr(
        gate.gatepolicy, "COMPLETE_LISTING_BYTES" if bound == "bytes" else "COMPLETE_LISTING_PAGES", value
    )
    pages = [page([f"n{k}-{i:03d}" for i in range(10)], False, f"next-{k}") for k in range(3)] + [page(["end"], True)]
    world = served(WrittenPages(pages))
    status, answer = get(world, PAGES)
    assert status == 502
    assert b"n0-000" not in json.dumps(answer).encode()
    logged = line(world)
    assert (logged["gate_refusal"], logged["listing_bound"]) == ("listing_too_large", bound)
    error = kinds.gate_error(gatelog.refusals(world.log.read_text().splitlines()))
    assert error is not None
    assert error.kind == kinds.DATA_GATE_INTEGRITY


@pytest.mark.parametrize(
    ("query", "body"),
    [("?token=t1", None), ("?limit=1000&index=file-1", None), ("", b'{"offset": 3}')],
    ids=["token", "index", "offset-in-body"],
)
def test_o19_a_complete_listing_that_names_where_to_start_is_the_agents_400(served, query, body):
    """O19: a listing that names where it starts could not be the directory's complete listing, so it is refused with a
    400 the agent reads, a policy refusal, and nothing is forwarded."""
    upstream = WrittenPages([page(["a"], True)])
    world = served(upstream)
    status, answer = get(world, PAGES + query, body)
    assert status == 400
    assert "token, index or offset" in answer["error"]["details"]
    assert upstream.seen == []
    logged = line(world)
    assert (logged["decision"], logged["status"], logged["body"]) == ("deny", 400, "gate")


def test_o19_the_page_size_is_the_gates_and_the_agents_other_options_go_with_every_page(served):
    """O19: the agent's limit (here above what the paged mode allows) and the large-listing hint are dropped from the
    query and the body; every page goes with the gate's limit, the agent's attributes and its other options."""
    mock = MockOnedata({f"file-{n:05d}.bin": b"x" for n in range(1500)}, space_name="invented-space", token=GATE_TOKEN)
    world = served(mock)
    body = json.dumps({"limit": 5000, "tuneForLargeContinuousListing": True, "attributes": ["name", "size"]}).encode()
    status, answer = get(world, f"{ROOT}?limit=7&tuneForLargeContinuousListing=true&other=kept", body)
    assert status == 200
    assert len(answer["children"]) == 1500
    assert set(answer["children"][0]) == {"name", "size"}
    asked = [r for r in mock.requests if r.path == ROOT]
    assert [(r.query.get("limit"), r.query.get("other")) for r in asked] == [("1000", "kept"), ("1000", "kept")]
    assert all("tuneForLargeContinuousListing" not in r.query for r in asked)
    assert all(json.loads(r.body) == {"attributes": ["name", "size"]} for r in asked)


def test_o19_a_page_that_holds_the_gates_token_refuses_the_listing_and_passes_no_entry(served):
    """O19, O05: a page passes the response boundary as any answer that is no file's content does: one that holds the
    gate's token is refused for the gate's integrity, and nothing of the listing goes."""
    world = served(WrittenPages([page(["a"], False, "next-a"), page([f"echo {GATE_TOKEN}"], True)]))
    status, answer = get(world, PAGES)
    assert status == 502
    assert b'"a"' not in json.dumps(answer).encode()
    assert line(world)["gate_refusal"] == "credential_in_response"


def test_o19_a_gate_given_a_listing_mode_it_does_not_know_refuses_to_start(tmp_path, monkeypatch):
    """O19: the mode comes from the gate's configuration; one it does not know stops it before it serves anything."""
    config = tmp_path / "gate.json"
    document = {"log": str(tmp_path / "r.jsonl"), "hosts": {}, "listen_port": 0, "listing": "everything"}
    config.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["gate.py", str(config)])
    monkeypatch.setenv(gate.TOKEN_ENV, GATE_TOKEN)
    gate.CFG.clear()
    with pytest.raises(SystemExit, match="listing mode"):
        gate.main()
    gate.CFG.clear()
