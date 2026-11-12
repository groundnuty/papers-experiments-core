"""O29: the client retries a GET that timed out, boundedly and in the open, and nothing else (#185).

Three attempts, pauses of 2 s and 5 s, each retry logged and counted; a PUT, a POST, an error answer, a connection
error and a streamed read are never retried; the total stays within a call's own deadline. Each test plays a script on a
mock transport whose timeouts move a ticker by the seconds the request was allowed, so the measured seconds are exact
and nothing really waits.
"""

import inspect
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from platform_support import leaked_values

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.readretry import (
    ONE_ATTEMPT,
    PLUMBING_ATTEMPTS,
    PLUMBING_PAUSES_S,
    ReadRetry,
    connection_state,
)
from expcore_onedata.testing import SPACE_PREFIX, Ticker, write_platform

LOG = "expcore_onedata.readretry"
PHASE = {httpx.ReadTimeout: "read", httpx.ConnectTimeout: "connect", httpx.PoolTimeout: "pool"}


class Script:
    """A transport handler that plays steps in order, then answers 200: a step is an exception class (raised with the
    ticker moved by the timeout the request carried, as a real wait would) or a status."""

    def __init__(self, ticker, *steps):
        self.ticker, self.steps, self.seen = ticker, list(steps), []

    def __call__(self, request):
        self.seen.append(request)
        step = self.steps.pop(0) if self.steps else 200
        if isinstance(step, type) and issubclass(step, Exception):
            if step in PHASE:
                self.ticker.now += request.extensions["timeout"][PHASE[step]]
            raise step("simulated", request=request)
        return httpx.Response(step, json={"size": 7} if step < 400 else {"error": {"id": "internalServerError"}})


@pytest.fixture
def ticker():
    return Ticker()


@pytest.fixture
def policy(ticker):
    return ReadRetry(clock=ticker.clock, sleep=ticker.sleep)


def scripted(config, ticker, *steps, timeout_s=6.0):
    """A client over a script, and the script, whose `seen` lists the requests that left."""
    script = Script(ticker, *steps)
    http = httpx.Client(transport=httpx.MockTransport(script))
    return OnezoneClient(config, http=http, timeout_s=timeout_s), script


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOG]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.INFO, logger=LOG)


def test_o29_the_plumbing_policy_is_three_attempts_with_pauses_of_two_and_five_seconds():
    """O29: the rule the study asked for: 3 attempts in all, 2 s and 5 s between them."""
    assert (PLUMBING_ATTEMPTS, PLUMBING_PAUSES_S) == (3, (2.0, 5.0))
    default = ReadRetry()
    assert (default.attempts, default.pauses_s, default.retries) == (3, (2.0, 5.0), 0)
    assert (ONE_ATTEMPT.attempts, ONE_ATTEMPT.pauses_s) == (1, ())


@pytest.mark.parametrize(("attempts", "pauses"), [(0, ()), (3, (1.0,)), (2, (1.0, 2.0)), (2, (-1.0,)), (True, ())])
def test_o29_a_policy_needs_one_pause_between_each_two_attempts(attempts, pauses):
    """O29: a policy that cannot say how long to wait, or that waits a negative time, is refused when it is built."""
    with pytest.raises(ValueError, match=r"tried at least once|one pause"):
        ReadRetry(attempts, pauses)


def test_o29_a_timeout_then_an_answer_succeeds_with_one_retry_logged_and_counted(config, fake, ticker, policy, caplog):
    """O29: the read hangs past its timeout once and then answers: the caller gets the answer; one retry is made after
    the 2 s pause, counted, and logged with the timeout measured, the connection and the next attempt's outcome."""
    client, script = scripted(config, ticker, httpx.ReadTimeout)
    assert client.get("/spaces", retry=policy) == {"size": 7}
    assert len(script.seen) == 2
    assert ticker.slept == [2.0]
    assert policy.retries == 1
    assert lines(caplog) == [
        "zone: a GET timed out after 6.00 s on attempt 1 of 3 (connection unknown); retrying in 2 s",
        "zone: a GET was answered with 200 on attempt 2 of 3 after 0.00 s (connection unknown)",
    ]
    assert [r.levelno for r in caplog.records if r.name == LOG] == [logging.WARNING, logging.INFO]


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_o29_a_read_connect_or_pool_timeout_is_retried(timeout, config, fake, ticker, policy):
    """O29: all three of the timeouts the rule names are retried."""
    client, script = scripted(config, ticker, timeout)
    assert client.get("/spaces", retry=policy) == {"size": 7}
    assert (len(script.seen), policy.retries) == (2, 1)


