"""O29: the plumbing's reads that time out are tried again, in the record, and the measuring reads are not (#185).

The provisioner's listings and metadata reads, `read_file`'s size read, the visibility wait and the reset's reads go
under the provisioner's policy: a read that hangs past its timeout once is asked again after 2 s, and the count of such
retries is in the reset's record. A probe's read, a block map, the health gate's reads, a streamed or range read and a
write are asked once. The federation is the public fake, behind a transport that makes chosen requests time out.
"""

import re

import httpx
import pytest
from fake_reset import FakeReset
from sandbox_support import add_sandbox

from expcore.campaign.healthgate import read_health
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.health import PlatformHealth
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    BASELINE,
    BETA_ID,
    DATA,
    NOW,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.tokens import TokenIssuer

PATH = "study/data.bin"
CHECK_FILE = "study/probe.bin"


class Flaky:
    """The fake's handler behind a rule list: a request a rule matches times out (`times` times, or always), moving the
    ticker by the seconds the client allowed it, as a real wait would; every other request is the fake's."""

    def __init__(self, respond, ticker):
        self.respond, self.ticker = respond, ticker
        self.rules: list[list] = []
        self.sent: list[httpx.Request] = []
        self.timed_out: list[httpx.Request] = []

    def hang(self, matches, times=1):
        self.rules.append([matches, times])

    def __call__(self, request):
        self.sent.append(request)
        for rule in self.rules:
            matches, left = rule
            if matches(request) and (left is None or left > 0):
                if left is not None:
                    rule[1] -= 1
                self.ticker.now += request.extensions["timeout"]["read"]
                self.timed_out.append(request)
                raise httpx.ReadTimeout("simulated", request=request)
        return self.respond(request)

    def count(self, matches):
        return sum(1 for request in self.sent if matches(request))


def reads(pattern, *, host=None, plain=False, **query):
    """A matcher of the GETs whose API path matches, on one host, with `plain` only those with no query, and with the
    query values given."""

    def matches(request):
        path = request.url.path.removeprefix("/api/v3/oneprovider").removeprefix("/api/v3/onezone")
        return (
            request.method == "GET"
            and re.fullmatch(pattern, path) is not None
            and (host is None or request.url.host == host)
            and (not plain or not request.url.params)
            and all(request.url.params.get(key) == value for key, value in query.items())
        )

    return matches


CHILDREN = reads(r"/data/[^/]+/children")
ATTRIBUTES = reads(r"/data/[^/]+", plain=True)  # a file's attributes, as `read_file` and an upload read the size
CONTENT = reads(r"/data/[^/]+/content")
DISTRIBUTION = reads(r"/data/[^/]+/distribution")
TRANSFERS = reads(r"/spaces/[^/]+/transfers")
EVERY_GET = reads(r".*")


