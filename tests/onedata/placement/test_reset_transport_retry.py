"""A reset's per-file reads try a connection failure again, never an answer, record what they met, and stay within a
per-provider bound on the reads in flight (O29, O46, O51).

A live reset read 8,290 block maps at one provider and then one `GET` failed to connect: the map was unknown and the
reset failed closed, 1,238 s lost. Each path of the reset's per-file reads (the occupancy stage, the quiet stage's maps,
the settle, the QoS scan, the planner's reading and the shortcut's narrowed reads) now tries such a read again, four
attempts 2, 5 and 15 s apart, and an exhausted one still fails closed. The federation is the public fake behind a
transport that breaks chosen requests; its clock is the test's ticker, so no pause really waits.
"""

import dataclasses
import re
import threading
import time

import httpx
import pytest
from qos_world import FILES, federation, provisioner_with, two_providers  # the reset's QoS world

from expcore_onedata import consistency, occupancy, resetqos
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
from expcore_onedata.transferrecord import Unreadable

PAUSES = [2.0, 5.0, 15.0]
HELD = len(FILES) * len(DATA)  # what beta holds of the space: every file of `study`
LATCH_WAIT_S = 10.0  # the most a latched request waits for the others; reached only when no overlap can happen


class Broken:
    """The fake behind rules: a request a rule matches gets the rule's outcome, `times` times (None: always), an
    exception raised or an answer returned; every other request is the fake's. `peak` is the most requests to beta in
    flight at once, when `hold_s` keeps each one open that long, or when `latch_at` holds each one until that many are
    in flight together (`latch`, set then; each waits at most `LATCH_WAIT_S`): an overlap proved without timing."""

    def __init__(self, respond):
        self.respond = respond
        self.rules: list[list] = []
        self.sent: list[httpx.Request] = []
        self.hold_s, self.peak, self._now = 0.0, 0, 0
        self.latch_at, self.latch = 0, threading.Event()
        self._lock = threading.Lock()

    def on(self, matches, outcome, times=1):
        self.rules.append([matches, outcome, times])

    def __call__(self, request):
        with self._lock:
            self.sent.append(request)
            rule = next((r for r in self.rules if r[0](request) and (r[2] is None or r[2] > 0)), None)
            if rule is not None and rule[2] is not None:
                rule[2] -= 1
        if rule is not None:
            return rule[1](request)
        held = (self.hold_s or self.latch_at) and request.url.host == "beta.test"
        return self._held(request) if held else self.respond(request)

    def _held(self, request):
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
            if self.latch_at and self._now >= self.latch_at:
                self.latch.set()
        try:
            if self.latch_at:
                self.latch.wait(LATCH_WAIT_S)  # returns at once when enough requests are in flight together
            else:
                time.sleep(self.hold_s)  # a real wait, short: an upper bound holds whatever the timing
            return self.respond(request)
        finally:
            with self._lock:
                self._now -= 1

    def count(self, matches):
        return sum(1 for request in self.sent if matches(request))


def gets(pattern, host=None):
    """A matcher of the provider GETs whose API path matches `pattern`, on `host` when given."""

    def matches(request):
        path = request.url.path.removeprefix("/api/v3/oneprovider")
        return request.method == "GET" and re.fullmatch(pattern, path) and (host is None or request.url.host == host)

    return matches


def nth(matches, number):
    """A matcher of the `number`th request `matches` accepts, and of no other."""
    seen = []

    def which(request):
        if not matches(request):
            return False
        seen.append(request)
        return len(seen) == number

    return which


def raising(kind):
    def outcome(request):
        raise kind("simulated", request=request)

    return outcome


def answering(status, body=None):
    return lambda request: httpx.Response(status, json=body if body is not None else {"error": {"id": "notFound"}})


class CutBody(httpx.SyncByteStream):
    """A body that breaks after its first bytes, as a connection reset in the middle of an answer does."""

    def __iter__(self):
        yield b'{"error": '
        raise httpx.ReadError("simulated")


