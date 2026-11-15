"""A reset's mutation-gate reads, its eviction path listings, its `verify_space` and its first read of the space root
try a connection failure again under the reset's policy, and no mutation is ever sent twice (O09, O29, O46).

#306 gave a reset's per-file reads a policy that tries a connection failure again; these reads kept timeouts only, and
the gate makes one before every cancel, eviction and QoS deletion, thousands in a large eviction. Each now reads under
the stage's policy and fails closed when its reads are spent: the gate sends nothing after its last read
(`ScopeUnread`). The mutation itself is sent once: one whose answer is lost after it went out may have been applied,
so it is never sent again, and the reset reads the state and decides. The federation is the public fake behind a
transport that breaks chosen requests; its clock is the test's ticker, so no pause really waits.
"""

import collections
import re
import threading
from contextlib import contextmanager

import httpx
import pytest
from qos_world import FILES, federation, provisioner_with, two_providers

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.eviction import evict_ids
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform

HELD = len(FILES) * len(DATA)  # what beta holds of the space: every file of `study`
PAUSES = [2.0, 5.0, 15.0]


class Broken:
    """The fake behind rules: a request a rule matches gets the rule's outcome, `times` times (None: always); every
    other request is the fake's. Every request is kept, in order (`sent`)."""

    def __init__(self, respond):
        self.respond = respond
        self.rules: list[list] = []
        self.sent: list[httpx.Request] = []
        self._lock = threading.Lock()

    def on(self, matches, outcome, times=1):
        self.rules.append([matches, outcome, times])

    def __call__(self, request):
        with self._lock:
            self.sent.append(request)
            rule = next((r for r in self.rules if r[0](request) and (r[2] is None or r[2] > 0)), None)
            if rule is not None and rule[2] is not None:
                rule[2] -= 1
        return rule[1](request) if rule is not None else self.respond(request)

    def count(self, matches):
        return sum(1 for request in self.sent if matches(request))


def route(method, pattern, host=None, query=None):
    """A matcher of the requests of `method` whose API path matches `pattern`, on `host` and with exactly `query` when
    given."""

    def matches(request):
        path = re.sub(r"^/api/v3/one(provider|zone)", "", request.url.path)
        return (
            request.method == method
            and re.fullmatch(pattern, path) is not None
            and (host is None or request.url.host == host)
            and (query is None or request.url.query.decode() == query)
        )

    return matches


def nth(matches, number, *, onward=False):
    """A matcher of the `number`th request `matches` accepts (and of every later one, `onward`), of no other."""
    seen = []

    def which(request):
        if not matches(request):
            return False
        seen.append(request)
        return len(seen) == number or (onward and len(seen) > number)

    return which


def raising(kind):
    def outcome(request):
        raise kind("simulated", request=request)

    return outcome


def in_turn(*outcomes):
    """Each outcome once, in order, for the requests a rule matches."""
    left = list(outcomes)
    return lambda request: left.pop(0)(request)


def answering(status, body):
    return lambda request: httpx.Response(status, json=body)


def applied_then_lost(fake, then=None):
    """The fake applies the request (and `then` is given its answer), and the answer is lost on the way back."""

    def outcome(request):
        answer = fake.respond(request)
        if then is not None:
            then(answer)
        raise httpx.ReadError("simulated", request=request)

    return outcome


ZONE_SPACE = route("GET", r"/spaces/[^/]+", "zone.test")  # a space's name on Onezone: `verify_space`, the gate's last
ROOT = route("GET", r"/spaces/[^/]+", "alpha.test")  # the space root on the data provider
CHILDREN = route("GET", r"/data/[^/]+/children")  # a directory listing (the QoS scan here reads the recursive one)
PAGES = route("GET", r"/data/[^/]+/files")  # the QoS scan's recursive listing


def parent_reads(host):
    """The gate's walk up a file's parent links on `host`: a read the gate alone makes."""
    return route("GET", r"/data/[^/]+", host, query="attribute=parent_id")


