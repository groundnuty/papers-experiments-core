"""O73: the gate's log says how long each relayed request took, and where it waited, so a stall is measurable in a run.

The gate runs in-process: a TLS listener on a loopback port, a plain-HTTP upstream in a thread whose answers follow a
plan, and one keep-alive client connection (the gate keeps one upstream connection per client connection, so a second
request on it reuses the first's). Time is a fake clock the gate's duration stamps read (`gate._clock`) and the upstream
advances: a stall of a minute takes no time in the test, and every figure is exact. The upstream advances the clock
before it writes anything, so the gate's stamp of the first answer byte always comes after the advance. Host names are
invented (`.test`); nothing leaves the machine.
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

from expcore_onedata.sandbox import gate, gatelog
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the run CA")

PROVIDER = "alpha.test"
ATTRS = "/api/v3/oneprovider/data/f1"  # a file's attributes: an answer that is no content, buffered and relayed whole
CONTENT = "/api/v3/oneprovider/data/f1/content"  # a file's content: streamed as it arrives
OCTETS = "application/octet-stream"
STOPPED = {
    "signalled": True,
    "exit_code": 0,
    "output_read": True,
    "log_write_failed": False,
    "teardown_incomplete": False,
}
DONE = json.dumps({"event": "teardown_complete", "connections": 1})
NO_BAND = {"requests": 0, "over_10s": 0, "over_30s": 0, "over_60s": 0}


class Clock:
    """A clock that moves only when a test says: the gate's durations are then the sum of the advances it saw."""

    def __init__(self) -> None:
        self._now, self._lock = 1000.0, threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        kind, seconds = self.server.next_step()
        self.server.reached.set()
        self.server.clock.advance(seconds)  # before a byte is written: the gate's stamps come after it
        if kind == "drop":  # the connection ends with no answer: the gate may try once more
            self.close_connection = True
            return
        if kind == "stream":  # a file's content: its first half now, the rest when the test releases it
            self.send_response(200)
            self.send_header("Content-Type", OCTETS)
            self.send_header("Content-Length", "20")
            self.end_headers()
            self.wfile.write(b"a" * 10)
            self.wfile.flush()
            self.server.release.wait(30)
            self.wfile.write(b"b" * 10)
            return
        if kind == "hold":  # an answer that does not come until the test releases it
            self.server.release.wait(30)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Upstream(ThreadingHTTPServer):
    """A plain-HTTP stand-in for a provider: each request takes the next step of `plan` (an answer after advancing the
    clock, a dropped connection, a streamed content, an answer held back), and an empty plan answers at once."""

    daemon_threads = True

    def __init__(self, clock: Clock) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.clock = clock
        self.plan: list[tuple[str, float]] = []
        self.release, self.reached = threading.Event(), threading.Event()
        self._plan_lock = threading.Lock()

    def next_step(self) -> tuple[str, float]:
        with self._plan_lock:
            return self.plan.pop(0) if self.plan else ("answer", 0)

    def handle_error(self, request, client_address):  # a client the gate dropped
        pass


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    DockerHost().make_ca(directory, [PROVIDER])
    return directory


@pytest.fixture
def world(tls, tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(gate, "_clock", clock)
    upstream = Upstream(clock)
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
        "World",
        (),
        {"port": httpd.server_port, "upstream": upstream, "log": log, "tls": tls, "clock": clock, "clients": clients},
    )
    upstream.release.set()
    for client in clients:  # no handler is left waiting on a client for the next test's gate to find
        client.close()
    httpd.shutdown()
    httpd.server_close()
    upstream.shutdown()
    upstream.server_close()
    gate.CFG.clear()


def connect(world) -> http.client.HTTPConnection:
    """One keep-alive client connection to the gate over TLS: its requests share the gate's upstream connection."""
    context = ssl.create_default_context(cafile=str(world.tls / "ca.pem"))
    raw = socket.create_connection(("127.0.0.1", world.port), timeout=20)
    conn = http.client.HTTPConnection(PROVIDER, timeout=20)
    conn.sock = context.wrap_socket(raw, server_hostname=PROVIDER)
    world.clients.append(conn)
    return conn


