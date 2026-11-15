"""O35: the fault gate's health check heals stuck pairs its own link check found, under its own exclusive admission.

No injection is open, so no restore will heal them: the pattern seen live is a provider whose rtransfer link crashed
(`providers[0]`), and a reader whose pair reading from it never reconnected (`providers[2]`). The gate's own all-pairs
link check (`GateCheck`, as `PlatformHealth` runs it inside the gate) finds the pair, and the gate heals it before any
attempt launches anything, with the restore heal's rules: the modes of `[faults] heal`, its bounds, the enforced
no-fetch settle, fresh pair reads around each action, its record, and failing closed.
"""

import contextvars
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from campaign_support import keys_under, planned_store
from fault_support import SERVICES, FakeClock, FakeServices, faults_table
from heal_support import NAMES, STORES, four_providers, heal_margin_s

from expcore.campaign.healthgate import DEFER, FOUND, GO, Health, HealthGate
from expcore_onedata import health as platform_health
from expcore_onedata.admission import OUTAGE_RUN, RECOVERY, FederationLock
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_gate_heal import GateHeals
from expcore_onedata.fault_journal import InjectionJournal, holder
from expcore_onedata.fault_matrix import LinkMatrix, PairState
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer

CRASHED, OTHER, READER, SECOND = (f"providers[{index}]" for index in range(4))
EVERY = (CRASHED, OTHER, READER, SECOND)