def test_o29_three_timeouts_fail_with_all_three_logged_and_the_pauses_taken(config, fake, ticker, policy, caplog):
    """O29: every attempt timed out: the call fails as a transport failure that says how many were made, three
    requests left, both pauses were taken, two retries counted, and all three timeouts are in the log."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout, httpx.ReadTimeout)
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", retry=policy)
    assert caught.value.transport is True
    assert caught.value.status is None
    assert str(caught.value) == "zone: GET failed (ReadTimeout) after 3 attempts"
    assert len(script.seen) == 3
    assert ticker.slept == [2.0, 5.0]
    assert policy.retries == 2
    assert lines(caplog) == [
        "zone: a GET timed out after 6.00 s on attempt 1 of 3 (connection unknown); retrying in 2 s",
        "zone: a GET timed out after 6.00 s on attempt 2 of 3 (connection unknown); retrying in 5 s",
        "zone: a GET timed out after 6.00 s on attempt 3 of 3 (connection unknown); giving up",
    ]


def test_o29_a_provider_is_named_by_its_position_in_the_log(config, fake, ticker, policy, caplog):
    """O29: a retried read of a provider logs `providers[i]`, as every message does (O12)."""
    client, _ = scripted(config, ticker, httpx.ReadTimeout)
    client.get("/spaces", provider="beta", retry=policy)
    assert all(line.startswith("providers[1]: ") for line in lines(caplog))


def test_o29_a_log_line_holds_no_path_id_token_or_configured_value(config, fake, ticker, policy, caplog, tmp_path):
    """O29: the lines name a label, an attempt and seconds; the path of the read (it holds ids), a token and the
    configured names never appear."""
    client, _ = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout)
    client.get("/data/f1d-secret-id/children", provider="alpha", token="a-user-token", retry=policy)
    text = "\n".join(lines(caplog))
    assert text
    for forbidden in ("f1d-secret-id", "a-user-token", fake.admin_token, "/data", "children"):
        assert forbidden not in text
    assert leaked_values(text, tmp_path) == []


def test_o29_without_a_policy_a_timeout_is_one_attempt(config, fake, ticker, caplog):
    """O29: a call that names no policy, or `ONE_ATTEMPT`, sends once and fails as it always did, logging nothing."""
    for kwargs in ({}, {"retry": ONE_ATTEMPT}):
        client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout)
        with pytest.raises(PlatformError, match=r"^zone: GET failed \(ReadTimeout\)$"):
            client.get("/spaces", **kwargs)
        assert len(script.seen) == 1
    assert ticker.slept == []
    assert lines(caplog) == []
    assert ONE_ATTEMPT.retries == 0


@pytest.mark.parametrize(
    "step",
    [
        httpx.ConnectError,
        httpx.ReadError,
        httpx.RemoteProtocolError,
        httpx.WriteTimeout,
        404,
        400,
        500,
        503,
    ],
)
def test_o29_an_error_answer_a_connection_error_and_a_write_timeout_are_never_retried(
    step, config, fake, ticker, policy
):
    """O29: any status, a refused or reset connection (a provider that is down must look down) and a write timeout
    are one request, no pause and no count, even under the plumbing policy."""
    client, script = scripted(config, ticker, step, step, step)
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", retry=policy)
    assert len(script.seen) == 1
    assert ticker.slept == []
    assert policy.retries == 0
    assert caught.value.transport is isinstance(step, type)
    assert "attempts" not in str(caught.value)


def test_o29_a_connection_error_after_a_timeout_ends_the_retries_and_is_logged(config, fake, ticker, policy, caplog):
    """O29: the retry that meets a refused connection stops there (the provider is down) and says so; the error is the
    connection error's."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ConnectError)
    with pytest.raises(PlatformError, match=r"^zone: GET failed \(ConnectError\)$"):
        client.get("/spaces", retry=policy)
    assert len(script.seen) == 2
    assert lines(caplog)[-1] == "zone: a GET failed (ConnectError) on attempt 2 of 3 after 0.00 s (connection unknown)"