def get(conn, path=ATTRS, method="GET") -> int:
    conn.request(method, path)
    answer = conn.getresponse()
    answer.read()
    return answer.status


def lines_of(world) -> list[dict]:
    return [json.loads(line) for line in world.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def requests_of(world, count: int) -> list[dict]:
    """The log's request lines, once `count` of them are there (the gate writes a line after it answers)."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        found = [r for r in lines_of(world) if "method" in r]
        if len(found) >= count:
            return found
        time.sleep(0.05)
    raise AssertionError(f"the gate logged fewer than {count} requests")


def summary_of(world) -> dict:
    """What the facts would say of the log as it stands, were the gate stopped whole."""
    return gatelog.request_durations([json.dumps(r) for r in lines_of(world)] + [DONE], STOPPED)


def test_o73_a_slow_answer_on_a_reused_connection_is_logged_with_its_duration_and_the_connection(world):
    """O73: the request that waited 31 s on a kept upstream connection says so on its line: its duration, `reused`,
    the wait for the first byte and how the wait ended; the first request, on a new connection, took no time."""
    world.upstream.plan = [("answer", 0), ("answer", 31)]
    conn = connect(world)
    assert get(conn) == 200
    assert get(conn) == 200
    first, second = requests_of(world, 2)
    assert (first["upstream_conn"], first["duration_ms"]) == ("new", 0)
    assert second["upstream_conn"] == "reused"
    assert second["duration_ms"] == 31_000
    assert second["upstream_wait_ms"] == 31_000
    assert second["upstream_wait_conn"] == "reused"
    assert second["upstream_wait_end"] == "first_byte"


def test_o73_the_summary_counts_a_slow_reused_request_in_the_bands_it_passed_and_a_fast_new_one_in_none(world):
    """O73: the log's two lines, summed as the facts sum them: 31 s is over 10 s and over 30 s but not over 60 s, on
    the reused connection; the fast request on the new connection is over none; the maximum is the slow one's."""
    world.upstream.plan = [("answer", 0), ("answer", 31)]
    conn = connect(world)
    get(conn)
    get(conn)
    requests_of(world, 2)
    assert summary_of(world) == {
        "version": 1,
        "requests": 2,
        "by_connection": {
            "new": {**NO_BAND, "requests": 1},
            "reused": {"requests": 1, "over_10s": 1, "over_30s": 1, "over_60s": 0},
        },
        "max_duration_ms": 31_000,
        "unknown": [],
    }


def test_o73_a_fast_request_on_a_new_connection_counts_under_ten_seconds(world):
    """O73: one quick request on a new connection is one request, in no band, and the maximum is its own duration."""
    conn = connect(world)
    get(conn)
    requests_of(world, 1)
    found = summary_of(world)
    assert found["by_connection"]["new"] == {**NO_BAND, "requests": 1}
    assert found["by_connection"]["reused"] == NO_BAND
    assert found["max_duration_ms"] == 0


def test_o73_a_request_that_hung_on_a_reused_connection_and_was_retried_is_filed_under_the_reused_one(world):
    """O73: the kept connection hangs 61 s and is dropped with no answer, the gate retries on a new one that answers in
    2 s. `upstream_conn` still names the connection that answered (`new`), but the wait that was long, its connection
    and its end are on the line, and the summary counts the request as a reused one over 60 s: the stall is not filed
    where it did not happen."""
    world.upstream.plan = [("answer", 0), ("drop", 61), ("answer", 2)]
    conn = connect(world)
    get(conn)
    assert get(conn) == 200
    _, line = requests_of(world, 2)
    assert (line["upstream_conn"], line["upstream_retries"]) == ("new", 1)
    assert line["duration_ms"] == 63_000
    assert (line["upstream_wait_ms"], line["upstream_wait_conn"], line["upstream_wait_end"]) == (
        61_000,
        "reused",
        "failure",
    )
    assert summary_of(world)["by_connection"] == {
        "new": {**NO_BAND, "requests": 1},
        "reused": {"requests": 1, "over_10s": 1, "over_30s": 1, "over_60s": 1},
    }


def test_o73_a_request_that_failed_logs_its_duration_until_the_failure(world):
    """O73: both attempts end in a dropped connection after 7 s: the agent gets a 502 and the line holds 14 s, the
    longest wait (7 s, on a new connection, ended in failure) and no first-byte time of an answer that never came."""
    world.upstream.plan = [("drop", 7), ("drop", 7)]
    conn = connect(world)
    assert get(conn) == 502
    (line,) = requests_of(world, 1)
    assert (line["status"], line["error_origin"], line["upstream_retries"]) == (502, "upstream", 1)
    assert line["duration_ms"] == 14_000
    assert (line["upstream_wait_ms"], line["upstream_wait_conn"], line["upstream_wait_end"]) == (
        7_000,
        "new",
        "failure",
    )
    assert summary_of(world)["by_connection"]["new"] == {"requests": 1, "over_10s": 1, "over_30s": 0, "over_60s": 0}


def test_o73_the_duration_runs_to_the_last_byte_relayed_and_the_wait_to_the_first(world):
    """O73: a file's content whose head comes at once and whose body ends 40 s later: the wait for the first byte is 0
    and the duration 40 s (the client reads the head, the clock moves, and the rest is released)."""
    world.upstream.plan = [("stream", 0)]
    conn = connect(world)
    conn.request("GET", CONTENT)
    answer = conn.getresponse()  # the gate has the upstream's head, and has relayed it
    world.clock.advance(40)
    world.upstream.release.set()
    assert answer.read() == b"a" * 10 + b"b" * 10
    (line,) = requests_of(world, 1)
    assert line["duration_ms"] == 40_000
    assert (line["upstream_wait_ms"], line["upstream_wait_end"]) == (0, "first_byte")


def test_o73_the_new_fields_hold_integers_and_fixed_words_only(world):
    """O73: nothing of a body or of a header is on the timing: whole numbers of milliseconds and three fixed words."""
    world.upstream.plan = [("answer", 0), ("answer", 2)]
    conn = connect(world)
    get(conn)
    get(conn)
    for line in requests_of(world, 2):
        assert type(line["duration_ms"]) is int
        assert type(line["upstream_wait_ms"]) is int
        assert line["upstream_conn"] in ("new", "reused")
        assert line["upstream_wait_conn"] in ("new", "reused")
        assert line["upstream_wait_end"] in ("first_byte", "failure")


def test_o73_an_answer_of_the_gates_own_carries_no_timing_and_is_no_relayed_request(world):
    """O73: a request the gate refuses (a method it does not allow) never reached an upstream: its line has none of the
    timing, and the summary does not count it."""
    conn = connect(world)
    assert get(conn, method="DELETE") == 403
    (line,) = requests_of(world, 1)
    assert line["decision"] == "deny"
    assert not [key for key in line if key == "duration_ms" or key.startswith("upstream_")]
    assert summary_of(world)["requests"] == 0


def test_o73_a_request_still_being_answered_when_the_gate_stops_has_its_age_on_the_teardown_line(world):
    """O73: the answer that has not come when the gate stops writes no request line, so the teardown line of its
    connection says how long it had waited (`request_age_ms`), which keeps the stall's evidence."""
    world.upstream.plan = [("hold", 0)]
    before = set(gate._LIVE)  # the gate's connections are the process's: only this test's own are looked at
    conn = connect(world)
    conn.request("GET", ATTRS)  # sent, and never read: the answer is held
    assert world.upstream.reached.wait(15)
    ours = set(gate._LIVE) - before
    assert len(ours) == 1
    world.clock.advance(90)
    gate._teardown_lines()
    waiting = [
        r
        for r in lines_of(world)
        if r.get("event") == "conn_close" and r.get("conn") in ours and r.get("in_request") is True
    ]
    assert [r["request_age_ms"] for r in waiting] == [90_000]
    world.upstream.release.set()