@pytest.fixture
def fake(monkeypatch, ticker):
    """The federation with a reset's calls; beta supports the space, so every path of the plumbing can be read on it."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


@pytest.fixture
def platform_file(tmp_path):
    """Both providers support the space, and a REST data path makes the health gate measure the content type first."""

    def edit(data):
        with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=1)
        data["support"]["providers"] = ["alpha", "beta"]
        add_sandbox(data)
        data["sandbox"]["content_check_file"] = CHECK_FILE

    return write_platform(tmp_path, edit)


@pytest.fixture
def flaky(fake, ticker):
    return Flaky(fake.respond, ticker)


@pytest.fixture
def client(config, flaky):
    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(flaky)))


def retried_once(provisioner, flaky, ticker):
    assert len(flaky.timed_out) == 1
    assert provisioner.reads_retried == 1
    assert ticker.slept == [2.0]


# -- the plumbing retries ------------------------------------------------------------------------------------------


def test_o29_a_listing_page_that_times_out_is_asked_again_after_two_seconds_and_counted(
    provisioner, seeded, flaky, ticker
):
    """O29: the first page of a directory's listing hangs past its timeout; the provisioner waits 2 s and asks again,
    gets the page, and counts one retry."""
    flaky.hang(CHILDREN)
    assert "study" in provisioner.children(provisioner.root())
    retried_once(provisioner, flaky, ticker)


def test_o29_a_listing_that_times_out_three_times_fails_with_two_retries_counted(provisioner, seeded, flaky, ticker):
    """O29: three attempts, 2 s and 5 s apart, and then the failure, as a transport failure the caller can tell from an
    answer; the count says how many retries were made before it."""
    flaky.hang(CHILDREN, times=None)
    root = provisioner.root()
    with pytest.raises(PlatformError, match=r"providers\[0\]: GET failed \(ReadTimeout\) after 3 attempts") as caught:
        provisioner.children(root)
    assert caught.value.transport is True
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (3, 2, [2.0, 5.0])


def test_o29_read_files_size_read_is_asked_again_and_the_file_is_read(provisioner, seeded, flaky, ticker):
    """O29: `read_file` without a size reads the file's attributes first; that read hangs once, is asked again, and the
    file is read whole after it."""
    flaky.hang(ATTRIBUTES)
    found = provisioner.read_file(seeded.id_of(PATH))
    assert found.size == len(DATA)
    retried_once(provisioner, flaky, ticker)


def test_o29_the_size_read_of_an_upload_is_asked_again_and_the_upload_succeeds(
    provisioner, seeded, flaky, ticker, tmp_path
):
    """O29: an upload reads the size back after it wrote; the read hangs once and is asked again, and the upload reports
    what it did. Only reads are asked again: the file was created and written once."""
    source = tmp_path / "new.bin"
    source.write_bytes(DATA)
    flaky.hang(ATTRIBUTES)
    done = provisioner.upload("study/new.bin", source)
    assert (done.action, done.digest.size) == ("created", len(DATA))
    retried_once(provisioner, flaky, ticker)
    writes = [r.method for r in flaky.sent if r.method != "GET"]
    assert writes.count("POST") == 1
    assert writes.count("PUT") == len(DATA) // 1024


def test_o29_the_visibility_wait_is_asked_again_on_both_providers(provisioner, seeded, flaky, ticker):
    """O29: `await_visible` reads the file's size on the data provider, then through the receiver; each read hangs once
    and is asked again, two retries in all."""
    file_id = seeded.id_of(PATH)
    flaky.hang(reads(r"/data/[^/]+", host="alpha.test", attribute="size"))
    flaky.hang(reads(r"/data/[^/]+", host="beta.test", attribute="size"))
    provisioner.await_visible("beta", [file_id])
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (2, 2, [2.0, 2.0])


# -- what is never asked again ---------------------------------------------------------------------------------


def test_o29_a_block_map_that_times_out_is_one_attempt(provisioner, seeded, flaky, ticker):
    """O29: the block map is what the probes record and the placement polls judge: its timeout stays the timeout."""
    flaky.hang(DISTRIBUTION)
    with pytest.raises(PlatformError, match=r"^providers\[0\]: GET failed \(ReadTimeout\)$"):
        provisioner.blocks(seeded.id_of(PATH), "alpha")
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (1, 0, [])


def test_o29_a_streamed_read_and_a_range_read_that_time_out_are_one_attempt(provisioner, seeded, flaky, ticker):
    """O29: a content read, streamed by ranges or read by one range, is never asked again: it may have made the
    provider fetch bytes, and the bytes that arrived are not the file."""
    file_id = seeded.id_of(PATH)
    flaky.hang(CONTENT, times=None)
    with pytest.raises(PlatformError, match=r"GET failed \(ReadTimeout\)$"):
        provisioner.read_file(file_id, size=len(DATA))
    with pytest.raises(PlatformError, match=r"GET failed \(ReadTimeout\)$"):
        provisioner.fetch("alpha", file_id, 0, 100)
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (2, 0, [])


# -- the measuring sites -----------------------------------------------------------------------------------------


def test_o29_a_probes_listing_is_one_attempt_and_only_the_probes_own_retry_remains(
    provisioner, client, seeded, flaky, ticker
):
    """O29: a probe resolves the tracked file by listing, with no wait (`wait=False`): each listing is one request, and
    the probe's own three attempts at a transport failure, counted in its record, are all that is asked, so a stall is
    not hidden inside a retried read."""
    flaky.hang(CHILDREN, times=None)
    probes = PlatformProbes(
        client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock
    )
    probes.start({})
    assert flaky.count(CHILDREN) == 3  # the probe's 3 attempts, each one request: not 9
    assert provisioner.reads_retried == 0
    assert 2.0 not in ticker.slept
    assert 5.0 not in ticker.slept  # the probe's backoff only, never the plumbing's pauses


def test_o29_a_probes_block_map_read_is_one_request_per_attempt(provisioner, client, seeded, flaky, ticker):
    """O29: the placement probe reads each provider's block map; a stall there is the probe's recorded failure."""
    flaky.hang(DISTRIBUTION, times=None)
    probes = PlatformProbes(client, provisioner, {"data": PATH}, ["alpha"], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    assert flaky.count(DISTRIBUTION) == 3
    assert provisioner.reads_retried == 0


def test_o29_the_health_gates_reads_are_one_attempt(config, client, seeded, flaky, ticker):
    """O29: the gate's first read measures the content type through the local provider's space root; when it times
    out, the check is refused on that one request, with no pause: the gate records what the platform does."""
    seeded.add_file(CHECK_FILE, DATA)
    flaky.hang(EVERY_GET, times=None)
    gate = PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)
    result = read_health(gate)
    assert result.ok is False
    assert flaky.count(EVERY_GET) == 1
    assert ticker.slept == []


# -- the reset carries the count ----------------------------------------------------------------------------------


def test_o29_a_reset_read_that_times_out_is_asked_again_and_its_record_counts_it(provisioner, seeded, flaky, ticker):
    """O29: a transfer listing hangs once during the reset; it is asked again, the reset passes, and its record says
    `reads_retried` 1, the provisioner's own count over the reset."""
    flaky.hang(TRANSFERS)
    record = SpaceReset(provisioner).run()
    assert record["ok"] is True
    assert (record["reads_retried"], provisioner.reads_retried, len(flaky.timed_out)) == (1, 1, 1)


def test_o29_a_reset_that_retried_nothing_says_zero(provisioner, seeded, flaky):
    """O29: the key is always in the record, so that a reader sees a count of zero, not an absent field."""
    assert SpaceReset(provisioner).run()["reads_retried"] == 0


def test_o29_a_failed_reset_still_says_how_many_reads_it_retried(provisioner, seeded, flaky, ticker):
    """O29: a listing that times out on all three attempts fails the reset at its stage; the record of the failure holds
    the two retries it made, since a failed reset's evidence is the one that is read."""
    flaky.hang(TRANSFERS, times=None)
    with pytest.raises(ResetFailed) as caught:
        SpaceReset(provisioner).run()
    assert caught.value.stage == "transfers"
    assert caught.value.record["reads_retried"] == 2
    assert len(flaky.timed_out) == 3


def test_o29_the_count_of_a_second_reset_is_its_own(provisioner, seeded, flaky, ticker):
    """O29: the reset reports the retries made during it, not the provisioner's total since it was built."""
    flaky.hang(TRANSFERS)
    first = SpaceReset(provisioner).run()
    second = SpaceReset(provisioner).run()
    assert (first["reads_retried"], second["reads_retried"], provisioner.reads_retried) == (1, 0, 1)