def test_o29_an_error_answer_after_a_timeout_is_the_answer_and_ends_the_retries(config, fake, ticker, policy, caplog):
    """O29: a retry that is answered with an error status is an answer: it raises as one, with its status."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, 503)
    with pytest.raises(PlatformError) as caught:
        client.get("/spaces", retry=policy)
    assert (caught.value.status, caught.value.transport) == (503, False)
    assert len(script.seen) == 2
    assert lines(caplog)[-1].startswith("zone: a GET was answered with 503 on attempt 2 of 3")


def test_o29_a_put_a_post_a_patch_and_a_delete_are_never_retried(fake, config, ticker, monkeypatch):
    """O29: a mutation that times out is sent once, whatever the policy of the reads around it; the client's `mutate`
    takes no policy, and so a mutation cannot be handed one."""
    fake.other_spaces["sp-run-1"] = SPACE_PREFIX + "run-1"
    sent = []

    def respond(request):
        if request.method == "GET":
            return fake.respond(request)
        sent.append(request.method)
        ticker.now += request.extensions["timeout"]["read"]
        raise httpx.ReadTimeout("simulated", request=request)

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(respond)))
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        with pytest.raises(PlatformError, match=rf"^zone: {method} failed \(ReadTimeout\)$"):
            client.mutate(method, "/spaces/sp-run-1/x", json={"a": 1})
    assert sent == ["PUT", "POST", "PATCH", "DELETE"]
    assert ticker.slept == []


def test_o29_a_reading_post_is_never_retried(config, fake, ticker):
    """O29: `query` is a POST that only reads (examining a token, inferring its scope), and a POST is never retried."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout)
    with pytest.raises(PlatformError, match=r"^zone: POST failed \(ReadTimeout\)$"):
        client.query("/tokens/examine", {"token": "t"})
    assert [r.method for r in script.seen] == ["POST"]
    assert ticker.slept == []


def test_o29_only_get_can_be_handed_a_policy():
    """O29: by construction: `get` takes `retry`, and no other call of the client does, so a PUT, a POST (reading POSTs
    included), a PATCH, a DELETE, a range read, a streamed read, the head of a content read and a dated read cannot be
    retried by a caller that is later added."""
    public = {name: f for name, f in inspect.getmembers(OnezoneClient, inspect.isfunction) if not name.startswith("_")}
    takes = sorted(name for name, f in public.items() if "retry" in inspect.signature(f).parameters)
    assert takes == ["get"]


def test_o29_the_deadline_holds_a_retry_that_would_pass_it_is_not_made(config, fake, ticker, policy, caplog):
    """O29: a call with `deadline_s=10` whose attempts each wait the 6 s timeout: the first ends at 6 s, the 2 s pause
    leaves 2 s, so the retry runs with its timeout cut to 2 s, ends at 10 s, and the pause of 5 s that would follow
    does not fit: the call stops at 10 s with two attempts, never at 6 + 2 + 6 + 5 + 6."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout, httpx.ReadTimeout)
    began = ticker.now
    with pytest.raises(PlatformError, match="after 2 attempts"):
        client.get("/spaces", retry=policy, deadline_s=10.0)
    assert ticker.now - began == 10.0
    assert [r.extensions["timeout"]["read"] for r in script.seen] == [6.0, 2.0]
    assert ticker.slept == [2.0]
    assert policy.retries == 1
    assert lines(caplog)[-1].endswith("giving up")


def test_o29_a_deadline_with_no_room_for_a_pause_and_a_second_makes_no_retry(config, fake, ticker, policy):
    """O29: after the first attempt 0.5 s of the deadline remains: no retry, no pause, the timeout is the call's own."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, 200)
    with pytest.raises(PlatformError, match=r"^zone: GET failed \(ReadTimeout\)$"):
        client.get("/spaces", retry=policy, deadline_s=6.5)
    assert len(script.seen) == 1
    assert ticker.slept == []
    assert policy.retries == 0