class Federation(FakeServices):
    """Providers' services with scripted pairs, by label. `stuck` holds the failing pairs as (reader, source) whose
    fetch failed; `untested` those whose probe could not be made ready (failing, with no stuck link). A link restart of
    a reader mends its own stuck pairs unless `link_fixes[reader]` says what it mends; a container restart of a reader
    in `container_fixes` mends its pairs. `reads` notes every all-pairs check with the clock's time: `gate` for the
    gate's own, `heal` for the heal's; `actions` every ping, link restart, stop, start and arming of the session
    gate."""

    def __init__(self, stores=None):
        stores = dict(stores or STORES)
        super().__init__(tuple(stores.values()))
        self.label = {service: f"providers[{index}]" for index, service in enumerate(stores.values())}
        self.stuck: set[tuple[str, str]] = {(READER, CRASHED)}
        self.untested: set[tuple[str, str]] = set()
        self.link_fixes: dict[str, tuple[tuple[str, str], ...]] = {}
        self.container_fixes: set[str] = set()
        self.clock: FakeClock | None = None
        self.ping_s = 0.0
        self.on_ping = None  # called with the provider's label while it is pinged
        self.on_read = None  # called before each of the heal's own reads of every pair
        self.on_restart = None  # called at each link restart, before it mends anything
        self.restart_s = 0.0  # real seconds a link restart takes: a heal that outlasts the core's wait for a check
        self.actions: list[tuple[str, str, float]] = []
        self.reads: list[tuple[str, float]] = []

    def now(self) -> float:
        return self.clock.now if self.clock is not None else 0.0

    def matrix(self) -> LinkMatrix:
        labels = list(self.label.values())
        failing = self.stuck | self.untested
        pairs = tuple(
            PairState(reader, source, (reader, source) not in failing, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = ", ".join(f"{source}->{reader}" for reader, source in sorted(failing))
        return LinkMatrix(not failing, f"stuck: {shown}" if shown else "every pair passed", pairs)

    def _note(self, verb: str, service: str) -> None:
        self.actions.append((verb, self.label[service], self.now()))

    def link_matrix(self, service):
        self.reads.append(("heal", self.now()))
        if self.on_read is not None:
            self.on_read()
        return self.matrix()

    def active_transfers(self):
        return 0

    def sessions_ready(self, service, gate_probe):
        return None

    def arm_gate(self, service):
        self._note("arm_gate", service)
        return "gate-probe-id"

    def ping(self, service):
        self._note("ping", service)
        if self.clock is not None:
            self.clock.sleep(self.ping_s)
        if self.on_ping is not None:
            self.on_ping(self.label[service])
        return True

    def restart_link(self, service):
        self._note("restart_link", service)
        if self.on_restart is not None:
            self.on_restart()
        time.sleep(self.restart_s)
        reader = self.label[service]
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck -= mended
        return "ok"

    def stop(self, service):
        self._note("stop", service)
        super().stop(service)

    def start(self, service):
        self._note("start", service)
        super().start(service)
        if self.label[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != self.label[service]}


class GateCheck:
    """The rest of the platform's health check as `PlatformHealth` runs it inside the gate: the space (`space_ok`),
    then its own link check of every pair. `take` hands the gate the report a call made (once), `expire` makes the next
    call check the links again. With `cache`, as `PlatformHealth` within `every_s`, a call whose last report passed and
    was not expired answers with it and checks nothing. `grow` adds stuck pairs before the check of that number, as a
    pair that sticks between two checks; `report` is the latest report, whoever made it."""

    def __init__(self, federation: Federation) -> None:
        self.federation, self.report, self.made, self.runs = federation, None, None, 0
        self.space_ok, self.cache, self.expired = True, False, False
        self.grow: dict[int, set[tuple[str, str]]] = {}
        self.on_check = None  # called at each check, while the gate holds the federation for it

    def take(self) -> LinkMatrix | None:
        made, self.made = self.made, None
        return made

    def expire(self) -> None:
        self.expired = True

    def __call__(self) -> Health:
        self.runs += 1
        if self.on_check is not None:
            self.on_check()
        if not self.space_ok:
            return Health(False, "the providers supporting the space differ from the declared set")
        if self.cache and self.report is not None and self.report.ok and not self.expired:
            return Health(True, "every provider pair transferred (the last check, still recent)")
        self.expired = False
        self.federation.stuck |= self.grow.get(self.runs, set())
        self.federation.reads.append(("gate", self.federation.now()))
        self.report = self.made = self.federation.matrix()
        if self.report.ok:
            return Health(True, "every provider pair transferred")
        return Health(False, f"no run is admitted until every provider link transfers: {self.report.detail}")


def gate_of(tmp_path, federation=None, **faults):
    """The fault gate around `GateCheck`, as `PlatformHealth` builds it, on a fake clock that is also its wall: four
    providers (`heal_support.four_providers`, with the token margin a declared heal needs) and a short wait for the
    federation."""
    tmp_path.mkdir(parents=True, exist_ok=True)  # a test may build a second federation under a subdirectory
    platform = write_platform(tmp_path, lambda data: four_providers(data, **{"health_wait_s": 0.2, **faults}))
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    federation = federation or Federation()
    clock = FakeClock()
    federation.clock = clock
    check = GateCheck(federation)
    labels = {config.provider_label(name): service for name, service in settings.services.items()}
    gate = FaultGate(
        settings,
        federation,
        check,
        labels=labels,
        links=check.take,
        relink=check.expire,
        clock=clock,
        sleep=clock.sleep,
        wall=clock,
    )
    return gate, federation, check, settings


def acted(federation, *verbs):
    return [(verb, label) for verb, label, _ in federation.actions if verb in verbs]


def heal_lines(settings):
    path = settings.journal / "gate-heals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


ANY_ACTION = ("arm_gate", "ping", "restart_link", "stop", "start")
UNCHARGED_WAIT = (False, True, False)  # (ok, deferred, charge): the hold a heal in progress answers


def healed(gate, federation):
    """The check that finds the stuck pair begins the heal and answers the uncharged deferral; once the heal has ended
    and its settle's drain record has passed, the answer of the next check."""
    first = gate()
    assert (first.ok, first.deferred, first.charge) == UNCHARGED_WAIT, first.detail
    assert gate.wait_for_heal(10)
    federation.clock.sleep(600)  # past the drain record each restart's settle wrote
    return gate()


def core_gate(tmp_path, check, **options):
    """The core's health gate around `check`, over a small planned campaign: the gate, its store and a run's key."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, schema, manifest = planned_store(campaign)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    core = HealthGate(store, check, lambda: next(clock), **options)
    core.load()
    return core, store, keys_under(schema, manifest, "mount-read")[0]


# --- it heals what its own check found stuck --------------------------------------------------------------------


def test_o35_a_stuck_pair_at_the_health_check_is_healed_by_all_links_and_the_gate_then_opens(tmp_path):
    """O35: the gate's own link check finds a stuck pair and no injection is open; the gate takes the federation
    exclusively, restarts every provider's link once, in label order, with nothing fetched in between, settles, reads
    every pair afresh, and answers healthy only once its own check passes again."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    reading = healed(gate, federation)
    assert reading.ok is True, reading.detail
    assert acted(federation, "restart_link") == [("restart_link", label) for label in EVERY]
    assert acted(federation, "stop", "start") == []
    assert check.runs == 3  # the check that found the stuck pair, the heal's own after it, and the next one
    assert gate.last_heal["outcome"] == "healed"


def test_o35_a_heal_longer_than_the_health_timeout_that_mends_the_pair_charges_no_run(tmp_path):
    """O35: the heal outlasts the core's wait for a check (`health_timeout_s`); the check that found the stuck pair
    answers the uncharged deferral at once and the heal runs on, so the run is deferred, not charged, the gate stays
    open, and once the pair is mended the next check lets the run go, its attempt never consumed."""
    federation = Federation()
    federation.restart_s = 0.3  # four link restarts take 1.2 s against the core's 0.5 s wait
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links")
    core, store, key = core_gate(tmp_path, gate, timeout_s=0.5)
    assert core.before_run(key).verdict == DEFER
    assert core.closed is False
    assert gate.wait_for_heal(10)
    assert gate.last_heal["outcome"] == "healed"
    federation.clock.sleep(600)
    assert core.before_run(key).verdict == GO
    assert store.q("SELECT COUNT(*) FROM attempts")[0][0] == 0


def test_o35_a_failed_heal_still_closes_the_gate(tmp_path):
    """O35: a heal that leaves the pair stuck charges nothing while it runs, and the next check finds the pair still
    stuck with the latch set: the gate closes, as for any stuck link, and no second heal runs."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links", heal_container_fallback=False)
    core, _, key = core_gate(tmp_path, gate, timeout_s=5)
    assert core.before_run(key).verdict == DEFER
    assert gate.wait_for_heal(10)
    federation.clock.sleep(600)
    after = core.before_run(key)
    assert after.verdict == FOUND
    assert "until a link check passes" in after.detail
    assert core.closed is True
    assert len(acted(federation, "restart_link")) == 4


def test_o35_a_check_that_meets_another_checks_heal_waits_uncharged_instead_of_the_latch(tmp_path):
    """O35: this check found the stuck pair too, and by the time it would heal, another check's heal holds the
    federation (its holder record, alive) and has set the latch: it answers the uncharged deferral, not the latch's
    charged refusal."""
    gate, federation, check, settings = gate_of(tmp_path, heal="all_links")
    journal = InjectionJournal(settings.journal)

    def another_heal_begins():  # while this check reads its pairs: another check's heal takes the federation
        GateHeals(settings.journal).append({"event": "healing", "id": "another-check", "mode": "all_links"})
        since = datetime.now(UTC).isoformat(timespec="seconds")
        journal.write_holder(
            {"token": "another-check", **holder(), "purpose": RECOVERY, "target": None, "since_utc": since}
        )

    check.on_check = another_heal_begins
    reading = gate()
    journal.clear_holder("another-check")
    assert (reading.ok, reading.deferred, reading.charge) == UNCHARGED_WAIT, reading.detail
    assert acted(federation, *ANY_ACTION) == []


def test_o35_another_process_waits_uncharged_while_a_heals_container_restart_is_unverified(tmp_path):
    """O35: a heal's container restart that was not verified is journalled `clear_failed` while the heal still holds
    the federation for its settle; another process's check that times out on the federation then waits, uncharged,
    instead of failing for a restore that is the heal's own and not over yet."""
    gate, _, _, settings = gate_of(tmp_path, heal="all_links")
    journal = InjectionJournal(settings.journal)
    line = {"target": READER, "service": STORES["gamma"], "identity_sha256": "0" * 64, "heal": True}
    journal.append({"event": "injecting", "id": "a-heals-restart", **line})
    journal.append({"event": "clear_failed", "id": "a-heals-restart", "how": "heal_restart_failed"})
    healing = FederationLock(settings).acquire(exclusive=True, purpose=RECOVERY)
    try:
        reading = gate()
    finally:
        healing.close()
    assert (reading.ok, reading.deferred, reading.charge) == UNCHARGED_WAIT, reading.detail


@pytest.mark.parametrize("settling", [False, True])
def test_o35_another_processs_heal_past_its_bound_closes_the_gate_uncharged(tmp_path, settling):
    """O35: another process's health check has held the federation for its heal longer than the heal's bound
    (`heal_timeout_s` and `heal_settle_s`), its process still alive: a check that meets it closes the gate, uncharged,
    instead of deferring for as long as that holder lives, even while a settle that late heal published still
    closes the federation (its drain record)."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    healing = FederationLock(settings).acquire(exclusive=True, purpose=RECOVERY)
    journal = InjectionJournal(settings.journal)
    long_ago = datetime.now(UTC) - timedelta(seconds=settings.heal_timeout_s + settings.heal_settle_s + 60)
    journal.write_holder({**healing.record, "since_utc": long_ago.isoformat()})
    if settling:  # a link restart that returned late published another settle, on the gate's (fake) wall clock
        journal.raise_drain(federation.now() + 120)
    try:
        reading = gate()
    finally:
        healing.close()
    assert (reading.ok, reading.deferred, reading.charge) == (False, False, False), reading.detail
    assert "past the heal's bound" in reading.detail


def test_o35_while_the_link_check_is_made_afresh_after_a_heal_no_check_answers_an_older_pass(
    tmp_path, fake, monkeypatch
):
    """O35: after a heal the platform's last link report is expired; while one check makes the link check afresh,
    another that cannot run it at the same time waits, uncharged, instead of answering with the pass from before."""
    federation = Federation(SERVICES)
    federation.stuck = set()  # the heal's own fresh read passes: it does nothing, and writes no drain record
    stuck = {("providers[1]", "providers[0]")}
    reports = [stuck, set()]  # the check that finds the pair, the heal's own check after it; then the fresh one
    fresh_began, release = threading.Event(), threading.Event()

    def scripted(*args, **kwargs):
        if reports:
            federation.stuck = reports.pop(0)
        else:  # the check made afresh after the heal: it takes its time, and finds the pair stuck again
            fresh_began.set()
            release.wait(10)
            federation.stuck = stuck
        report = report_of(federation)
        federation.stuck = set()
        return report

    monkeypatch.setattr(platform_health, "check_links", scripted)

    def edit(data):
        data["faults"] = faults_table(heal="all_links", heal_settle_s=0, health_wait_s=0.2)
        data["tokens"] = {**data.get("tokens", {}), "margin_s": heal_margin_s(data["faults"])}

    platform = write_platform(tmp_path, edit)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, fault_driver=federation)
    first = health()
    assert (first.ok, first.deferred, first.charge) == UNCHARGED_WAIT, first.detail
    assert health.wait_for_gate_heal(10)
    assert health.links.ok is True  # the heal's own check passed, and the report is now expired
    fresh = threading.Thread(target=health)
    fresh.start()
    try:
        assert fresh_began.wait(10)
        meanwhile = health()
    finally:
        release.set()
        fresh.join(10)
    assert (meanwhile.ok, meanwhile.deferred, meanwhile.charge) == UNCHARGED_WAIT, meanwhile.detail


def test_o35_a_failed_heal_never_reopens_the_gate_on_a_pass_cached_before_it(tmp_path):
    """O35: another check cached a passing report before the heal took the federation, and the heal then failed: the
    gate's link check is made afresh after any heal, so the next check finds the pair still stuck and stays closed."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, check, _ = gate_of(tmp_path, federation, heal="all_links", heal_container_fallback=False)
    check.cache = True
    federation.on_ping = lambda label: setattr(check, "report", LinkMatrix(True, "every pair passed", ()))
    reading = healed(gate, federation)
    assert (reading.ok, reading.deferred) == (False, False)
    assert "until a link check passes" in reading.detail


def test_o35_a_heal_in_progress_defers_within_its_bound_and_closes_past_it(tmp_path):
    """O35: every check while the heal runs answers the uncharged deferral, at once, until the heal's own bound
    (`heal_timeout_s` and `heal_settle_s`) has passed since it began; a heal still running then closes the gate,
    uncharged, so a hung heal never defers for ever."""
    release = threading.Event()
    federation = Federation()
    federation.on_restart = lambda: release.wait(10)  # the heal hangs in its first link restart
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="all_links")
    try:
        first = gate()
        assert (first.ok, first.deferred, first.charge) == UNCHARGED_WAIT
        during = gate()
        assert (during.ok, during.deferred, during.charge) == UNCHARGED_WAIT
        assert "healing" in during.detail
        federation.clock.sleep(settings.heal_timeout_s + settings.heal_settle_s + 1)
        past = gate()
        assert (past.ok, past.deferred, past.charge) == (False, False, False)
        assert "past its bound" in past.detail
    finally:
        release.set()
        assert gate.wait_for_heal(10)


def test_o35_the_gate_heal_runs_through_the_platforms_standard_health_check(tmp_path, fake, monkeypatch):
    """O35: `PlatformHealth` hands the gate the link report its own call made and the providers' services, so a stuck
    pair its first call finds is healed in the background, that call answering the uncharged deferral, and the heal's
    own check after it is the platform's latest link report."""
    federation = Federation(SERVICES)
    federation.stuck = {("providers[1]", "providers[0]")}
    monkeypatch.setattr(platform_health, "check_links", lambda *args, **kwargs: report_of(federation))

    def edit(data):
        data["faults"] = faults_table(heal="all_links", heal_settle_s=0, health_wait_s=0.2)
        data["tokens"] = {**data.get("tokens", {}), "margin_s": heal_margin_s(data["faults"])}

    platform = write_platform(tmp_path, edit)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, fault_driver=federation)
    reading = health()
    assert (reading.ok, reading.deferred, reading.charge) == UNCHARGED_WAIT, reading.detail
    assert health.wait_for_gate_heal(10)
    assert acted(federation, "restart_link") == [("restart_link", "providers[0]"), ("restart_link", "providers[1]")]
    assert health.gate_heal["outcome"] == "healed"
    assert health.links.ok is True
    after = health()  # the settle's drain record (on the wall clock) may still close the federation: a wait, uncharged
    assert after.ok is True or (after.deferred, after.charge) == (True, False), after.detail


