"""O25: a content write at or above `[placement] fresh_connection_bytes` goes on a connection of its own (#186).

On the 25.0 development build a 64 MiB or 32 MiB chunk stalled on a reused keep-alive connection and never on a new one
(3 of 289 reused PUTs against 0 of 336 new ones), and 16 MiB never stalled either way. These tests run the real httpx
transport and its pool against a socket of this machine that hands each request to the fake federation, and number the
connections the server accepts, so a request's connection can be told new from reused: a mock transport cannot.
"""

import dataclasses
import hashlib
import itertools
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from expcore_onedata import client as client_module
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import PlacementSettings

LOG = "expcore_onedata.client"
CHUNK = 1024
BODY = bytes((index * 11 + index // 97) % 256 for index in range(3 * CHUNK + 100))  # three full chunks and 100 bytes
TARGET = "ingest/table.bin"


class Bridge(BaseHTTPRequestHandler):
    """A keep-alive server of a real socket that answers each request as the fake federation does, and numbers each
    connection it accepts from 1. A request the fake refuses with a transport failure is answered by closing the
    connection, as a reset is."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.number = next(self.server.numbers)

    def serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, len(body), self.number))
        headers = {name: value for name, value in self.headers.items() if name.lower() != "host"}
        request = httpx.Request(
            self.command, f"https://{self.headers['Host']}{self.path}", headers=headers, content=body
        )
        try:
            answer = self.server.fake.respond(request)
        except httpx.TransportError:
            self.close_connection = True
            return
        bodied = answer.status_code not in (204, 304)
        self.send_response(answer.status_code)
        for name, value in answer.headers.items():
            if name.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                self.send_header(name, value)
        if bodied:
            self.send_header("Content-Length", str(len(answer.content)))
        self.end_headers()
        if bodied:
            self.wfile.write(answer.content)

    do_GET = do_PUT = do_POST = do_DELETE = serve


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fake):
        super().__init__(("127.0.0.1", 0), Bridge)
        self.fake, self.numbers, self.seen = fake, itertools.count(1), []

    def handle_error(self, request, client_address):
        pass


class ToLoopback(httpx.HTTPTransport):
    """The real httpx transport and its connection pool, with every request sent to the server's socket."""

    def __init__(self, port):
        super().__init__()
        self.port = port

    def handle_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return super().handle_request(request)


@pytest.fixture
def server(fake):
    found = Server(fake)
    threading.Thread(target=found.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield found
    found.shutdown()
    found.server_close()


@pytest.fixture
def built(monkeypatch, server):
    """Every HTTP client the layer builds, as `(config, timeout)`: the pool's and each one-shot's, all to the server."""
    made = []

    def build(config, timeout_s):
        made.append((config, timeout_s))
        return httpx.Client(transport=ToLoopback(server.server_address[1]), timeout=timeout_s)

    monkeypatch.setattr(client_module, "_build_http", build)
    return made


def uploader(config, placement, ticker, tmp_path, **settings):
    """A provisioner over a client that builds its own HTTP client, as a real run's does, and the client."""
    client = OnezoneClient(config, timeout_s=10.0)
    return Provisioner(
        client,
        config,
        dataclasses.replace(placement.settings, chunk_bytes=CHUNK, **settings),
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    ), client


def local(tmp_path, content=BODY):
    path = Path(tmp_path) / "local.bin"
    path.write_bytes(content)
    return path


def writes(server):
    """Every content write the server saw, in order, as `(index in server.seen, bytes, connection number)`."""
    return [
        (index, size, number)
        for index, (method, path, size, number) in enumerate(server.seen)
        if method == "PUT" and "/content" in path
    ]


def connection_of(server, index):
    """How the request at `index` met its connection: `new` when no earlier request used it, `alone` when it also
    carries no later one, `reused` otherwise."""
    number = server.seen[index][3]
    earlier = [s for s in server.seen[:index] if s[3] == number]
    others = [s for i, s in enumerate(server.seen) if s[3] == number and i != index]
    return "reused" if earlier else ("alone" if not others else "new")


def test_o25_every_write_at_or_above_the_threshold_goes_on_a_connection_of_its_own_and_a_smaller_one_reuses_the_pool(
    server, built, config, placement, ticker, tmp_path
):
    """O25: against a real socket and the real pool: with the threshold at the chunk size, each of the three full
    chunks is the only request on a connection the server accepted for it, so no connection is reused for a write of
    that size, and the 100-byte remainder, below it, goes on the pool's connection that carried earlier requests. Each
    of the three chunks is built by the layer's own client builder with the pool's configuration, so one-shot
    connections have the TLS settings of the pool's."""
    provisioner, client = uploader(config, placement, ticker, tmp_path, fresh_connection_bytes=CHUNK)
    try:
        result = provisioner.upload(TARGET, local(tmp_path))
    finally:
        client.close()
    assert (result.action, result.sent_bytes) == ("created", len(BODY))
    sent = writes(server)
    assert [size for _, size, _ in sent] == [CHUNK, CHUNK, CHUNK, 100]
    assert [connection_of(server, index) for index, _, _ in sent] == ["alone", "alone", "alone", "reused"]
    assert len({number for _, _, number in sent}) == 4
    assert len({number for *_, number in server.seen}) == 1 + 3  # the pool's one, and one for each full chunk
    assert len(built) == 1 + 3
    assert all(config_built is config and timeout == 10.0 for config_built, timeout in built)


def test_o25_a_write_below_the_threshold_reuses_the_pool_and_opens_no_connection(
    server, built, config, placement, ticker, tmp_path
):
    """O25: the threshold is at or above, not above: with it one byte over the chunk size no write is sent on a new
    connection, every request of the upload shares the pool's single connection, and no one-shot client is built."""
    provisioner, client = uploader(config, placement, ticker, tmp_path, fresh_connection_bytes=CHUNK + 1)
    try:
        result = provisioner.upload(TARGET, local(tmp_path))
    finally:
        client.close()
    assert result.action == "created"
    assert len(writes(server)) == 4
    assert {number for *_, number in server.seen} == {1}
    assert len(built) == 1


def test_o25_the_log_says_for_each_write_whether_its_connection_was_new_and_holds_no_path_id_or_token(
    server, built, config, placement, ticker, tmp_path, fake, caplog
):
    """O25: one line per content write, with its size, the seconds it took and how its connection began (`new`, or
    `reused` with the requests that connection had served before it), so that a live run shows which writes stalled on a
    reused connection; the provider is named by its position, and no line holds a path, a file id or a credential."""
    caplog.set_level(logging.INFO, logger=LOG)
    provisioner, client = uploader(config, placement, ticker, tmp_path, fresh_connection_bytes=CHUNK)
    try:
        result = provisioner.upload(TARGET, local(tmp_path))
    finally:
        client.close()
    said = [r.getMessage() for r in caplog.records if r.name == LOG and "PUT" in r.getMessage()]
    assert len(said) == 4
    assert all(line.startswith("providers[0]: a PUT of ") for line in said)
    assert [line.split(" bytes ")[0].split()[-1] for line in said] == ["1024", "1024", "1024", "100"]
    assert [line.endswith("(connection new)") for line in said] == [True, True, True, False]
    assert "(connection reused, " in said[3]
    text = "\n".join(said)
    for secret in (fake.admin_token, result.file_id, TARGET, "ingest"):
        assert secret not in text


def test_o25_an_upload_cut_by_a_reset_resumes_and_the_resumed_chunks_go_on_connections_of_their_own(
    server, built, config, placement, ticker, tmp_path, fake
):
    """O25: the server drops the connection of the third write, whose chunk was on a connection of its own. The upload
    fails as a transport failure with the first two chunks in the file; run again it finds that prefix, resumes from its
    end, sends the rest, and verifies the whole by its hash, and the chunks it sends are each on a connection of their
    own again."""
    provisioner, client = uploader(config, placement, ticker, tmp_path, fresh_connection_bytes=CHUNK)
    source = local(tmp_path)
    try:
        fake.writes_before_reset = 2
        with pytest.raises(PlatformError) as cut:
            provisioner.upload(TARGET, source)
        assert cut.value.transport
        cut_writes = writes(server)
        assert [size for _, size, _ in cut_writes] == [CHUNK, CHUNK, CHUNK]
        assert [connection_of(server, index) for index, _, _ in cut_writes] == ["alone"] * 3
        fake.writes_before_reset = None
        result = provisioner.upload(TARGET, source)
    finally:
        client.close()
    assert (result.action, result.sent_bytes) == ("resumed", len(BODY) - 2 * CHUNK)
    assert result.digest.sha256 == hashlib.sha256(BODY).hexdigest()
    assert bytes(fake.nodes[result.file_id].content) == BODY
    resumed = writes(server)[3:]
    assert [size for _, size, _ in resumed] == [CHUNK, 100]
    assert [connection_of(server, index) for index, _, _ in resumed] == ["alone", "reused"]
    offsets = [int(r.query["offset"]) for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")]
    assert offsets[-2:] == [2 * CHUNK, 3 * CHUNK]


def test_o25_a_client_given_an_http_client_leaves_its_connections_to_its_caller(
    config, placement, ticker, tmp_path, fake, monkeypatch
):
    """O25: a client built over an HTTP client its caller owns cannot make a new connection of its own, as a retry of a
    read cannot (`readretry.py`): the writes go through the caller's client, no other is built, and the upload is
    whole."""

    def refuse(*args):
        raise AssertionError("a client that does not own its pool built one")

    monkeypatch.setattr(client_module, "_build_http", refuse)
    client = OnezoneClient(config, http=fake.client_http())
    provisioner = Provisioner(
        client,
        config,
        dataclasses.replace(placement.settings, chunk_bytes=CHUNK, fresh_connection_bytes=CHUNK),
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    result = provisioner.upload(TARGET, local(tmp_path))
    assert (result.action, result.sent_bytes) == ("created", len(BODY))
    assert bytes(fake.nodes[result.file_id].content) == BODY


def test_o25_at_the_defaults_a_50_mib_upload_is_three_full_chunks_each_on_its_own_connection_and_a_remainder(
    server, built, config, placement, ticker, tmp_path, caplog
):
    """O25: no setting is changed: the defaults, 16 MiB chunks and a threshold of 16 MiB, send 50 MiB as three chunks
    that are each alone on a connection the client opened for them and a remainder of 2 MiB on the pool's, the log says
    so for each write as the live test at the default reads it, and the file read back has the SHA-256 of the bytes."""
    caplog.set_level(logging.INFO, logger=LOG)
    content = bytes(range(256)) * (50 << 12)
    assert len(content) == 50 << 20
    client = OnezoneClient(config, timeout_s=30.0)
    provisioner = Provisioner(
        client,
        config,
        PlacementSettings(holder=placement.settings.holder, request_timeout_s=60.0),
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    try:
        result = provisioner.upload(TARGET, local(tmp_path, content))
        back = provisioner.read_file(result.file_id)
    finally:
        client.close()
    assert (result.action, result.sent_bytes) == ("created", 50 << 20)
    assert result.digest.sha256 == back.sha256 == hashlib.sha256(content).hexdigest()
    sent = writes(server)
    assert [size for _, size, _ in sent] == [16 << 20] * 3 + [2 << 20]
    assert [connection_of(server, index) for index, _, _ in sent] == ["alone", "alone", "alone", "reused"]
    said = [r.getMessage() for r in caplog.records if r.name == LOG and "PUT" in r.getMessage()]
    assert [line.endswith("(connection new)") for line in said] == [True, True, True, False]