def test_o29_a_deadline_already_spent_allows_the_first_attempt_and_no_retry(config, fake, ticker, policy):
    """O29: a deadline of zero or less (what a bounded wait that has run out passes) never skips the first read and
    never retries it."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, 200)
    with pytest.raises(PlatformError):
        client.get("/spaces", retry=policy, deadline_s=-3.0)
    assert len(script.seen) == 1
    assert ticker.slept == []


def test_o29_a_deadline_that_leaves_room_lets_the_first_retry_through_at_the_full_timeout(config, fake, ticker, policy):
    """O29: a roomy deadline does not change the timeouts: the retry is as long as the first attempt."""
    client, script = scripted(config, ticker, httpx.ReadTimeout, httpx.ReadTimeout, 200)
    assert client.get("/spaces", retry=policy, deadline_s=100.0) == {"size": 7}
    assert [r.extensions["timeout"]["read"] for r in script.seen] == [6.0, 6.0, 6.0]
    assert ticker.slept == [2.0, 5.0]


def test_o29_how_a_connection_began_is_read_from_httpcores_trace_events():
    """O29: a connect event is a new connection; a request sent with none is a pooled one; no event is unknown."""
    new = ["connection.connect_tcp.started", "connection.connect_tcp.complete", "http11.send_request_headers.started"]
    assert connection_state(new) == "new"
    assert connection_state(["connection.start_tls.started"]) == "new"
    assert (
        connection_state(["http11.send_request_headers.started", "http11.receive_response_headers.started"]) == "reused"
    )
    assert connection_state(["http2.send_request_headers.started"]) == "reused"
    assert connection_state([]) == "unknown"
    assert connection_state(["connection.close.started"]) == "unknown"


class Hanging(BaseHTTPRequestHandler):
    """A keep-alive server of a real socket. `/ok` answers. `/pair` answers once two requests are in, so that two
    connections are open at once. `/hang` hangs the first time it is asked, then answers."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/pair"):
            self.server.pair.wait(timeout=5)
        if self.path.endswith("/hang") and not self.server.hung:
            self.server.hung = True
            self.server.release.wait(timeout=5)
            return
        body = b'{"size": 7}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Counting(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        super().__init__(*args)
        self.hung, self.release, self.connections = False, threading.Event(), 0
        self.pair = threading.Barrier(2)

    def get_request(self):
        found = super().get_request()
        self.connections += 1
        return found

    def handle_error(self, request, client_address):
        pass


class ToLoopback(httpx.HTTPTransport):
    """The real httpx transport and its connection pool, with every request sent to a socket of this machine."""

    def __init__(self, port):
        super().__init__()
        self.port = port

    def handle_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return super().handle_request(request)