def report_of(federation: Federation) -> LinkReport:
    """The link check's report of the federation's pairs: a stuck pair's fetch failed, naming the remedy."""
    pairs = tuple(
        PairResult(
            pair.reader, pair.source, pair.ok, 0.1, remedy=None if pair.ok else REMEDY.format(reader=pair.reader)
        )
        for pair in federation.matrix().pairs
    )
    failed = [pair for pair in pairs if not pair.ok]
    return LinkReport(not failed, "stuck" if failed else "every pair passed", pairs)


# --- it acts only on stuck pairs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("found", ["every pair passing", "an untested pair", "a space that fails first"])
def test_o35_the_gate_heals_nothing_unless_its_own_check_finds_a_stuck_pair(tmp_path, found):
    """O35: the heal is never routine. A check whose pairs pass, one whose only failure is a pair that was not tested
    (no fetch failed, so no stuck link), and a check that fails before its link check (the space) while an earlier
    report shows a stuck pair, start no heal: nothing is pinged, restarted or read by a heal, and nothing is
    recorded."""
    federation = Federation()
    federation.stuck = set() if found != "a space that fails first" else {(READER, CRASHED)}
    federation.untested = {(READER, CRASHED)} if found == "an untested pair" else set()
    gate, federation, check, settings = gate_of(tmp_path, federation, heal="all_links")
    if found == "a space that fails first":
        check.report = check.made = federation.matrix()  # a stuck pair an earlier call found, left untaken
        check.space_ok = False
    reading = gate()
    assert reading.ok is (found == "every pair passing")
    assert acted(federation, *ANY_ACTION) == []
    assert [who for who, _ in federation.reads if who == "heal"] == []
    assert gate.last_heal is None
    assert heal_lines(settings) == []


