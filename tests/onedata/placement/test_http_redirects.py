"""A 3xx answer is never a result, on every path the client sends a request (O47, #259).

The client follows no redirect, so a 3xx is the answer it gets, and a bodyless one decodes to `{}`: an empty success, a
false pass for a write as much as for a read. Each path raises a `PlatformError` that names the status, holds no
`Location` value, retries nothing and leaves no request or answer alive, whatever the status and whether the answer
names a `Location` or carries a body. The HTTP client each test gives the layer follows redirects: it must not."""

import gc
import weakref

import httpx
import pytest

from expcore_onedata.client import API_ZONE, OnezoneClient, PlatformError
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.readretry import ReadRetry
from expcore_onedata.testing import ROOT_ID

ELSEWHERE = "elsewhere.test"
STATUSES = [300, 301, 302, 303, 304, 307, 308]
SHAPES = {
    "bare": {},
    "body": {"content": b'{"name": "moved", "fileId": "elsewhere"}'},
    "location": {"headers": {"Location": f"https://{ELSEWHERE}/moved"}},
    "location-and-body": {
        "headers": {"Location": f"https://{ELSEWHERE}/moved"},
        "content": b'{"name": "moved", "fileId": "elsewhere"}',
    },
}


@pytest.fixture(autouse=True)
def collector_off():
    """Run with the cyclic collector off, so that what an exchange leaves alive is what its own references hold."""
    was_on = gc.isenabled()
    gc.disable()
    yield
    if was_on:
        gc.enable()


class Moving:
    """A federation that answers the requests `where` picks with a 3xx and every other request as `fake` does. It
    records the hosts it was asked, how many requests it moved, and a weak reference to every request and answer."""

    def __init__(self, fake, status, shape, where):
        self.fake, self.status, self.shape, self.where = fake, status, shape, where
        self.moved = 0
        self.hosts = []
        self.seen = []

    def __call__(self, request):
        self.hosts.append(request.url.host)
        if self.where(request):
            self.moved += 1
            response = httpx.Response(self.status, **self.shape)
        else:
            response = self.fake.respond(request)
        self.seen.extend((weakref.ref(request), weakref.ref(response)))
        return response

    def alive(self):
        return sum(1 for ref in self.seen if ref() is not None)

    def client(self, config):
        """A client over this federation whose HTTP client follows redirects, which the layer must not."""
        return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True))


def refusal(call):
    """Run `call`, which must raise a PlatformError, and return its status, its transport flag and its message; the
    error itself is let go here, with the frames it passed through."""
    try:
        call()
    except PlatformError as error:
        return error.status, error.transport, str(error)
    raise AssertionError("a 3xx answer was accepted")


def moved_with(fake, status, shape, where):
    return Moving(fake, status, SHAPES[shape], where)


def assert_refused(moving, outcome, status, shape, fake):
    """The error names the status and the presence of a Location, never its value or a credential; nothing was sent
    on, and nothing of the exchange is alive."""
    got, transport, message = outcome
    assert (got, transport) == (status, False)
    assert str(status) in message
    assert ("Location" in message) is ("location" in shape)
    assert ELSEWHERE not in message
    assert fake.admin_token not in message
    assert ELSEWHERE not in moving.hosts
    assert moving.alive() == 0


every_status = pytest.mark.parametrize("status", STATUSES)
every_shape = pytest.mark.parametrize("shape", list(SHAPES))


def anything(request):
    return True


def a_put(request):
    return request.method == "PUT"


def the_files_content(request):
    return request.method == "GET" and request.url.path.endswith("/content")


# -- a read ---------------------------------------------------------------------------------------------------------


READS = {
    "get": lambda client, policy: client.get("/spaces/s", retry=policy),
    "get_dated": lambda client, policy: client.get_dated("/spaces/s", retry=policy),
}


@every_status
@every_shape
@pytest.mark.parametrize("read", list(READS))
def test_o47_a_3xx_answer_to_a_read_is_an_error_that_names_the_status_and_is_not_retried(
    config, fake, status, shape, read
):
    """O47: a read answered with a 3xx raises, where a bodyless one used to read as `{}` and one with a JSON body as
    that body. It is not a timeout, so a read under a policy of three attempts is made once and counts no retry."""
    moving = moved_with(fake, status, shape, anything)
    policy = ReadRetry(sleep=lambda seconds: None)
    client = moving.client(config)
    outcome = refusal(lambda: READS[read](client, policy))
    assert_refused(moving, outcome, status, shape, fake)
    assert (moving.moved, policy.retries) == (1, 0)


def test_o47_the_policy_the_3xx_tests_use_does_retry_a_read_that_timed_out(config):
    """O47: the control for those tests: the same policy retries a timeout, so a 3xx that is made once was not retried
    because the policy was off."""
    asked = []

    def timed_out(request):
        asked.append(request)
        raise httpx.ReadTimeout("no answer", request=request)

    policy = ReadRetry(sleep=lambda seconds: None)
    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(timed_out)))
    assert refusal(lambda: client.get("/spaces/s", retry=policy))[1] is True
    assert (len(asked), policy.retries) == (3, 2)


# -- a write, a mutation and an upload ------------------------------------------------------------------------------


WRITES = {
    "a write of content": (
        lambda client, file_id: client.mutate(
            "PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"written"
        ),
        a_put,
    ),
    "a creation": (
        lambda client, file_id: client.mutate(
            "POST", f"/data/{ROOT_ID}/children", provider="alpha", params={"name": "made", "type": "DIR"}
        ),
        lambda request: request.method == "POST" and request.url.path.endswith("/children"),
    ),
    "a delete": (
        lambda client, file_id: client.mutate("DELETE", f"/data/{file_id}", provider="alpha"),
        lambda request: request.method == "DELETE",
    ),
}