RECORDS = route("GET", r"/transfers/[^/]+")  # a job's record: the cancel's gate reads it first
EVICTIONS = route("POST", r"/transfers")
CANCELS = route("DELETE", r"/transfers/[^/]+", "alpha.test")
DELETIONS = route("DELETE", r"/qos_requirements/[^/]+")
CONNECT = raising(httpx.ConnectError)


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


def run(p, ticker, **options):
    return SpaceReset(p, now=ticker.clock).run(allowed={"beta": HELD}, **options)


def stage_of(record, stage):
    return record["stages"][stage]


def evictions(fake):
    return [job for job in fake.transfers.values() if job.kind == "eviction"]


# The three mutations of a reset, each with what sets it up, the read only its gate makes, and its stage's tally.
def _evict_setup(fake, ticker):
    return {"evict": {"beta": [FILES[0]]}}


def _cancel_setup(fake, ticker):
    fake.add_transfer("replication", FILES[2], BETA_ID, scheduled=ticker.now, ended=False)
    return {}


def _delete_setup(fake, ticker):
    fake.add_qos(FILES[1])
    return {}


MUTATIONS = {
    "eviction": (_evict_setup, parent_reads("beta.test"), EVICTIONS, "eviction", "transfer_read_retry"),
    "cancel": (_cancel_setup, RECORDS, CANCELS, "transfers", "transfer_read_retry"),
    "qos-deletion": (_delete_setup, parent_reads("alpha.test"), DELETIONS, "qos", "read_retry"),
}


# -- 1. the mutation gate's reads -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(MUTATIONS))
def test_o46_a_gate_read_that_fails_to_connect_once_is_tried_again_and_the_mutation_is_sent_once(
    kind, p, fake, broken, ticker
):
    """O46: the first read only the gate makes before a cancel, an eviction or a QoS deletion fails to connect; it is
    asked again under the stage's policy, the gate passes, the mutation is sent once, the reset passes, and the
    stage's record tallies the retry."""
    setup, gate_read, mutation, stage, key = MUTATIONS[kind]
    options = setup(fake, ticker)
    broken.on(nth(gate_read, 1), CONNECT)
    record = run(p, ticker, **options)
    tally = stage_of(record, stage)[key]
    assert record["ok"] is True
    assert (tally["errors"], tally["retries"], tally["failed"]) == ({"ConnectError": 1}, 1, 0)
    assert broken.count(mutation) == 1
    assert 2.0 in ticker.slept


@pytest.mark.parametrize("kind", list(MUTATIONS))
def test_o09_a_gate_whose_reads_never_connect_fails_closed_and_sends_nothing_after_its_last_read(
    kind, p, fake, broken, ticker
):
    """O09: a gate read that fails to connect on every attempt leaves the target unread: after the fourth attempt the
    gate refuses the mutation unsent (`ScopeUnread`), nothing is sent, and the stage fails with its tally."""
    setup, gate_read, mutation, stage, key = MUTATIONS[kind]
    options = setup(fake, ticker)
    broken.on(gate_read, CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"the gate could not read the target's space") as caught:
        run(p, ticker, **options)
    failed = caught.value.record["stages"][stage]
    assert (caught.value.stage, failed["ok"]) == (stage, False)
    assert failed[key]["failed"] == 1
    assert failed[key]["errors"] == {"ConnectError": 4}
    assert broken.count(gate_read) == 4
    assert broken.count(mutation) == 0  # nothing after the last read
    assert set(PAUSES) <= set(ticker.slept)


