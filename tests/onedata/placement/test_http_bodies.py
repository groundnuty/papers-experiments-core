"""A large HTTP body is freed as soon as its exchange is used, never at the next full collection (O47, #259).

httpx binds every Response to a stream that points back at it, a reference cycle that holds the answer, its request and
both bodies until the cyclic collector runs. A batch of large uploads or reads then grows by the size of everything it
moved, and a host short of memory fails at whatever moment the collector happens to wait for. Each test runs with the
collector off, and with its work done asks whether the exchanges and their bytes are gone, without a collection."""

import contextlib
import dataclasses
import gc
import hashlib
import http.server
import sys
import threading
import tracemalloc
import weakref

import httpx
import pytest

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.readretry import ReadRetry
from expcore_onedata.testing import BODY_KEPT_BYTES, Request

MIB = 1 << 20
FILES, SIZE = 8, 4 * MIB  # modest: the host is shared, and eight files of four MiB hold 32 MiB in the fake
SLACK = MIB  # what may be held beyond the fake's own storage once the work is done: bookkeeping, never a body


@contextlib.contextmanager
def collector_off():
    """Run with the cyclic collector off, and put it back as it was however the block ends."""
    was_on = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_on:
            gc.enable()


def watched_client(config, fake, seen, **options):
    """A client whose federation is the fake, and `seen` a weak reference to every request and every answer it makes."""

    def handler(request):
        response = fake.respond(request)
        seen.extend((weakref.ref(request), weakref.ref(response)))
        return response

    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler)), **options)


@pytest.fixture
def seen():
    return []


@pytest.fixture
def big(placement):
    """The placement's settings with chunks of a MiB, so that a file of four is four writes and four reads."""
    return dataclasses.replace(placement.settings, chunk_bytes=MIB)


def provisioner_over(client, config, settings, ticker, tmp_path) -> Provisioner:
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


@pytest.fixture
def watched(config, fake, seen, big, ticker, tmp_path) -> Provisioner:
    return provisioner_over(watched_client(config, fake, seen), config, big, ticker, tmp_path)


def local_files(tmp_path):
    """FILES local files of SIZE bytes, each written and let go, so that the test holds none of them."""
    paths = []
    for index in range(FILES):
        path = tmp_path / "local" / f"part-{index}.bin"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(bytes(SIZE))
        paths.append(path)
    return paths


def seed(fake):
    """FILES files of SIZE bytes in the space, which the fake holds; return their ids."""
    return [fake.add_file(f"study/part-{index}.bin", bytes(SIZE)) for index in range(FILES)]


def stored(fake):
    """The bytes the fake holds as files: the platform's own storage, which no client of it can free."""
    return sum(sys.getsizeof(node.content) for node in fake.nodes.values())


def alive(seen):
    return sum(1 for ref in seen if ref() is not None)


def measured(fake, seen, work):
    """Run `work` with the collector off; return the bytes it left allocated beyond what the fake stores, and how many
    of the requests and answers it made are still alive, both taken before the collector can run."""
    gc.collect()
    tracemalloc.start()
    try:
        before, kept = tracemalloc.get_traced_memory()[0], stored(fake)
        with collector_off():
            work()
            return tracemalloc.get_traced_memory()[0] - before - (stored(fake) - kept), alive(seen)
    finally:
        tracemalloc.stop()


# -- the exchanges a batch makes ------------------------------------------------------------------------------------


def test_o47_an_upload_leaves_no_request_or_answer_alive_without_a_collection(watched, seen, tmp_path):
    """O47: eight files of four MiB go in, chunk by chunk, and are read back to be checked. With the collector off,
    every request and every answer of the batch, the writes' and the read-back's, is gone once the batch is."""
    paths = local_files(tmp_path)
    with collector_off():
        for index, path in enumerate(paths):
            assert watched.upload(f"ingest/part-{index}.bin", path).action == "created"
        assert seen
        assert alive(seen) == 0


def test_o47_an_upload_holds_no_body_beyond_what_the_fake_stores(watched, fake, seen, tmp_path):
    """O47: the same batch measured in bytes: nothing of the 32 MiB sent and the 32 MiB read back is still allocated,
    once the fake's own copy of the files is set aside. The fake's log of requests counts: it kept every body."""
    paths = local_files(tmp_path)

    def upload():
        for index, path in enumerate(paths):
            watched.upload(f"ingest/part-{index}.bin", path)

    held, lingering = measured(fake, seen, upload)
    assert lingering == 0
    assert held < SLACK