def test_o35_heal_off_makes_the_gate_take_no_action(tmp_path):
    """O35: with `[faults] heal = "off"` the gate's check fails closed on a stuck pair, as before, and acts on
    nothing."""
    gate, federation, _, settings = gate_of(tmp_path, heal="off")
    reading = gate()
    assert (reading.ok, reading.deferred) == (False, False)
    assert "stuck: providers[0]->providers[2]" in reading.detail
    assert acted(federation, *ANY_ACTION) == []
    assert gate.last_heal is None
    assert heal_lines(settings) == []


# --- the conditions -----------------------------------------------------------------------------------------------


def test_o35_the_gate_heal_never_restarts_the_container_of_a_stuck_pairs_source(tmp_path):
    """O35: a provider that was the source of a stuck pair is never restarted, even once the pair it was the source of
    passes and it is left a stuck reader: the link restarts mend nothing, the fallback restarts the reader of the
    stuck pair, and the crashed source, still failing to read another provider, is skipped and the gate stays closed."""
    federation = Federation()
    federation.stuck = {(READER, CRASHED), (CRASHED, SECOND)}
    federation.link_fixes = dict.fromkeys(EVERY, ())
    federation.container_fixes = {READER, CRASHED}  # a restart of the crashed source would mend it: never made
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links")
    reading = healed(gate, federation)
    assert reading.ok is False
    assert acted(federation, "stop") == [("stop", READER)]
    assert ("stop", CRASHED) not in acted(federation, "stop")
    assert ("stop", SECOND) not in acted(federation, "stop")
    assert CRASHED in [skip["provider"] for skip in gate.last_heal["skipped"]]
    assert gate.last_heal["outcome"] == "links_stuck"