def test_o09_each_gate_attempt_decides_from_its_own_answer(p, fake, broken, ticker):
    """O09: the gate's read of the target space's name fails to connect, and the retry's answer names a space outside
    the prefixes: that answer decides, the eviction is refused unsent, and nothing the first attempt began is kept."""
    # Onezone's space reads: the driver's `verify_space`, the eviction stage's, then the eviction gate's
    broken.on(nth(ZONE_SPACE, 3, onward=True), in_turn(CONNECT, answering(200, {"name": "elsewhere"})), times=2)
    with pytest.raises(ResetFailed, match=r"another space than the one the request is bound to") as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert broken.count(EVICTIONS) == 0
    assert evictions(fake) == []


# -- 2. the eviction stage's path listings -----------------------------------------------------------------------------

LISTINGS = {  # the plan, and which directory listing of the stage is whose: `resolve_many` lists first, then `walk`
    "resolve_many": ([FILES[0]], 1),
    "walk": (["study"], 2),
}


@pytest.mark.parametrize("which", list(LISTINGS))
def test_o46_an_eviction_path_listing_that_fails_to_connect_once_is_tried_again(which, p, fake, broken, ticker):
    """O46: the eviction stage lists the plan's paths (`resolve_many`) and a tree's directories (`walk`); a listing
    that fails to connect once is asked again under the stage's policy, the files are evicted and the reset passes."""
    paths, number = LISTINGS[which]
    broken.on(nth(CHILDREN, number), CONNECT)
    record = run(p, ticker, evict={"beta": paths})
    tally = stage_of(record, "eviction")["transfer_read_retry"]
    assert record["ok"] is True
    assert (tally["errors"], tally["failed"]) == ({"ConnectError": 1}, 0)
    assert fake.held(FILES[0], BETA_ID) == []


@pytest.mark.parametrize("which", list(LISTINGS))
def test_o46_an_eviction_path_listing_that_never_connects_fails_the_reset_closed(which, p, fake, broken, ticker):
    """O46: a listing that fails on every attempt leaves the plan unresolved, or the tree unlisted: the eviction stage
    fails with its tally and evicts nothing."""
    paths, number = LISTINGS[which]
    broken.on(nth(CHILDREN, number, onward=True), CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"after 4 attempts") as caught:
        run(p, ticker, evict={"beta": paths})
    assert caught.value.stage == "eviction"
    assert caught.value.record["stages"]["eviction"]["transfer_read_retry"]["failed"] == 1
    assert broken.count(EVICTIONS) == 0


# -- 3. verify_space ---------------------------------------------------------------------------------------------------


def test_o46_the_drivers_verify_space_tries_a_connection_failure_again(p, broken, ticker):
    """O46: the space's live name, read before any stage, fails to connect once; it is asked again, the reset passes,
    and the record tallies it (`verify_read_retry`)."""
    broken.on(nth(ZONE_SPACE, 1), CONNECT)
    record = run(p, ticker)
    tally = record["verify_read_retry"]
    assert record["ok"] is True
    assert (tally["reads"], tally["errors"], tally["retries"]) == (1, {"ConnectError": 1}, 1)
    assert tally["policy"]["transport"] is True


def test_o46_a_verify_space_that_never_connects_fails_before_any_stage(p, broken, ticker):
    """O46: a name that cannot be read verifies nothing: the reset raises before any stage reads or changes anything,
    with the tally on the error."""
    broken.on(ZONE_SPACE, CONNECT, times=None)
    with pytest.raises(PlatformError, match=r"after 4 attempts") as caught:
        run(p, ticker)
    assert caught.value.facts["verify_read_retry"]["failed"] == 1
    assert broken.count(PAGES) == 0


def test_o46_the_eviction_stages_verify_space_tries_a_connection_failure_again(p, broken, ticker):
    """O46: the eviction verifies the space again before it sends anything; that read fails to connect once and is
    asked again under the stage's policy."""
    broken.on(nth(ZONE_SPACE, 2), CONNECT)  # the driver's is the first
    record = run(p, ticker, evict={"beta": [FILES[0]]})
    assert record["ok"] is True
    assert stage_of(record, "eviction")["transfer_read_retry"]["errors"] == {"ConnectError": 1}