@pytest.fixture
def server():
    """A server on a socket of this machine, closed with its threads when the test ends."""
    found = Counting(("127.0.0.1", 0), Hanging)
    threading.Thread(target=found.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield found
    found.release.set()
    found.pair.abort()
    found.shutdown()
    found.server_close()


@pytest.fixture
def own(server, tmp_path, fake):
    """A client that builds its own HTTP client, as a real run's does, whose zone is the server's socket."""

    def point_at_server(data):
        data["zone"]["url"] = f"http://127.0.0.1:{server.server_address[1]}"

    (tmp_path / "loopback").mkdir()
    client = OnezoneClient(load_platform(write_platform(tmp_path / "loopback", point_at_server)), timeout_s=10.0)
    yield client
    client.close()


def pooled(client):
    """The connections of the pool of the client's own HTTP client, as httpcore lists them."""
    return client._http._transport._pool.connections


def release_on_sleep(server):
    """A policy of two attempts with no pause, whose sleep frees the request the server hung (a test's stand-in for
    waiting for the stalled request to be given up)."""
    return ReadRetry(2, (0.0,), sleep=lambda seconds: server.release.set())


def test_o29_a_retry_runs_on_a_fresh_connection_not_the_one_that_hung(server, config, fake, caplog):
    """O29: against a real socket and the real pool: a connection that carried an answer is reused, then hangs past the
    read timeout; the retry opens another, and the server has accepted exactly two. The log says `reused` and how many
    requests the stalled connection had served for the attempt that hung, and `new` for the one that was answered."""
    http = httpx.Client(transport=ToLoopback(server.server_address[1]))
    try:
        client = OnezoneClient(config, http=http)
        policy = release_on_sleep(server)
        assert client.get("/ok", timeout_s=10.0) == {"size": 7}
        assert server.connections == 1
        assert client.get("/hang", retry=policy, timeout_s=0.4) == {"size": 7}
        assert server.connections == 2
    finally:
        http.close()
    assert policy.retries == 1
    first, second = lines(caplog)
    assert first.endswith("on attempt 1 of 2 (connection reused, 1 earlier request served); retrying in 0 s")
    assert second.startswith("zone: a GET was answered with 200 on attempt 2 of 2 after ")
    assert second.endswith("(connection new)")


def test_o29_a_connection_that_timed_out_is_closed_and_not_returned_to_the_pool(server, config, fake):
    """O29: httpcore closes the connection whose request timed out, and the pool drops it, so the stalled connection
    cannot be handed out again: the pool is empty after the timeout, and the next read opens a new connection."""
    http = httpx.Client(transport=ToLoopback(server.server_address[1]))
    try:
        client = OnezoneClient(config, http=http)
        assert client.get("/ok", timeout_s=10.0) == {"size": 7}
        assert [c.info().endswith("IDLE, Request Count: 1") for c in pooled(client)] == [True]
        with pytest.raises(PlatformError, match=r"^zone: GET failed \(ReadTimeout\)$"):
            client.get("/hang", timeout_s=0.4)
        assert pooled(client) == []
        assert client.get("/ok", timeout_s=10.0) == {"size": 7}
        assert server.connections == 2
    finally:
        http.close()


def test_o29_a_client_that_owns_its_pool_sends_the_retry_on_a_connection_of_its_own(server, own, caplog):
    """O29: the retry of a real run's client goes out through a one-shot client, so its connection is new and is closed
    when the answer is in: after the retry the pool holds nothing (the stalled connection is gone, the retry's was never
    pooled), the server has accepted two, and the next read opens a third."""
    policy = release_on_sleep(server)
    assert own.get("/ok") == {"size": 7}
    assert own.get("/hang", retry=policy, timeout_s=0.4) == {"size": 7}
    assert server.connections == 2
    assert pooled(own) == []
    first, second = lines(caplog)
    assert first.endswith("(connection reused, 1 earlier request served); retrying in 0 s")
    assert second.endswith("(connection new)")
    assert own.get("/ok") == {"size": 7}
    assert server.connections == 3


def test_o29_a_retry_is_never_given_another_idle_connection_of_the_pool(server, own, caplog):
    """O29: with two idle connections in the pool, the read that stalls on one is retried on a new connection, not on
    the other idle one, which could be as stale: the server has accepted three, and one idle connection is left."""
    policy = release_on_sleep(server)
    pair = [threading.Thread(target=own.get, args=("/pair",)) for _ in range(2)]
    for thread in pair:
        thread.start()
    for thread in pair:
        thread.join(timeout=10)
    assert server.connections == 2
    assert len(pooled(own)) == 2
    assert own.get("/hang", retry=policy, timeout_s=0.4) == {"size": 7}
    assert server.connections == 3
    assert len(pooled(own)) == 1
    first, second = lines(caplog)
    assert "(connection reused, 1 earlier request served)" in first
    assert second.endswith("(connection new)")


def test_o29_the_requests_a_stalled_connection_had_served_are_counted_when_the_pool_can_be_seen(server, own, caplog):
    """O29: three reads answered on one connection, then a stall on it: the line says it had served three before."""
    policy = release_on_sleep(server)
    for _ in range(3):
        assert own.get("/ok") == {"size": 7}
    assert own.get("/hang", retry=policy, timeout_s=0.4) == {"size": 7}
    assert "(connection reused, 3 earlier requests served)" in lines(caplog)[0]
    assert server.connections == 2