def test_o35_the_gate_heal_acts_at_most_once_per_provider(tmp_path):
    """O35: one link restart and at most one container restart per provider in a recovery, in two passes, and a later
    check of the closed gate acts on nothing: the reader's restarted container stays open in the journal until every
    pair passes, and the recovery that settles it only checks."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="all_links")
    assert healed(gate, federation).ok is False
    counted = Counter(acted(federation, "restart_link", "stop"))
    assert counted == Counter({("restart_link", label): 1 for label in EVERY} | {("stop", READER): 1})
    federation.clock.sleep(600)
    assert gate().ok is False
    assert Counter(acted(federation, "restart_link", "stop")) == counted
    (still_open,) = InjectionJournal(settings.journal).open_entries()
    assert (still_open["target"], still_open["heal"]) == (READER, True)


def test_o35_a_gate_heal_that_left_a_pair_failing_is_not_run_again_until_a_check_passes(tmp_path):
    """O35: a heal that left a pair failing is not run again by later checks of the closed gate, which fail closed
    naming why, until the gate's check finds every pair passing (mended by hand); a pair that sticks after that is
    healed again."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="all_links", heal_container_fallback=False)
    again = healed(gate, federation)
    assert len(acted(federation, "restart_link")) == 4
    assert (again.ok, again.deferred) == (False, False)
    assert "until a link check passes" in again.detail
    federation.clock.sleep(600)
    assert gate().ok is False
    assert len(acted(federation, "restart_link")) == 4
    federation.stuck = set()  # an operator mended it
    federation.clock.sleep(600)
    assert gate().ok is True
    assert heal_lines(settings)[-1]["event"] == "links_passed"
    federation.stuck = {(SECOND, CRASHED)}
    federation.link_fixes = {}
    federation.clock.sleep(600)
    assert healed(gate, federation).ok is True
    assert len(acted(federation, "restart_link")) == 8