BLOCK_MAPS = gets(r"/data/[^/]+/distribution", "beta.test")
COUNTER = gets(r"/data/[^/]+/distribution", "alpha.test")  # the space root's, on the data provider
PAGES = gets(r"/data/[^/]+/files")
CHILDREN = gets(r"/data/[^/]+/children")  # `summaries` mode's directory listings
LISTINGS = gets(r"/spaces/[^/]+/transfers")
RECORDS = gets(r"/transfers/[^/]+")
CONNECT = raising(httpx.ConnectError)


def zone_clock(request):
    """Onezone's public current-time read, the history's reading of the global clock."""
    return request.method == "GET" and request.url.path.endswith("/provider/public/get_current_time")


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def broken(fake):
    return Broken(fake.respond)


@pytest.fixture
def client(config, broken):
    return OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(broken)))


@pytest.fixture
def p(client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")


def listing_of(fake) -> occupancy.Listing:
    return occupancy.Listing(frozenset(fake.id_of(path) for path in FILES))


def holdings(p, fake, **options) -> occupancy.Reading:
    """A reading of the occupancy as the reset makes one: under a policy made for it."""
    return occupancy.holdings(
        p,
        listing_of(fake),
        (),
        providers=["alpha", "beta"],
        label=p.client.provider_label,
        retry=p.reading_retry(),
        **options,
    )


def run(p, ticker, **options):
    return SpaceReset(p, now=ticker.clock).run(allowed={"beta": HELD}, **options)


def beta(facts) -> dict:
    return facts["providers"]["providers[1]"]


# -- the occupancy stage ---------------------------------------------------------------------------------------------


def test_o46_a_connection_failure_on_one_block_map_read_is_tried_again_and_the_reset_passes(p, broken, ticker):
    """O46: the first block map the occupancy stage reads at beta fails to connect; it is asked again 2 s later, on
    the reading's policy, and the reset passes. The stage's record counts the reads, the one retry and the
    `ConnectError`, at beta, and the provisioner's `reads_retried` holds it too."""
    broken.on(nth(BLOCK_MAPS, 1), CONNECT)
    record = run(p, ticker)
    tally = record["stages"]["occupancy"]["read_retry"]
    assert record["ok"] is True
    assert (tally["retries"], tally["errors"], tally["failed"]) == (1, {"ConnectError": 1}, 0)
    assert tally["reads"] == len(FILES) + 1  # every file's map at beta, and the counter at the data provider
    assert {k: v for k, v in beta(tally).items() if k != "slot_wait_s"} == {
        "reads": len(FILES),
        "retries": 1,
        "errors": {"ConnectError": 1},
        "failed": 0,
        "answers": {},
    }
    assert tally["policy"] == {"attempts": 4, "pauses_s": PAUSES, "transport": True, "provider_concurrency": 16}
    assert 2.0 in ticker.slept
    assert record["reads_retried"] >= 1


def test_o46_a_block_map_that_fails_to_connect_on_every_attempt_fails_the_reset_closed(p, broken, ticker):
    """O46: a provider that never lets a block-map read connect is down as far as the reset can tell: each read is
    tried four times, 2, 5 and 15 s apart, then the map is unknown and the occupancy stage fails, with the reading's
    tally in the failed stage's record."""
    broken.on(BLOCK_MAPS, CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"GET failed \(ConnectError\) after 4 attempts") as caught:
        run(p, ticker)
    stage = caught.value.record["stages"]["occupancy"]
    tally = stage["read_retry"]
    assert (stage["ok"], caught.value.stage) == (False, "occupancy")
    assert tally["failed"] >= 1
    assert tally["errors"] == {"ConnectError": 4 * tally["failed"]}
    assert set(PAUSES) <= set(ticker.slept)


# -- the quiet stage's maps, and the settle -------------------------------------------------------------------------


def test_o46_the_quiet_stages_block_map_read_tries_a_connection_failure_again(p, fake, broken):
    """O46: the quiet stage's read at T of a map the eviction emptied fails to connect once, is asked again, and the
    stage holds it to what the eviction left; its record counts the retry (`maps_read_retry`)."""
    file_id = fake.id_of(FILES[0])
    broken.on(nth(BLOCK_MAPS, 1), CONNECT)
    facts = resetqos.quiet_maps(p, {(file_id, "beta"): ((0, len(DATA)),)})
    assert facts["block_maps"] == 1
    assert (facts["maps_read_retry"]["retries"], facts["maps_read_retry"]["errors"]) == (1, {"ConnectError": 1})


def test_o46_the_quiet_stages_block_map_read_that_never_connects_fails_with_its_tally(p, fake, broken):
    """O46: the same read failing on every attempt leaves the map unread: the stage fails, never passes, and the error
    carries the tally for the failed stage's record."""
    broken.on(BLOCK_MAPS, CONNECT, times=None)
    with pytest.raises(PlatformError, match=r"after 4 attempts") as caught:
        resetqos.quiet_maps(p, {(fake.id_of(FILES[0]), "beta"): ((0, len(DATA)),)})
    assert caught.value.facts["maps_read_retry"]["failed"] == 1


def test_o33_the_settles_block_map_read_tries_a_connection_failure_again(p, fake, broken):
    """O33: a block map the settle reads after a removal fails to connect once and is asked again; the settle ends on
    stable maps and its facts count the retry."""
    broken.on(nth(BLOCK_MAPS, 1), CONNECT)
    facts, maps = resetqos.settle(p, {fake.id_of(FILES[0]): 2}, ["alpha", "beta"])
    assert maps[(fake.id_of(FILES[0]), "beta")] == ((0, len(DATA)),)
    assert (facts["read_retry"]["retries"], facts["read_retry"]["errors"]) == (1, {"ConnectError": 1})


def test_o33_the_settles_block_map_read_that_never_connects_fails_the_settle(p, fake, broken):
    """O33: a settle whose map stays unread fails, with the tally on the error."""
    broken.on(BLOCK_MAPS, CONNECT, times=None)
    with pytest.raises(PlatformError, match=r"after 4 attempts") as caught:
        resetqos.settle(p, {fake.id_of(FILES[0]): 2}, ["alpha", "beta"])
    assert caught.value.facts["read_retry"]["failed"] == 1


# -- the QoS scan, the planner's reading and the shortcut's narrowed reads -------------------------------------------


def test_o33_the_qos_scans_listing_page_tries_a_connection_failure_again(p, broken, ticker):
    """O33: the first page of the recursive listing the qos stage reads fails to connect once; it is asked again and
    the stage's record counts it (`read_retry`)."""
    broken.on(nth(PAGES, 1), CONNECT)
    record = run(p, ticker)
    assert record["stages"]["qos"]["read_retry"]["errors"] == {"ConnectError": 1}
    assert record["stages"]["qos"]["read_retry"]["retries"] == 1


def test_o33_a_qos_scan_whose_listing_never_connects_fails_the_reset_closed(p, broken, ticker):
    """O33: a listing page that never connects leaves the scan incomplete: the qos stage fails, with the tally."""
    broken.on(PAGES, CONNECT, times=None)
    with pytest.raises(ResetFailed) as caught:
        run(p, ticker)
    assert caught.value.stage == "qos"
    assert caught.value.record["stages"]["qos"]["read_retry"]["failed"] >= 1


def test_o51_the_planners_reading_tries_a_connection_failure_again_in_its_listing_and_its_block_maps(p, fake, broken):
    """O51: `plan_scan`, which another study's planner calls, retries a listing page and a block map that failed to
    connect once each, gives every file's holding at beta, and records both retries (`read_retry`)."""
    broken.on(nth(PAGES, 1), CONNECT)
    broken.on(nth(BLOCK_MAPS, 1), raising(httpx.RemoteProtocolError))
    holdings_ = SpaceReset(p).plan_scan()
    assert holdings_.held["beta"] == {fake.id_of(path): len(DATA) for path in FILES}
    tally = holdings_.facts(p.client.provider_label)["read_retry"]
    assert tally["errors"] == {"ConnectError": 1, "RemoteProtocolError": 1}
    assert tally["retries"] == 2


def test_o51_the_planners_reading_whose_block_map_never_connects_fails_closed(p, broken):
    """O51: a block map that never connects leaves the planner's reading without a verdict: it raises, with the
    tally, and never gives a holding of 0 for the file."""
    broken.on(BLOCK_MAPS, CONNECT, times=None)
    with pytest.raises(Unreadable, match=r"is unknown") as caught:
        SpaceReset(p).plan_scan()
    assert caught.value.facts["read_retry"]["failed"] >= 1


def test_o51_the_shortcuts_narrowed_reads_try_a_connection_failure_again(p, fake, broken):
    """O51: the shortcut reads only the files it names at a provider (`only`), through the same per-file read: one
    that fails to connect once is asked again, and the reading carries the rest as empty."""
    file_id = fake.id_of(FILES[0])
    broken.on(nth(BLOCK_MAPS, 1), CONNECT)
    reading = holdings(p, fake, only={"beta": frozenset({file_id})})
    assert reading.files["beta"][file_id] == len(DATA)
    assert reading.carried == {"beta": len(FILES) - 1}
    assert reading.read_retry["retries"] == 1


def test_o51_the_shortcuts_narrowed_read_that_never_connects_fails_closed(p, fake, broken):
    """O51: the narrowed read failing on every attempt fails the reading, with its tally."""
    broken.on(BLOCK_MAPS, CONNECT, times=None)
    with pytest.raises(Unreadable) as caught:
        holdings(p, fake, only={"beta": frozenset({fake.id_of(FILES[0])})})
    assert caught.value.facts["read_retry"]["failed"] == 1


# -- an answer is never asked again -----------------------------------------------------------------------------------


def test_o46_a_404_counts_nothing_and_is_never_asked_again(p, fake, broken):
    """O46: a provider that answers it has no such file holds none of it: one request, no retry."""
    first = nth(BLOCK_MAPS, 1)
    broken.on(first, answering(404))
    reading = holdings(p, fake)
    assert sorted(reading.files["beta"].values()) == [0] + [len(DATA)] * (len(FILES) - 1)
    assert (reading.read_retry["retries"], reading.read_retry["errors"]) == (0, {})
    assert reading.read_retry["answers"] == {"404": 1}  # an answer, tallied apart: the provider has no such file
    assert broken.count(BLOCK_MAPS) == len(FILES)


@pytest.mark.parametrize(
    ("outcome", "tallied"),
    [
        (answering(500), {"answers": {"500": len(FILES)}}),
        (answering(400, {"error": {"id": "posix", "details": {"errno": "eacces"}}}), {"answers": {"400": len(FILES)}}),
        (  # a provider's failed storage entry
            answering(200, {"distributionPerProvider": {BETA_ID: {"success": False}}}),
            {"answers": {"unreadable": len(FILES)}},
        ),
        (  # an error answer cut in its body: no retry, a failed read
            lambda request: httpx.Response(500, stream=CutBody()),
            {"errors": {"ReadError": len(FILES)}, "failed": len(FILES)},
        ),
    ],
    ids=["500", "posix-eacces", "failed-entry", "500-cut"],
)
def test_o46_an_error_answer_a_failed_entry_or_a_cut_error_answer_fails_at_once_and_is_never_asked_again(
    outcome, tallied, p, fake, broken, ticker
):
    """O46: an answer decides at once: an error status, a POSIX error that is no absence, a provider's failed storage
    entry and an error answer whose body was cut short each leave the map unknown after one request, no pause taken,
    and the reading fails closed; none is retried into a pass, and the failed reading's tally says which it was."""
    broken.on(BLOCK_MAPS, outcome, times=None)
    with pytest.raises(Unreadable) as caught:
        holdings(p, fake)
    tally = caught.value.facts["read_retry"]
    assert ticker.slept == []
    assert tally["retries"] == 0
    assert {key: tally[key] for key in tallied} == tallied
    assert broken.count(BLOCK_MAPS) == len(FILES)  # one request per file, every one of them failed


def test_o46_a_good_answer_cut_in_its_body_is_tried_again(p, fake, broken):
    """O46: a 200 whose body breaks off is no answer the reset can read, as a connection reset before it is not: it is
    asked again, and the retry's whole answer decides."""
    broken.on(nth(BLOCK_MAPS, 1), lambda request: httpx.Response(200, stream=CutBody()))
    reading = holdings(p, fake)
    assert reading.held("beta") == HELD
    assert reading.read_retry["errors"] == {"ReadError": 1}


# -- the per-provider bound -------------------------------------------------------------------------------------------


def bounded(client, config, platform_file, ticker, tmp_path, bound) -> Provisioner:
    """A provisioner like `p` whose `[placement] provider_concurrency` is `bound`."""
    settings = dataclasses.replace(
        load_placement(platform_file, config).settings, qos_scan="listing", provider_concurrency=bound
    )
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


@pytest.mark.parametrize("bound", [1, 2])
def test_o51_two_readings_at_once_never_have_more_reads_in_flight_at_one_provider_than_its_bound(
    bound, fake, broken, client, config, platform_file, ticker, tmp_path
):
    """O51: two readings at once, each with four workers (eight reads could be in flight), reach beta at most
    `provider_concurrency` at a time between them: the bound is the provider's, across readings, and their records
    say it."""
    first, second = (bounded(client, config, platform_file, ticker, tmp_path, bound) for _ in range(2))
    broken.hold_s = 0.02
    readings = consistency.bounded_map(lambda one: holdings(one, fake), [first, second], 2)
    assert 1 <= broken.peak <= bound
    assert all(r.held("beta") == HELD for r in readings)
    assert {r.read_retry["policy"]["provider_concurrency"] for r in readings} == {bound}


def test_o51_two_qos_scans_at_once_hold_the_bound_in_their_root_and_listing_reads_too(
    fake, broken, client, config, platform_file, ticker, tmp_path
):
    """O51: every read of a reading takes its provider's slot, the space root's and the listing pages' included: two
    scans at once never have more than one read in flight at beta under a bound of one."""
    first, second = (bounded(client, config, platform_file, ticker, tmp_path, 1) for _ in range(2))
    broken.hold_s = 0.02
    found = consistency.bounded_map(lambda one: resetqos.by_listing(one, ["alpha", "beta"]), [first, second], 2)
    assert broken.peak == 1
    assert all(len(f.listing.regular) == len(FILES) for f in found)


def test_o51_without_a_tight_bound_the_same_readings_do_overlap_at_one_provider(
    fake, broken, client, config, platform_file, ticker, tmp_path
):
    """O51: the check above can fail: with the default bound the two readings' eight workers do overlap at beta. Each
    read at beta is held until three are in flight together (a latch, no timing): the latch opens, so three were."""
    first, second = (bounded(client, config, platform_file, ticker, tmp_path, 16) for _ in range(2))
    broken.latch_at = 3
    consistency.bounded_map(lambda one: holdings(one, fake), [first, second], 2)
    assert broken.latch.is_set()
    assert broken.peak >= 3


# -- the occupancy's counter, each stage's transfer reads, and `summaries` mode's listings ----------------------------


def test_o46_the_occupancy_counter_read_tries_a_connection_failure_again(p, fake, broken):
    """O46: the space root's distribution, read on the data provider beside the files, fails to connect once and is
    asked again, under the reading's policy: the reading passes and its tally holds the retry at the data provider."""
    broken.on(nth(COUNTER, 1), CONNECT)
    reading = holdings(p, fake)
    assert reading.held("beta") == HELD
    assert reading.read_retry["providers"]["providers[0]"]["errors"] == {"ConnectError": 1}


def test_o46_an_occupancy_counter_that_never_connects_fails_the_reading_closed(p, fake, broken):
    """O46: a counter read that fails on every attempt leaves the reading without its counter: it raises, with the
    tally, and is never taken for a zero."""
    broken.on(COUNTER, CONNECT, times=None)
    with pytest.raises(PlatformError, match=r"after 4 attempts") as caught:
        holdings(p, fake)
    assert caught.value.facts["read_retry"]["providers"]["providers[0]"]["failed"] == 1


def test_o46_the_transfers_stages_listing_tries_a_connection_failure_again(p, broken, ticker):
    """O46: the first transfer listing the transfers stage reads fails to connect once; it is asked again, the reset
    passes, and the stage's record tallies its own transfer reads (`transfer_read_retry`)."""
    broken.on(nth(LISTINGS, 1), CONNECT)
    record = run(p, ticker)
    tally = record["stages"]["transfers"]["transfer_read_retry"]
    assert (tally["errors"], tally["retries"], tally["failed"]) == ({"ConnectError": 1}, 1, 0)


def test_o46_a_transfer_listing_that_never_connects_fails_the_reset_closed(p, broken, ticker):
    """O46: a listing that never connects shows nothing the stage can trust: the transfers stage fails, never settles,
    and its record holds the tally."""
    broken.on(LISTINGS, CONNECT, times=None)
    with pytest.raises(ResetFailed) as caught:
        run(p, ticker)
    assert caught.value.stage == "transfers"
    assert caught.value.record["stages"]["transfers"]["transfer_read_retry"]["failed"] >= 1


def test_o46_the_eviction_stages_wait_for_its_transfer_tries_a_connection_failure_again(p, broken, ticker):
    """O46: the eviction stage's wait for its own job polls the job's record; one read that fails to connect is asked
    again, the eviction is verified and the reset passes; the stage's record tallies it."""
    broken.on(nth(RECORDS, 1), CONNECT)
    record = run(p, ticker, evict={"beta": [FILES[0]]})
    assert record["ok"] is True
    assert record["stages"]["eviction"]["transfer_read_retry"]["errors"] == {"ConnectError": 1}


def test_o46_an_eviction_whose_transfer_record_never_connects_fails_the_reset_closed(p, broken, ticker):
    """O46: a job whose record never answers is not known to have ended: the eviction stage fails, with its tally."""
    broken.on(RECORDS, CONNECT, times=None)
    with pytest.raises(ResetFailed) as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert caught.value.record["stages"]["eviction"]["transfer_read_retry"]["failed"] >= 1


def test_o46_the_historys_reading_of_the_global_clock_tries_a_connection_failure_again(p, broken, ticker):
    """O46: Onezone's current-time read, which places the history's window on the global clock, fails to connect once
    and is asked again: the history stage passes and tallies it."""
    broken.on(nth(zone_clock, 1), CONNECT)
    record = run(p, ticker)
    assert record["stages"]["history"]["transfer_read_retry"]["errors"] == {"ConnectError": 1}


def test_o46_a_global_clock_that_never_connects_fails_the_history_stage(p, broken, ticker):
    """O46: a clock that cannot be read places nothing: the history stage fails, as it did, now after the retries."""
    broken.on(zone_clock, CONNECT, times=None)
    with pytest.raises(ResetFailed) as caught:
        run(p, ticker)
    assert caught.value.stage == "history"
    assert caught.value.record["stages"]["history"]["transfer_read_retry"]["failed"] >= 1


@pytest.fixture
def summaries(client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan="summaries")


def test_o33_a_summaries_scans_directory_listing_tries_a_connection_failure_again(summaries, broken, ticker):
    """O33: with `qos_scan = "summaries"` the scan walks the space by its directory listings; the first fails to
    connect once and is asked again, under the scan's policy, and the qos stage's record tallies it."""
    broken.on(nth(CHILDREN, 1), CONNECT)
    record = run(summaries, ticker)
    assert record["stages"]["qos"]["read_retry"]["errors"] == {"ConnectError": 1}


def test_o33_a_summaries_scan_whose_listing_never_connects_fails_the_reset_closed(summaries, broken, ticker):
    """O33: a directory listing that never connects leaves the walk incomplete: the qos stage fails, with the tally."""
    broken.on(CHILDREN, CONNECT, times=None)
    with pytest.raises(ResetFailed) as caught:
        run(summaries, ticker)
    assert caught.value.stage == "qos"
    assert caught.value.record["stages"]["qos"]["read_retry"]["failed"] >= 1
