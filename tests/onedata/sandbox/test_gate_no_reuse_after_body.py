"""O85: the gate never reuses an upstream connection after relaying a request that carried a body.

Measured live (17 Nov, two Onedata providers, Python's http.client): after a request whose body was about 1.5 MiB, the
next request on the same kept-alive connection stalled in about half the tries until the client's timeout, while a fresh
connection answered in milliseconds; 400 KiB and 4 MiB bodies never stalled it. The gate keeps one upstream connection
per client connection and reads an answer for up to 4 h, so a request behind such a body would hang for hours. The gate
relays a body of at most `gate.MAX_BODY` (1 MiB; a request body over it is refused unread), so the test uses the
largest it relays, and the fake upstream stalls the next request on a connection after any body of at least 512 KiB.

The gate runs in-process: a TLS listener on a loopback port, a plain-HTTP upstream in a thread, and keep-alive client
connections whose timeout is a few seconds, so a stalled request fails the test at once instead of waiting out the
step. The upstream reads every request body, notes each request with the connection it arrived on, and answers; a
request it sees twice would be a request re-sent. Host names are invented (`.test`); nothing leaves the machine.
"""

import http.client
import itertools
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

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

PROVIDER = "alpha.test"
ATTRS = "/api/v3/oneprovider/data/f1"  # a file's attributes: a GET with no body
LOOKUP = "/api/v3/oneprovider/lookup-file-id/space1"  # a POST the gate relays, with a body of the client's choosing
CHILDREN = "/api/v3/oneprovider/data/dir1/children"  # a listing: two pages in the complete mode
STALL_AT = 512 * 1024  # a body of at least this leaves its connection stalled for the next request, as measured
CLIENT_TIMEOUT_S = 5  # a request that stalls fails the test in this long
ANSWER = b'{"fileId": "f1"}'
WORD = "after_body"  # the fixed word of `upstream_closed`


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.conn_id = self.server.new_connection()
        self.stalls_next = False  # set by a request that carried a band-sized body: the next one waits to be released

    def log_message(self, *args):
        pass

    def serve(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        if size:
            self.rfile.read(size)  # a body left unread would be read as the next request
        self.server.note((self.conn_id, self.command, self.path.split("?")[0], size))
        if self.stalls_next:
            self.server.release.wait(60)
        self.stalls_next = size >= STALL_AT
        if self.path.split("?")[0] == CHILDREN:
            last = "token=next-a" in self.path
            body = json.dumps(
                {
                    "children": [{"name": "b" if last else "a", "fileId": "id-b" if last else "id-a"}],
                    "isLast": last,
                    "nextPageToken": None if last else "next-a",
                }
            ).encode()
            status = 200
        else:
            body, status = ANSWER, self.server.statuses.get(self.command, 200)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = serve


class Upstream(ThreadingHTTPServer):
    """A plain-HTTP stand-in for a provider that stalls the request after a band-sized body, on that connection only."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.release, self.statuses = threading.Event(), {}
        self.seen: list[tuple[int, str, str, int]] = []  # (connection, method, path, body bytes), in arrival order
        self._lock, self._ids = threading.Lock(), itertools.count(1)

    def new_connection(self) -> int:
        with self._lock:
            return next(self._ids)

    def note(self, request: tuple[int, str, str, int]) -> None:
        with self._lock:
            self.seen.append(request)

    def handle_error(self, request, client_address):  # a client the gate dropped
        pass


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    DockerHost().make_ca(directory, [PROVIDER])
    return directory


@pytest.fixture
def world(tls, tmp_path):
    upstream = Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "requests.jsonl"
    log.touch()
    gate.CFG.clear()
    gate.CFG.update(
        run_key="run-test-1",
        token="tok-gate-0123456789",
        log=str(log),
        tls_dir=str(tls),
        hosts={PROVIDER: {"role": "oneprovider", "addr": "127.0.0.1", "port": upstream.server_port, "tls": False}},
        listen_host="127.0.0.1",
        listen_port=0,
    )
    httpd = gate.GateServer(("127.0.0.1", 0), gate.Handler)
    httpd.socket = gate._tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    clients: list[http.client.HTTPConnection] = []
    yield type(
        "World", (), {"port": httpd.server_port, "upstream": upstream, "log": log, "tls": tls, "clients": clients}
    )
    upstream.release.set()  # a request the old code left stalled ends here, so no thread outlives the test
    for client in clients:
        client.close()
    httpd.shutdown()
    httpd.server_close()
    upstream.shutdown()
    upstream.server_close()
    gate.CFG.clear()


def connect(world) -> http.client.HTTPConnection:
    """One keep-alive client connection to the gate over TLS: its requests share the gate's upstream connection."""
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", world.port), timeout=CLIENT_TIMEOUT_S)
    conn = http.client.HTTPConnection(PROVIDER, timeout=CLIENT_TIMEOUT_S)
    conn.sock = context.wrap_socket(raw, server_hostname=PROVIDER)
    world.clients.append(conn)
    return conn


def send(conn, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    """One request on the client connection: (status, body). A request that stalls fails here, naming what stalled."""
    headers = {"Content-Type": "application/octet-stream"} if body else {}
    try:
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        return answer.status, answer.read()
    except TimeoutError:
        pytest.fail(f"{method} {path} got no answer in {CLIENT_TIMEOUT_S} s: it stalled on a kept upstream connection")


def requests_of(world, count: int) -> list[dict]:
    """The log's request lines, once `count` of them are there (the gate writes a line after it answers)."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        lines = [json.loads(x) for x in world.log.read_text(encoding="utf-8").splitlines() if x.strip()]
        found = [r for r in lines if "method" in r]
        if len(found) >= count:
            return found
        time.sleep(0.05)
    raise AssertionError(f"the gate logged fewer than {count} requests")


def test_o85_the_request_after_the_largest_relayed_body_is_not_stalled_and_goes_on_a_new_connection(world):
    """O85: a POST with a body of `MAX_BODY` is answered, and the GET after it on the same client connection is
    answered at once on a new upstream connection, with each request seen by the upstream exactly once."""
    conn = connect(world)
    assert send(conn, "POST", LOOKUP, b"x" * gate.MAX_BODY) == (200, ANSWER)
    assert send(conn, "GET", ATTRS) == (200, ANSWER)
    first, second = world.upstream.seen
    assert (first[1:], second[1:]) == (("POST", LOOKUP, gate.MAX_BODY), ("GET", ATTRS, 0))
    assert first[0] != second[0]  # a new connection: the first one was closed once its answer was relayed
    assert len(world.upstream.seen) == 2  # nothing was sent twice
    post, get = requests_of(world, 2)
    assert (post["upstream_conn"], post["upstream_closed"], post["status"]) == ("new", WORD, 200)
    assert get["upstream_conn"] == "new"
    assert not {"upstream_stale", "upstream_retries", "upstream_closed"} & set(
        get
    )  # no body, nothing closed or retried


@pytest.mark.parametrize("first", ["GET", "POST"])
def test_o85_a_request_with_no_body_keeps_its_connection_for_the_next(world, first):
    """O85: a GET, or a POST that declares no body, leaves its upstream connection for the next request: it is
    reused, on the same upstream connection, and neither line says it was closed."""
    conn = connect(world)
    assert send(conn, first, ATTRS if first == "GET" else LOOKUP) == (200, ANSWER)
    assert send(conn, "GET", ATTRS) == (200, ANSWER)
    one, two = world.upstream.seen
    assert one[0] == two[0]
    assert len(world.upstream.seen) == 2
    first_line, second_line = requests_of(world, 2)
    assert (first_line["upstream_conn"], second_line["upstream_conn"]) == ("new", "reused")
    assert [r.get("upstream_closed") for r in (first_line, second_line)] == [None, None]


def test_o85_a_request_with_a_body_answered_with_an_error_closes_its_connection_all_the_same(world):
    """O85: the upstream's 403 to a POST with a small body reaches the client as it is, and the connection that
    carried the body is closed once it was relayed, the status making no difference."""
    world.upstream.statuses["POST"] = 403
    conn = connect(world)
    assert send(conn, "POST", LOOKUP, b'{"path": "/x"}') == (403, ANSWER)
    assert send(conn, "GET", ATTRS) == (200, ANSWER)
    one, two = world.upstream.seen
    assert one[0] != two[0]
    assert len(world.upstream.seen) == 2
    post, get = requests_of(world, 2)
    assert (post["status"], post["upstream_closed"]) == (403, WORD)
    assert (get["upstream_conn"], "upstream_closed" in get) == ("new", False)


def test_o85_a_body_carrying_request_after_a_body_less_one_is_sent_once_on_the_kept_connection(world):
    """O85: the rule decides what happens after a request, never before one: a POST with a body that follows a GET goes
    on that GET's kept connection, once, and the GET after the POST goes on a new one."""
    conn = connect(world)
    assert send(conn, "GET", ATTRS) == (200, ANSWER)
    assert send(conn, "POST", LOOKUP, b'{"path": "/x"}') == (200, ANSWER)
    assert send(conn, "GET", ATTRS) == (200, ANSWER)
    get_one, post, get_two = world.upstream.seen
    assert get_one[0] == post[0] != get_two[0]
    assert len(world.upstream.seen) == 3
    lines = requests_of(world, 3)
    assert [r["upstream_conn"] for r in lines] == ["new", "reused", "new"]
    assert [r.get("upstream_closed") for r in lines] == [None, WORD, None]


def test_o85_each_page_of_a_complete_listing_with_a_body_goes_on_a_new_connection(world):
    """O85: a complete listing's pages are requests of their own with the agent's body: the connection that carried
    page one is closed before page two, each page is asked once, and the merged answer is the one it always was."""
    gate.CFG["listing"] = "complete"
    conn = connect(world)
    status, answer = send(conn, "GET", CHILDREN, b'{"attribute": ["name"]}')
    assert status == 200
    assert [entry["name"] for entry in json.loads(answer)["children"]] == ["a", "b"]
    one, two = world.upstream.seen
    assert (one[2], two[2]) == (CHILDREN, CHILDREN)
    assert one[0] != two[0]
    assert len(world.upstream.seen) == 2
    (listing,) = requests_of(world, 1)
    assert (listing["pages"], listing["upstream_closed"]) == (2, WORD)