def test_o35_a_shared_holder_that_does_not_release_fails_the_gate_heal_closed(tmp_path):
    """O35: the heal's exclusive admission waits, within `health_wait_s`, for every shared holder (a campaign run, a
    tool outside the campaign through the same shared lock); one that does not let go fails the check closed, not
    deferred, as the hold `federation_busy` that charges no run, with nothing done and nothing recorded, and the next
    check after it has let go heals."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    holder = FederationLock(settings).acquire(exclusive=False)
    try:
        reading = gate()
    finally:
        holder.close()
    assert (reading.ok, reading.deferred, reading.charge) == (False, False, False)
    assert "did not run (federation_busy)" in reading.detail
    assert acted(federation, *ANY_ACTION) == []
    assert gate.last_heal is None
    assert heal_lines(settings) == []
    assert healed(gate, federation).ok is True
    assert len(acted(federation, "restart_link")) == 4


def test_o35_the_gate_heals_only_once_the_federations_drain_has_passed(tmp_path):
    """O35: the heal's admission waits for the federation's drain record like any admission: an outage run's drain
    that outlasts `health_wait_s` (begun after the check took the federation shared) leaves the heal undone, closed and
    uncharged (`federation_busy`), and once it has passed the next check heals."""
    gate, federation, check, settings = gate_of(tmp_path, heal="all_links")
    journal = InjectionJournal(settings.journal)
    check.on_check = lambda: journal.raise_drain(federation.now() + 300)  # an outage run's drain, published meanwhile
    reading = gate()
    assert (reading.ok, reading.deferred, reading.charge) == (False, False, False)
    assert "did not run (federation_busy)" in reading.detail
    assert acted(federation, *ANY_ACTION) == []
    check.on_check = None
    federation.clock.sleep(301)
    assert healed(gate, federation).ok is True
    assert len(acted(federation, "restart_link")) == 4


@pytest.mark.parametrize("exclusive", [False, True])
def test_o35_the_gate_never_heals_inside_a_runs_own_admission(tmp_path, exclusive):
    """O35: a post-run check runs under its run's admission, while the run's tokens may still be in play: the gate
    then answers its check and heals nothing, shared or exclusive."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    with FederationLock(settings).held(exclusive=exclusive, purpose=OUTAGE_RUN, target=OTHER):
        reading = gate()
    assert reading.ok is False
    assert acted(federation, *ANY_ACTION) == []
    assert gate.last_heal is None