def test_o47_a_streamed_read_leaves_no_answer_alive_and_holds_no_body(watched, fake, seen):
    """O47: a streamed read of eight files of four MiB, a MiB a range, hashes what it reads and keeps none of it: every
    answer is gone and no byte of the files is allocated beyond the fake's own."""
    ids = seed(fake)

    def read():
        for file_id in ids:
            assert watched.read_file(file_id).size == SIZE

    held, lingering = measured(fake, seen, read)
    assert lingering == 0
    assert held < SLACK


def test_o47_a_ranged_read_leaves_no_answer_alive_and_holds_no_body_once_its_bytes_are_dropped(watched, fake, seen):
    """O47: a range read hands its bytes to the caller, who drops them; the answer that carried them does not keep
    them. Eight reads of four MiB leave no answer alive and no byte allocated."""
    ids = seed(fake)

    def read():
        for file_id in ids:
            assert len(watched.fetch("alpha", file_id, 0, SIZE)) == SIZE

    held, lingering = measured(fake, seen, read)
    assert lingering == 0
    assert held < SLACK


def test_o47_the_head_of_a_whole_content_answer_keeps_no_body(config, fake, seen):
    """O47: `content_answer` closes an answer once its head is in, so a large file costs nothing: the answer, which
    the fake made whole, is gone and its four MiB with it."""
    file_id = fake.add_file("study/part.bin", bytes(SIZE))
    client = watched_client(config, fake, seen)

    def head():
        for _ in range(FILES):
            assert client.content_answer(f"/data/{file_id}/content", provider="alpha", first_byte=False)[0] == 200

    held, lingering = measured(fake, seen, head)
    assert lingering == 0
    assert held < SLACK


def test_o47_a_failed_write_carries_no_httpx_error_nor_its_request(watched, fake):
    """O47: a write of a large body that failed in transit raises a PlatformError that holds no httpx error, so nothing
    the error carries leads to the request, its body or the connection's answer."""
    fake.writes_before_reset = 0
    with pytest.raises(PlatformError) as failed:
        watched.put_file("ingest/lost.bin", bytes(SIZE))
    assert failed.value.transport
    assert failed.value.__cause__ is None
    assert failed.value.__context__ is None


def test_o47_an_error_answer_to_a_large_write_carries_no_httpx_error(watched, fake):
    """O47: nor does an error answer's PlatformError hold the answer or the error that carried it."""
    fake.statuses.append(["/content", 1, 500])
    with pytest.raises(PlatformError) as refused:
        watched.put_file("ingest/refused.bin", bytes(SIZE))
    assert refused.value.status == 500
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


def test_o47_a_transport_failure_of_a_head_or_a_streamed_read_has_no_httpx_error_as_context(config, fake, seen):
    """O47: the same for the two streamed paths: the error of a content answer and of a streamed range that failed in
    transit is raised with its handler over, so the httpx error and the request it holds are no part of it."""
    file_id = fake.add_file("study/part.bin", bytes(SIZE))
    client = watched_client(config, fake, seen)
    fake.resets.append(["/content", 2])
    with pytest.raises(PlatformError) as head:
        client.content_answer(f"/data/{file_id}/content", provider="alpha", first_byte=True)
    with pytest.raises(PlatformError) as streamed:
        client.read_content(f"/data/{file_id}/content", provider="alpha", size=SIZE, chunk_bytes=MIB, deadline_s=60)
    for failure in (head.value, streamed.value):
        assert failure.transport
        assert (failure.__cause__, failure.__context__) == (None, None)


def test_o47_a_read_that_timed_out_on_every_attempt_has_no_httpx_error_as_context(config):
    """O47: nor has the PlatformError of a retried read that ran out of attempts, which held the last timeout, with its
    request, in `Exhausted`."""

    def timed_out(request):
        raise httpx.ReadTimeout("no answer", request=request)

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(timed_out)))
    with pytest.raises(PlatformError, match="after 3 attempts") as failed:
        client.get("/spaces/s", retry=ReadRetry(sleep=lambda seconds: None))
    assert (failed.value.__cause__, failed.value.__context__) == (None, None)


