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
from expcore_onedata.client import OnezoneClient, PlatformError, ScopeUnread
from expcore_onedata.eviction import evict_ids
from expcore_onedata.health import PlatformHealth
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges
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
PARENT_ID = reads(
    r"/data/[^/]+", attribute="parent_id"
)  # the gate's read of a file's parent, never cached for a new one
SPACE_NAME_READ = reads(r"/spaces/[^/]+", host="zone.test", plain=True)  # the gate's read of a space's name, on Onezone
RECORD = reads(r"/transfers/[^/]+")  # one transfer's record: what a poll reads
BETA_DISTRIBUTION = reads(r"/data/[^/]+/distribution", host="beta.test")


def nth(matches, number):
    """A matcher of the `number`th request `matches` accepts, counted from the first, and of no other."""
    seen = []

    def which(request):
        if not matches(request):
            return False
        seen.append(request)
        return len(seen) == number

    return which


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
    # the wait reads `name`, `size` and `parent_id` together, and a matcher compares the first value, `name`
    flaky.hang(reads(r"/data/[^/]+", host="alpha.test", attribute="name"))
    flaky.hang(reads(r"/data/[^/]+", host="beta.test", attribute="name"))
    provisioner.await_visible("beta", [file_id])
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (2, 2, [2.0, 2.0])


# -- the mutation gate's reads --------------------------------------------------------------------------------


def test_o29_the_gate_read_before_the_first_chunk_of_a_new_file_is_asked_again_and_each_chunk_is_written_once(
    provisioner, seeded, flaky, ticker, tmp_path
):
    """O29: the first chunk of a new file's upload reads the new file's parent from the data provider before its PUT,
    and that is no cached answer; it hangs once, is asked again, and the upload finishes: no chunk is sent twice, and
    the file is created once."""
    source = tmp_path / "new.bin"
    source.write_bytes(DATA)
    flaky.hang(nth(PARENT_ID, 2))  # the first is the directory's, for the creation; the second is the new file's
    done = provisioner.upload("study/new.bin", source)
    assert (done.action, done.digest.size) == ("created", len(DATA))
    (stalled,) = flaky.timed_out
    assert stalled.url.path.endswith(provisioner.logged()[-1]["id"])
    assert provisioner.reads_retried == 1
    writes = [r.method for r in flaky.sent if r.method != "GET"]
    assert (writes.count("POST"), writes.count("PUT")) == (1, len(DATA) // 1024)


def test_o29_a_gate_read_that_times_out_three_times_ends_the_upload_with_nothing_sent(
    provisioner, seeded, flaky, ticker, tmp_path
):
    """O29: the gate stays closed: three attempts at the read that finds the target's space, 2 s and 5 s apart, then
    ScopeUnread, a transport failure, and not one request that changes anything left."""
    source = tmp_path / "new.bin"
    source.write_bytes(DATA)
    flaky.hang(PARENT_ID, times=None)
    with pytest.raises(ScopeUnread) as caught:
        provisioner.upload("study/new.bin", source)
    assert (caught.value.transport, caught.value.status) == (True, None)
    assert [r for r in flaky.sent if r.method != "GET"] == []
    assert (len(flaky.timed_out), provisioner.reads_retried, ticker.slept) == (3, 2, [2.0, 5.0])


def test_o29_the_space_name_read_of_a_verification_is_asked_again(provisioner, seeded, flaky, ticker):
    """O29: `verify_space` reads the space's name from Onezone; the read hangs once, is asked again, and the name is
    the one it answered."""
    flaky.hang(SPACE_NAME_READ)
    assert provisioner.verify_space() == SPACE_NAME
    retried_once(provisioner, flaky, ticker)


def test_o29_the_gate_read_of_a_range_read_is_asked_again_and_the_range_is_read_once(
    provisioner, seeded, flaky, ticker
):
    """O29: `fetch` is a read that makes a provider fetch bytes, so it passes the gate; the gate's read of the file's
    parent through that provider hangs once and is asked again, and the range read itself, which could have fetched,
    is one request."""
    flaky.hang(reads(r"/data/[^/]+", host="beta.test", attribute="parent_id"))
    assert len(provisioner.fetch("beta", seeded.id_of(PATH), 0, 100)) == 100
    assert provisioner.reads_retried == 1
    assert flaky.count(CONTENT) == 1


def test_o29_the_gate_read_of_a_streamed_read_is_asked_again_and_each_range_is_streamed_once(
    provisioner, seeded, flaky, ticker
):
    """O29: `read_file` passes the gate before each range; a gate read that hangs once is asked again, and the ranges
    that were streamed are one request each."""
    flaky.hang(PARENT_ID)
    assert provisioner.read_file(seeded.id_of(PATH), size=len(DATA)).size == len(DATA)
    retried_once(provisioner, flaky, ticker)
    assert flaky.count(CONTENT) == len(DATA) // 1024


def test_o29_the_gate_reads_of_a_streamed_read_are_not_retried_past_its_deadline(provisioner, seeded, flaky, ticker):
    """O29: a gate read that hangs for 15 s under a deadline of 16 s leaves no room for the 2 s pause and a second, so
    the read is not asked again, and the streamed read fails closed with nothing streamed; with a deadline of 100 s the
    same stall is asked again and the file is read."""
    file_id = seeded.id_of(PATH)
    flaky.hang(PARENT_ID)
    with pytest.raises(ScopeUnread):
        provisioner.read_file(file_id, size=len(DATA), deadline_s=16.0)
    assert (provisioner.reads_retried, flaky.count(CONTENT)) == (0, 0)
    flaky.hang(PARENT_ID)
    assert provisioner.read_file(file_id, size=len(DATA), deadline_s=100.0).size == len(DATA)
    assert provisioner.reads_retried == 1


# -- the polls whose failure ends the operation, and the placement's first block-map read -------------------------


def on_beta(seeded):
    """The seeded file, held whole by beta as well as by the data provider."""
    file_id = seeded.id_of(PATH)
    seeded.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return file_id


def test_o29_the_poll_of_a_replication_that_times_out_is_asked_again(provisioner, seeded, flaky, ticker):
    """O29: `replicate` polls the transfer's record until it ends; a poll that hangs once is asked again, as the
    reset's own polls are, and the replication is confirmed by the block map as before."""
    flaky.hang(RECORD)
    record = provisioner.replicate(PATH, "beta")
    assert record["placement"]["fully_placed"] is True
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 1)