@every_status
@every_shape
@pytest.mark.parametrize("write", list(WRITES))
def test_o47_a_3xx_answer_to_a_mutation_is_an_error_not_an_empty_success(config, fake, status, shape, write):
    """O47: a write answered with a 3xx was taken for accepted (`{}`, or the answer's JSON body as the new file's id).
    It raises with the status, is sent once whatever the gate's policy, and `Location` is never followed."""
    file_id = fake.add_file("study/part.bin", b"x")
    send, picks = WRITES[write]
    moving = moved_with(fake, status, shape, picks)
    client = moving.client(config)
    outcome = refusal(lambda: send(client, file_id))
    assert_refused(moving, outcome, status, shape, fake)
    assert moving.moved == 1


@every_status
@every_shape
def test_o47_a_3xx_answer_to_the_gates_own_read_leaves_the_target_unread_and_nothing_sent(config, fake, status, shape):
    """O47: the read that finds a mutation's space is a request too. Answered with a 3xx it is no name, so the request
    is not sent (`ScopeUnread`, which keeps the status), and a gate policy of three attempts does not retry it."""
    file_id = fake.add_file("study/part.bin", b"x")
    moving = moved_with(
        fake,
        status,
        shape,
        lambda request: request.method == "GET" and request.url.path.startswith(f"{API_ZONE}/spaces/"),
    )
    client = moving.client(config)
    policy = ReadRetry(sleep=lambda seconds: None)
    outcome = refusal(
        lambda: client.mutate(
            "PUT", f"/data/{file_id}/content", provider="alpha", params={"offset": 0}, content=b"w", gate_retry=policy
        )
    )
    assert outcome[0] == status
    assert str(status) in outcome[2]
    assert (moving.moved, policy.retries) == (1, 0)
    assert not any(request.method == "PUT" for request in fake.requests)
    assert moving.alive() == 0


@every_status
@every_shape
def test_o47_a_3xx_answer_to_an_upload_fails_it_and_never_reads_as_written(
    config, placement, fake, ticker, tmp_path, status, shape
):
    """O47: the provisioner's upload of a file is a run of writes of content. One answered with a 3xx is a failed
    upload, with the status, where it used to count as accepted and have the size read back refuse it later."""
    moving = moved_with(fake, status, shape, a_put)
    provisioner = Provisioner(
        moving.client(config),
        config,
        placement.settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    source = tmp_path / "part.bin"
    source.write_bytes(b"some bytes")
    outcome = refusal(lambda: provisioner.upload("ingest/part.bin", source))
    assert_refused(moving, outcome, status, shape, fake)
    assert moving.moved == 1


# -- the reads of a file's content ----------------------------------------------------------------------------------


@every_status
@every_shape
def test_o47_a_3xx_answer_to_a_ranged_read_is_an_error_that_names_the_status(config, fake, status, shape):
    """O47: a range read's answer must be a 206 with its range, and a 3xx is refused as what it is, with its status,
    not as "not a 206" (a message that does not say a redirect came)."""
    file_id = fake.add_file("study/part.bin", bytes(100))
    moving = moved_with(fake, status, shape, the_files_content)
    client = moving.client(config)
    outcome = refusal(lambda: client.read_range(f"/data/{file_id}/content", provider="alpha", offset=0, length=10))
    assert_refused(moving, outcome, status, shape, fake)
    assert moving.moved == 1


@every_status
@every_shape
def test_o47_a_3xx_answer_to_a_streamed_read_is_an_error_that_is_not_retried(config, fake, status, shape):
    """O47: the streamed range is sent once, whatever the policy of its gate, and answered with a 3xx it raises with
    the status; its body, which a 3xx may carry, is never read, and the answer is released as the block ends."""
    file_id = fake.add_file("study/part.bin", bytes(100))
    moving = moved_with(fake, status, shape, the_files_content)
    client = moving.client(config)
    policy = ReadRetry(sleep=lambda seconds: None)
    fed = []
    outcome = refusal(
        lambda: client.read_content(
            f"/data/{file_id}/content",
            provider="alpha",
            size=100,
            chunk_bytes=100,
            deadline_s=60,
            sink=fed.append,
            gate_retry=policy,
        )
    )
    assert_refused(moving, outcome, status, shape, fake)
    assert (moving.moved, policy.retries) == (1, 0)
    assert fed == []


@every_status
@every_shape
@pytest.mark.parametrize("first_byte", [False, True])
def test_o47_a_3xx_answer_to_the_head_of_a_content_read_is_an_error_not_a_status_it_returns(
    config, fake, status, shape, first_byte
):
    """O47: `content_answer` returned any status below 400, so a 3xx came back as a result of the pre-campaign check.
    It raises, and the answer is closed unread."""
    file_id = fake.add_file("study/part.bin", bytes(100))
    moving = moved_with(fake, status, shape, the_files_content)
    client = moving.client(config)
    outcome = refusal(
        lambda: client.content_answer(f"/data/{file_id}/content", provider="alpha", first_byte=first_byte)
    )
    assert_refused(moving, outcome, status, shape, fake)
    assert moving.moved == 1


@pytest.mark.parametrize("status", [200, 204, 206])
def test_o47_an_answer_that_is_no_3xx_is_not_refused_as_one(config, fake, status):
    """O47: the check is of the 3xx range alone: a 2xx answer is the result it was, bodyless or not."""
    moving = moved_with(fake, status, "bare", anything)
    client = moving.client(config)
    assert client.get("/spaces/s") == {}
    assert moving.moved == 1