def test_o47_an_answer_that_is_not_json_has_no_decoder_error_holding_its_text_as_context(config):
    """O47: the decoder's error holds the whole text it could not read, so the PlatformError of a four MiB answer that
    is no JSON is raised with the decoder's handler over, and holds none of that text through its context."""
    client = OnezoneClient(
        config,
        http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * SIZE))),
    )
    with pytest.raises(PlatformError, match="not JSON") as failed:
        client.get("/spaces/s")
    assert (failed.value.__cause__, failed.value.__context__) == (None, None)


# -- what the fakes keep ---------------------------------------------------------------------------------------------


def test_o47_the_fakes_log_keeps_the_size_and_hash_of_a_large_body_and_a_small_one_whole(watched, fake, tmp_path):
    """O47: the fakes' log of requests holds every request for a test to assert on, and held every body as well: eight
    files of four MiB were 32 MiB that no collection could free. A body over the cap is recorded by its size and
    SHA-256; a small one, a JSON body or a short write, is kept whole as ever."""
    path = tmp_path / "part.bin"
    path.write_bytes(bytes(range(256)) * (SIZE // 256))
    watched.upload("ingest/part.bin", path)
    writes = [r for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")]
    assert [r.size for r in writes] == [MIB] * 4
    assert {r.sha256 for r in writes} == {hashlib.sha256(bytes(range(256)) * (MIB // 256)).hexdigest()}
    assert all(r.body == b"" for r in writes)
    watched.put_file("ingest/small.bin", b"abc")
    small = [r for r in fake.requests if r.method == "PUT" and r.path.endswith("/content")][-1]
    assert (small.body, small.size, small.sha256) == (b"abc", 3, hashlib.sha256(b"abc").hexdigest())


def test_o47_a_request_kept_by_the_fakes_log_holds_its_body_only_up_to_the_cap():
    """O47: the cap is `BODY_KEPT_BYTES`: at it a body is kept whole, a byte over it is not; its size and hash are
    always the body's."""
    whole = Request("PUT", "/x", {}, None, None, bytes(BODY_KEPT_BYTES))
    over = Request("PUT", "/x", {}, None, None, bytes(BODY_KEPT_BYTES + 1))
    assert (len(whole.body), whole.size) == (BODY_KEPT_BYTES, BODY_KEPT_BYTES)
    assert (over.body, over.size) == (b"", BODY_KEPT_BYTES + 1)
    assert over.sha256 == hashlib.sha256(bytes(BODY_KEPT_BYTES + 1)).hexdigest()
    assert Request("GET", "/x", {}, None, None).size == 0


# -- the fallback collection ------------------------------------------------------------------------------------------


@pytest.fixture
def collections():
    """The generation of every collection that runs while a test does, from the collector's own callback: a collection
    that `gc.collect` is asked for runs it with the automatic collector off too."""
    found = []

    def note(phase, info):
        if phase == "start":
            found.append(info["generation"])

    gc.callbacks.append(note)
    try:
        yield found
    finally:
        gc.callbacks.remove(note)


def test_o47_a_full_collection_runs_before_the_exchange_that_follows_the_budget_being_spent(
    config, fake, seen, collections
):
    """O47, the fallback and not the fix: once the bodies a client has moved since its last collection reach
    `collect_after_bytes`, a full collection runs BEFORE the next exchange begins, never inside the one that spent the
    budget, whose own bytes it would leave alive. Reading a MiB twice spends 2 MiB; the third read collects."""
    file_id = fake.add_file("study/part.bin", bytes(MIB))
    client = watched_client(config, fake, seen, collect_after_bytes=2 * MIB)
    ranged = {"offset": 0, "length": MIB, "provider": "alpha"}
    for done in (1, 2):
        client.read_range(f"/data/{file_id}/content", **ranged)
        assert collections == [], f"collected after {done} reads, inside or right after the exchange that spent it"
    client.read_range(f"/data/{file_id}/content", **ranged)
    assert collections == [2]
    client.read_range(f"/data/{file_id}/content", **ranged)  # the count began again at the collection
    assert collections == [2]
    client.read_range(f"/data/{file_id}/content", **ranged)  # and reaches the budget again after two reads
    assert collections == [2, 2]


def test_o47_no_collection_runs_while_the_bodies_stay_under_the_budget(config, fake, seen, collections):
    """O47: below the budget nothing is collected, the small exchanges that make up most of a run included."""
    file_id = fake.add_file("study/part.bin", bytes(MIB))
    client = watched_client(config, fake, seen, collect_after_bytes=64 * MIB)
    for _ in range(8):
        client.read_range(f"/data/{file_id}/content", provider="alpha", offset=0, length=MIB)
        client.get(f"/data/{file_id}", provider="alpha")
    assert collections == []


def test_o47_a_client_told_no_budget_never_collects(config, fake, seen, collections):
    """O47: `collect_after_bytes=None` turns the fallback off, for a caller that runs its own collections."""
    file_id = fake.add_file("study/part.bin", bytes(MIB))
    client = watched_client(config, fake, seen, collect_after_bytes=None)
    for _ in range(4):
        client.read_range(f"/data/{file_id}/content", provider="alpha", offset=0, length=MIB)
    assert collections == []


def test_o47_the_bytes_of_a_write_count_toward_the_budget_as_those_of_a_read_do(
    config, fake, seen, big, ticker, tmp_path, collections
):
    """O47: a file of three MiB goes in as three writes of one; the third finds two MiB spent, and collects first."""
    client = watched_client(config, fake, seen, collect_after_bytes=2 * MIB)
    provisioner_over(client, config, big, ticker, tmp_path).put_file("ingest/sent.bin", bytes(3 * MIB))
    assert collections == [2]


def test_o47_the_bytes_of_a_streamed_read_count_toward_the_budget(config, fake, seen, collections):
    """O47: a streamed read of four MiB in ranges of one is four exchanges; the third finds two MiB spent, and
    collects first."""
    file_id = fake.add_file("study/part.bin", bytes(4 * MIB))
    client = watched_client(config, fake, seen, collect_after_bytes=2 * MIB)
    found = client.read_content(
        f"/data/{file_id}/content", provider="alpha", size=4 * MIB, chunk_bytes=MIB, deadline_s=60
    )
    assert found.size == 4 * MIB
    assert collections == [2]


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_o47_a_budget_that_is_no_positive_whole_number_of_bytes_is_refused(config, fake, budget):
    """O47: a budget of nothing would collect before every exchange, so it is refused when the client is built."""
    with pytest.raises(ValueError, match="collect_after_bytes"):
        OnezoneClient(config, http=fake.client_http(), collect_after_bytes=budget)


# -- the real transport ---------------------------------------------------------------------------------------------


class Answers(http.server.BaseHTTPRequestHandler):
    """A loopback server that reads whatever it is sent and answers each request with SIZE bytes of JSON."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        while length:
            length -= len(self.rfile.read(min(length, 64 * 1024)))
        body = b'{"filler": "' + b"x" * (SIZE - 15) + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_PUT = answer


class Loopback(httpx.BaseTransport):
    """httpx's own transport and connection pool, sent to a local server whatever the URL names, with a weak reference
    to every answer it makes."""

    def __init__(self, port, seen):
        self._real, self._port, self._seen = httpx.HTTPTransport(), port, seen

    def handle_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        response = self._real.handle_request(request)
        self._seen.extend((weakref.ref(request), weakref.ref(response)))
        return response

    def close(self):
        self._real.close()


def test_o47_the_pool_of_the_real_transport_does_not_keep_an_exchange_either(config, seen):
    """O47: the cycle is httpx's, the same over a socket as over a mock transport, and httpcore's pool adds objects of
    its own to it; a four MiB write and a four MiB answer, over a real connection, leave nothing alive."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answers)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = OnezoneClient(config, http=httpx.Client(transport=Loopback(server.server_address[1], seen)))
    try:
        gc.collect()
        with collector_off():
            for _ in range(4):
                client._request("PUT", "/spaces", provider=None, headers={}, content=bytes(SIZE))
                assert len(client.get("/spaces")["filler"]) == SIZE - 15
            assert seen
            assert alive(seen) == 0
    finally:
        client.close()
        server.shutdown()
        server.server_close()