def test_o29_the_poll_of_an_eviction_that_times_out_is_asked_again(provisioner, seeded, flaky, ticker):
    """O29: `evict` polls the eviction's record; a poll that hangs once is asked again and the eviction is judged by
    the block map."""
    on_beta(seeded)
    flaky.hang(RECORD)
    assert provisioner.evict(PATH, "beta")["empty"] is True
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 1)


def test_o29_the_bulk_evictions_wait_for_its_transfers_is_asked_again(provisioner, seeded, flaky, ticker):
    """O29: `evict_ids` waits for its transfers to end with polls of their records; the first hangs once and is asked
    again, so a stall of one poll does not fail the eviction."""
    flaky.hang(RECORD)
    record = evict_ids(provisioner, [on_beta(seeded)], "beta")
    assert record["empty"] is True
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 1)


def test_o29_a_poll_of_the_resets_eviction_stage_that_times_out_does_not_fail_the_reset(
    provisioner, seeded, flaky, ticker
):
    """O29: the reset's eviction stage waits for its eviction through the same polls; one that hangs once is asked
    again, the reset passes, and its record counts the retry."""
    on_beta(seeded)
    flaky.hang(RECORD)
    record = SpaceReset(provisioner).run(evict={"beta": [PATH]})
    assert record["ok"] is True
    assert (record["reads_retried"], len(flaky.timed_out)) == (1, 1)


def test_o29_the_retries_of_a_transfer_poll_stay_within_the_waits_own_bound(provisioner, seeded, flaky, ticker):
    """O29: the transfer wait's bound is 30 s here and each stall takes the 15 s of the client's timeout: the first
    attempt ends at 15 s, the pause leaves 13 s for the one retry, and a third attempt and its 5 s pause do not fit, so
    the wait ends as a transport failure that says two attempts were made, never after the bound."""
    on_beta(seeded)
    flaky.hang(RECORD, times=None)
    began = ticker.now
    with pytest.raises(PlatformError, match=r"providers\[1\]: GET failed \(ReadTimeout\) after 2 attempts"):
        provisioner.evict(PATH, "beta")
    assert (len(flaky.timed_out), provisioner.reads_retried) == (2, 1)
    assert ticker.now - began <= provisioner.settings.transfer_timeout_s


def test_o29_a_block_map_read_is_one_attempt_unless_its_caller_names_a_policy(provisioner, seeded, flaky, ticker):
    """O29: `blocks` is the probes' read and the polls' read, one attempt by default; a caller that is plumbing hands it
    the provisioner's policy, and the read that hangs once is asked again and counted."""
    file_id = seeded.id_of(PATH)
    flaky.hang(DISTRIBUTION, times=2)
    with pytest.raises(PlatformError, match=r"^providers\[0\]: GET failed \(ReadTimeout\)$"):
        provisioner.blocks(file_id, "alpha")
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 0)
    assert provisioner.blocks(file_id, "alpha", retry=provisioner.retry)
    assert (len(flaky.timed_out), provisioner.reads_retried) == (2, 1)


def test_o29_the_block_map_read_before_a_placement_is_asked_again(provisioner, seeded, flaky, ticker):
    """O29: `place_ranges` reads what the receiver holds before it places anything, outside any poll that would absorb
    a failed read; the read hangs once and is asked again, and the placement goes on."""
    flaky.hang(nth(BETA_DISTRIBUTION, 1))
    record = place_ranges(provisioner, [RangeRow(PATH, "beta", ((0, 100),))])
    assert record["providers"]["providers[1]"]["placement"]["fully_placed"] is True
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 1)


def test_o29_the_block_map_read_after_a_placement_is_asked_again_too(provisioner, seeded, flaky, ticker):
    """O29: the last read of `place_ranges` records what the receiver holds of each file, after the placement settled,
    and is no part of a poll either: its stall is asked again. The placement runs twice on a receiver emptied between,
    and the second run's last block-map read of beta is the one that hangs."""
    row = RangeRow(PATH, "beta", ((0, 100),))
    place_ranges(provisioner, [row])
    reads_of_a_placement = flaky.count(BETA_DISTRIBUTION)
    seeded.blocks.pop((seeded.id_of(PATH), BETA_ID))
    flaky.hang(nth(BETA_DISTRIBUTION, reads_of_a_placement))
    record = place_ranges(provisioner, [row])
    assert record["providers"]["providers[1]"]["per_file"][PATH]["held_bytes"] > 0
    assert (len(flaky.timed_out), provisioner.reads_retried) == (1, 1)


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