def test_o46_an_eviction_stages_verify_space_that_never_connects_fails_the_stage(p, broken, ticker):
    """O46: the same read failing on every attempt fails the eviction stage before anything is sent."""
    broken.on(nth(ZONE_SPACE, 2, onward=True), CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"after 4 attempts") as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert broken.count(EVICTIONS) == 0


# -- 4. the first read of the space root -------------------------------------------------------------------------------

ROOT_READS = {  # the plan, the stage that reads the root first (after the QoS scan's own read), and its tally
    "eviction": ({"evict": {"beta": [FILES[0]]}}, "eviction", "transfer_read_retry"),
    "occupancy": ({}, "occupancy", "read_retry"),
}


@pytest.mark.parametrize("which", list(ROOT_READS))
def test_o46_the_first_read_of_the_space_root_tries_a_connection_failure_again(which, p, broken, ticker):
    """O46: the provisioner reads the space root once and keeps it; that first read, the eviction stage's or, with no
    plan, the occupancy's, fails to connect once and is asked again under the stage's policy, and the reset passes."""
    options, stage, key = ROOT_READS[which]
    broken.on(nth(ROOT, 2), CONNECT)  # the QoS scan's own read of the root is the first
    record = run(p, ticker, **options)
    assert record["ok"] is True
    assert stage_of(record, stage)[key]["errors"] == {"ConnectError": 1}


@pytest.mark.parametrize("which", list(ROOT_READS))
def test_o46_a_first_read_of_the_space_root_that_never_connects_fails_the_reset_closed(which, p, broken, ticker):
    """O46: a root that cannot be read is not guessed: the stage that needs it fails, with its tally."""
    options, stage, key = ROOT_READS[which]
    broken.on(nth(ROOT, 2, onward=True), CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"after 4 attempts") as caught:
        run(p, ticker, **options)
    assert caught.value.stage == stage
    assert caught.value.record["stages"][stage][key]["failed"] >= 1
    assert broken.count(EVICTIONS) == 0


# -- 5. a mutation whose answer is lost is never sent again ------------------------------------------------------------


@pytest.mark.parametrize("paths", [[FILES[0]], ["study"]], ids=["file", "tree"])
def test_o09_an_eviction_applied_and_its_answer_lost_is_not_sent_again_and_counts_as_done(
    paths, p, fake, broken, ticker
):
    """O09: the provider applies the eviction and its answer is lost on the way back. The eviction is not sent again:
    the stage waits for no job to run, reads the block maps, finds the file evicted, and the reset passes; the
    transfer listing holds one eviction job, and the record counts the lost answer."""
    broken.on(EVICTIONS, applied_then_lost(fake, lambda answer: fake.end_transfer(answer.json()["transferId"])))
    record = run(p, ticker, evict={"beta": paths})
    assert record["ok"] is True
    assert broken.count(EVICTIONS) == 1
    assert len(evictions(fake)) == 1
    assert fake.held(FILES[0], BETA_ID) == []
    assert stage_of(record, "eviction")["providers"]["providers[1]"]["unanswered"] == 1


def test_o09_an_eviction_whose_answer_was_lost_and_never_applied_is_not_sent_again_and_fails_closed(
    p, fake, broken, ticker
):
    """O09: an eviction that failed to connect may never have reached the provider, but the reset cannot tell: it is
    never sent again, the block map still shows the file held after the last attempt's bound, and the stage fails."""
    broken.on(EVICTIONS, CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"still hold bytes") as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert broken.count(EVICTIONS) == 1
    assert evictions(fake) == []
    assert caught.value.record["stages"]["eviction"]["providers"]["providers[1]"]["unanswered"] == 1