def test_o35_the_gate_never_opens_with_a_pair_its_own_check_still_finds_stuck(tmp_path):
    """O35: the heal's last matrix passed, but the gate's own check after it finds a pair stuck: the gate stays closed,
    and no second heal runs."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    check.grow[2] = {(SECOND, OTHER)}
    reading = healed(gate, federation)
    assert reading.ok is False
    assert "providers[1]->providers[3]" in reading.detail
    assert len(acted(federation, "restart_link")) == 4


def test_o35_a_pass_another_check_made_before_the_heal_never_opens_the_gate(tmp_path):
    """O35: while the heal runs, another check publishes a recent passing report, which the rest of the check would
    reuse; the gate makes its own check run the link check afresh after the heal, so a pair stuck by then keeps it
    closed."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    check.cache = True
    federation.on_ping = lambda label: setattr(check, "report", LinkMatrix(True, "every pair passed", ()))
    check.grow[2] = {(SECOND, OTHER)}
    reading = healed(gate, federation)
    assert reading.ok is False
    assert "providers[1]->providers[3]" in reading.detail
    assert [who for who, _ in federation.reads].count("gate") == 3  # the finding check, the heal's own, the next one


def test_o35_the_gate_heals_nothing_when_the_heals_fresh_matrix_shows_no_stuck_pair(tmp_path):
    """O35: the gate's check found a stuck pair, but by the heal's own fresh read it passes and only an untested pair
    fails: no link fetch failed, so nothing is pinged or restarted and the gate stays closed."""
    federation = Federation()

    def recovered():
        federation.stuck, federation.untested = set(), {(SECOND, OTHER)}

    federation.on_read = recovered
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links")
    reading = healed(gate, federation)
    assert reading.ok is False
    assert acted(federation, "ping", "restart_link", "stop", "start") == []
    assert "no pair failed a fetch" in gate.last_heal["why"]


def test_o35_the_latch_is_read_again_once_the_heals_admission_is_held(tmp_path):
    """O35: the latch read before the wait for the admission found no heal, but another check healed while this one
    waited: read again under the admission, the latch keeps this heal out."""
    gate, federation, check, settings = gate_of(tmp_path, heal="all_links", health_wait_s=10)
    holder = FederationLock(settings).acquire(exclusive=False)
    checked = threading.Event()
    check.on_check = checked.set

    def other_check_heals():
        checked.wait(10)
        time.sleep(0.3)  # this check now waits for the federation, past its own reading of the latch
        GateHeals(settings.journal).append({"event": "healing", "id": "another-check", "mode": "all_links"})
        holder.close()

    other = threading.Thread(target=other_check_heals)
    other.start()
    reading = gate()
    other.join(10)
    assert reading.ok is False
    assert "until a link check passes" in reading.detail
    assert acted(federation, *ANY_ACTION) == []


def test_o35_a_post_run_check_that_outlives_its_caller_still_holds_the_federation(tmp_path):
    """O35: a post-run check keeps a share of its run's admission until it ends: when the run lets its admission go
    while the check still reads (the core gave up on it), no heal's exclusive admission is granted under it."""
    gate, _, check, settings = gate_of(tmp_path, heal="all_links")
    reading, release = threading.Event(), threading.Event()
    check.on_check = lambda: (reading.set(), release.wait(10))
    with FederationLock(settings).held(exclusive=False):
        late = threading.Thread(target=contextvars.copy_context().run, args=(gate,))
        late.start()
        assert reading.wait(10)
    try:  # the run's admission is let go, and its check still reads
        assert FederationLock(settings).try_acquire(exclusive=True, purpose=RECOVERY) is None
    finally:
        release.set()
        late.join(10)
    after = FederationLock(settings).try_acquire(exclusive=True, purpose=RECOVERY)
    assert after is not None
    after.close()