def test_o09_a_cancel_applied_and_its_answer_lost_is_not_sent_again_and_the_jobs_record_decides(
    p, fake, broken, ticker
):
    """O09: the provider cancels the job and the answer is lost. The cancel is not sent again: the job's record shows
    it ended, the listings settle, and the reset passes, counting the lost answer."""
    tid = fake.add_transfer("replication", FILES[2], BETA_ID, scheduled=ticker.now, ended=False)
    broken.on(CANCELS, applied_then_lost(fake))
    record = run(p, ticker)
    assert record["ok"] is True
    assert broken.count(CANCELS) == 1
    assert getattr(fake.transfers[tid], "cancelled", False) is True
    assert stage_of(record, "transfers")["unanswered"] == 1


def test_o09_a_qos_deletion_applied_and_its_answer_lost_is_not_sent_again_and_the_barrier_decides(
    p, fake, broken, ticker
):
    """O09: the provider deletes the requirement and the answer is lost. The deletion is not sent again: the barrier
    reads every provider until each has processed it, the second scan finds none, and the reset passes."""
    qid = fake.add_qos(FILES[1])
    broken.on(DELETIONS, applied_then_lost(fake))
    record = run(p, ticker)
    assert record["ok"] is True
    assert broken.count(DELETIONS) == 1
    assert qid not in fake.qos
    assert stage_of(record, "qos")["unanswered"] == 1


def test_o09_a_qos_deletion_whose_answer_was_lost_and_never_applied_is_not_sent_again_and_fails_closed(
    p, fake, broken, ticker
):
    """O09: a deletion that may never have arrived is not sent again either: the requirement stays listed, the
    barrier fails at its bound, and the qos stage fails."""
    qid = fake.add_qos(FILES[1])
    broken.on(DELETIONS, CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"still listed after the deletions") as caught:
        run(p, ticker)
    assert caught.value.stage == "qos"
    assert broken.count(DELETIONS) == 1
    assert qid in fake.qos


def test_o29_an_instruments_read_of_the_root_or_a_path_takes_no_policy(p, broken):
    """O29: `root(wait=False)` and `resolve(wait=False)` are an instrument's reads, one attempt each: handed a reset's
    policy they refuse it before anything is read, so no measurement can be turned into a retried read that way."""
    with pytest.raises(ValueError, match=r"takes no policy"):
        p.root(wait=False, retry=p.reading_retry())
    with pytest.raises(ValueError, match=r"takes no policy"):
        p.resolve(FILES[0], wait=False, retry=p.reading_retry())
    assert broken.sent == []


VISIBILITY_SIZE = route("GET", r"/data/[^/]+", "alpha.test", query="attribute=name&attribute=size&attribute=parent_id")


def test_o09_a_read_that_fails_before_the_eviction_is_sent_is_a_failed_read_never_a_lost_eviction(
    p, fake, broken, ticker
):
    """O09: the size read of the visibility wait before an eviction fails to connect. Nothing was sent, so it is a
    failed read and fails the stage, never an eviction whose answer was lost: beta holds nothing of the file, and the
    stage does not pass on the empty map as though an eviction had gone out."""
    fake.blocks.pop((fake.id_of(FILES[0]), BETA_ID))
    broken.on(VISIBILITY_SIZE, CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"GET failed \(ConnectError\)") as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert broken.count(EVICTIONS) == 0


def test_o09_an_eviction_without_a_quiet_check_raises_a_lost_answer_as_before(p, fake, broken):
    """O09: only a caller that checks no transfer is running before the block maps are read (the reset's `quiet`) can
    judge a lost eviction by the maps; without one, a lost answer is raised as before, after one request."""
    broken.on(EVICTIONS, applied_then_lost(fake))
    with pytest.raises(PlatformError, match=r"POST failed \(ReadError\)"):
        evict_ids(p, [fake.id_of(FILES[0])], "beta")
    assert broken.count(EVICTIONS) == 1


# -- 6. the visibility wait before each eviction -----------------------------------------------------------------------