def test_o35_an_open_injection_is_the_restores_and_the_gate_heals_none_of_its_pairs(tmp_path):
    """O35: while an injection is open the check fails on it before any link check, so the gate heals nothing: a
    restore's own heal owns those links."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    InjectionJournal(settings.journal).append(
        {
            "event": "injecting",
            "id": "an-open-one",
            "target": OTHER,
            "service": STORES["beta"],
            "identity_sha256": "0" * 64,
        }
    )
    assert gate().ok is False
    assert acted(federation, *ANY_ACTION) == []
    assert gate.last_heal is None


# --- the bounds and the settle ------------------------------------------------------------------------------------


def test_o35_the_gate_heal_starts_no_restart_its_bound_cannot_cover(tmp_path):
    """O35: `heal_timeout_s` bounds the gate's heal as it does a restore's: four slow pings leave less than the
    restarts' reserve, so none starts and the gate stays closed."""
    federation = Federation()
    federation.ping_s = 100.0
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links", heal_timeout_s=900)
    assert healed(gate, federation).ok is False
    assert acted(federation, "restart_link") == []
    assert "heal_timeout_s" in gate.last_heal["why"]


def test_o35_the_gate_heal_fetches_nothing_inside_its_settle_and_closes_the_federation_for_it(tmp_path):
    """O35: no check, the heal's or the gate's, runs between the first link restart and the end of the settle; the
    settle is also written to the federation's drain record, so no admission in any process (after a crash
    included) is granted before it ends."""
    federation = Federation()
    federation.ping_s = 1.0  # the clock moves between the checks before the sequence and its restarts
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="all_links")
    assert healed(gate, federation).ok is True
    restarts = [at for verb, _, at in federation.actions if verb == "restart_link"]
    reads = [at for _, at in federation.reads]
    assert not [at for at in reads if restarts[0] <= at < restarts[-1] + 30]
    assert [at for at in reads if at >= restarts[-1] + 30]
    assert InjectionJournal(settings.journal).read_drain() >= restarts[-1] + 30


def test_o35_the_container_fallback_arms_the_session_gate_before_any_action(tmp_path):
    """O35: a container restart's gate reads a probe file that predates it, so the heal arms the session gate's
    probe before its first action when its mode may restart a container, and never in `link`."""
    federation = Federation()
    federation.link_fixes = {READER: ()}
    federation.container_fixes = {READER}
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="link_then_container")
    assert healed(gate, federation).ok is True
    verbs = [verb for verb, _, _ in federation.actions]
    assert verbs[0] == "arm_gate"
    assert acted(federation, "stop") == [("stop", READER)]
    plain, plain_federation, _, _ = gate_of(tmp_path / "link", heal="link")
    assert healed(plain, plain_federation).ok is True
    assert "arm_gate" not in [verb for verb, _, _ in plain_federation.actions]


# --- the record ---------------------------------------------------------------------------------------------------


def test_o35_the_gate_heal_is_recorded_before_it_acts_and_when_it_ends_by_label(tmp_path):
    """O35: the heal is journalled before its first action (`healing`, with the gate's check that found the pair) and
    when it ends (`ended`, with the heal's record: mode, outcome, the matrices before and after, the settle window
    and every action with its UTC times), by label alone, and the gate's check that passed after it is journalled
    too (`links_passed`); the gate keeps the latest record (`last_heal`)."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    assert healed(gate, federation).ok is True
    lines = heal_lines(settings)
    assert [line["event"] for line in lines] == ["healing", "ended", "links_passed"]
    started, ended, _ = lines
    assert started["id"] == ended["id"]
    assert started["trigger"]["failing_sources"] == {CRASHED: 1}
    assert (ended["ok"], ended["mode"], ended["outcome"]) == (True, "all_links", "healed")
    assert (ended["before"]["ok"], ended["after"]["ok"]) == (False, True)
    assert ended["window"]["settle_s"] == 30
    restarts = [a for a in ended["actions"] if a["action"] == "restart_link"]
    assert [(a["provider"], a["result"]) for a in restarts] == [(label, "ok") for label in EVERY]
    assert all(a["start_utc"] <= a["end_utc"] for a in ended["actions"])
    assert ended["start_utc"] <= ended["end_utc"]
    assert gate.last_heal == {key: value for key, value in ended.items() if key != "event"}
    text = (settings.journal / "gate-heals.jsonl").read_text(encoding="utf-8")
    assert not [name for name in (*NAMES, *STORES.values()) if name in text]