VISIBILITY_POLL = route("GET", r"/data/[^/]+", "beta.test", query="attribute=name&attribute=size&attribute=parent_id")
VISIBILITY = {  # the file's size on the data provider, then the wait's reads at the evicting provider
    "size": VISIBILITY_SIZE,
    "wait": VISIBILITY_POLL,
}


@pytest.mark.parametrize("which", list(VISIBILITY))
def test_o46_a_visibility_read_before_an_eviction_tries_a_connection_failure_again(which, p, fake, broken, ticker):
    """O46: before each eviction the reset waits until the evicting provider sees the file at its size, two reads per
    file, thousands in a large reset. One that fails to connect is asked again under the eviction stage's policy, the
    eviction is sent once and verified, and the stage's record tallies the retry."""
    broken.on(nth(VISIBILITY[which], 1), CONNECT)
    record = run(p, ticker, evict={"beta": [FILES[0]]})
    tally = stage_of(record, "eviction")["transfer_read_retry"]
    assert record["ok"] is True
    assert (tally["errors"], tally["retries"], tally["failed"]) == ({"ConnectError": 1}, 1, 0)
    assert broken.count(EVICTIONS) == 1
    assert fake.held(FILES[0], BETA_ID) == []


@pytest.mark.parametrize("which", list(VISIBILITY))
def test_o46_a_visibility_read_that_never_connects_fails_the_eviction_closed(which, p, fake, broken, ticker):
    """O46: a visibility read that fails on every attempt is a failed read: after the fourth the eviction stage fails
    with its tally, and no eviction is sent."""
    broken.on(VISIBILITY[which], CONNECT, times=None)
    with pytest.raises(ResetFailed, match=r"GET failed \(ConnectError\) after 4 attempts") as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    tally = caught.value.record["stages"]["eviction"]["transfer_read_retry"]
    assert caught.value.stage == "eviction"
    assert (tally["failed"], tally["errors"]) == (1, {"ConnectError": 4})
    assert broken.count(VISIBILITY[which]) == 4
    assert broken.count(EVICTIONS) == 0


@pytest.mark.parametrize("which", list(VISIBILITY))
def test_o46_an_error_answer_to_a_visibility_read_fails_at_once_and_is_never_asked_again(
    which, p, fake, broken, ticker
):
    """O46: an answer decides at once: a 500 to a visibility read is never asked again under the reset's policy, the
    stage fails after one request and no pause, and nothing is sent."""
    broken.on(VISIBILITY[which], answering(500, {"error": {"id": "internalServerError"}}), times=None)
    with pytest.raises(ResetFailed) as caught:
        run(p, ticker, evict={"beta": [FILES[0]]})
    assert caught.value.stage == "eviction"
    assert broken.count(VISIBILITY[which]) == 1
    assert broken.count(EVICTIONS) == 0
    assert caught.value.record["stages"]["eviction"]["transfer_read_retry"]["retries"] == 0


def test_o51_each_visibility_read_holds_its_providers_slot(p, fake, broken, ticker, monkeypatch):
    """O51: the visibility reads are plain reads of the reset: each is made while it holds one of its provider's slots
    (`Provisioner.reading_slot`), the size read the data provider's and the wait's reads the evicting provider's."""
    held: collections.Counter = collections.Counter()
    original = p.reading_slot

    @contextmanager
    def spy(provider, reads):
        with original(provider, reads):
            held[provider] += 1
            try:
                yield
            finally:
                held[provider] -= 1

    monkeypatch.setattr(p, "reading_slot", spy)
    seen = []

    def noting(request):
        seen.append((request.url.host, held[request.url.host.removesuffix(".test")]))
        return fake.respond(request)

    broken.on(VISIBILITY_SIZE, noting, times=None)
    broken.on(VISIBILITY_POLL, noting, times=None)
    assert run(p, ticker, evict={"beta": [FILES[0]]})["ok"] is True
    assert sorted(seen) == [("alpha.test", 1), ("beta.test", 1)]
