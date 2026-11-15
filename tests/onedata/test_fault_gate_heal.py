"""O35: the fault gate's health check heals stuck pairs its own link check found, under its own exclusive admission.

No injection is open, so no restore will heal them: the pattern seen live is a provider whose rtransfer link crashed
(`providers[0]`), and a reader whose pair reading from it never reconnected (`providers[2]`). The gate's own all-pairs
link check (`GateCheck`, as `PlatformHealth` runs it inside the gate) finds the pair, and the gate heals it before any
attempt launches anything, with the restore heal's rules: the modes of `[faults] heal`, its bounds, the enforced
no-fetch settle, fresh pair reads around each action, its record, and failing closed.
"""

import json
from collections import Counter

import pytest
from fault_support import SERVICES, FakeClock, FakeServices, faults_table, write_faults

from expcore.campaign.healthgate import Health
from expcore_onedata import health as platform_health
from expcore_onedata.admission import OUTAGE_RUN, FederationLock
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_matrix import LinkMatrix, PairState
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer

NAMES = ("alpha", "beta", "gamma", "delta")
STORES = {name: f"store-{name}" for name in NAMES}
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
        return True

    def restart_link(self, service):
        self._note("restart_link", service)
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
    then its own link check of every pair, whose latest report `links` returns. `grow` adds stuck pairs before the
    check of that number, as a pair that sticks between two checks."""

    def __init__(self, federation: Federation) -> None:
        self.federation, self.report, self.runs = federation, None, 0
        self.space_ok = True
        self.grow: dict[int, set[tuple[str, str]]] = {}
        self.unlinked_from: int | None = None  # from this check on it answers healthy without running its link check

    def __call__(self) -> Health:
        self.runs += 1
        if not self.space_ok:
            return Health(False, "the providers supporting the space differ from the declared set")
        if self.unlinked_from is not None and self.runs >= self.unlinked_from:
            return Health(True, "the space is supported by its declared set")
        self.federation.stuck |= self.grow.get(self.runs, set())
        self.federation.reads.append(("gate", self.federation.now()))
        self.report = self.federation.matrix()
        if self.report.ok:
            return Health(True, "every provider pair transferred")
        return Health(False, f"no run is admitted until every provider link transfers: {self.report.detail}")


def four_providers(data, **faults):
    """Four providers, each with a service; short commands and settles, so a declared heal fits its default bound,
    and a short wait for the federation."""
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    table = {"services": dict(STORES), "command_timeout_s": 10, "heal_settle_s": 30, "health_wait_s": 0.2, **faults}
    data["faults"] = faults_table(**table)


def gate_of(tmp_path, federation=None, **faults):
    """The fault gate around `GateCheck`, as `PlatformHealth` builds it, on a fake clock that is also its wall."""
    tmp_path.mkdir(parents=True, exist_ok=True)  # a test may build a second federation under a subdirectory
    platform = write_platform(tmp_path, lambda data: four_providers(data, **faults))
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
        links=lambda: check.report,
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


# --- it heals what its own check found stuck --------------------------------------------------------------------


def test_o35_a_stuck_pair_at_the_health_check_is_healed_by_all_links_and_the_gate_then_opens(tmp_path):
    """O35: the gate's own link check finds a stuck pair and no injection is open; the gate takes the federation
    exclusively, restarts every provider's link once, in label order, with nothing fetched in between, settles, reads
    every pair afresh, and answers healthy only once its own check passes again."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    reading = gate()
    assert reading.ok is True, reading.detail
    assert acted(federation, "restart_link") == [("restart_link", label) for label in EVERY]
    assert acted(federation, "stop", "start") == []
    assert check.runs == 2  # the check that found the stuck pair, and the one that passed after the heal
    assert gate.last_heal["outcome"] == "healed"
    assert "healed" in reading.detail


def test_o35_the_gate_heal_runs_through_the_platforms_standard_health_check(tmp_path, fake, monkeypatch):
    """O35: `PlatformHealth` hands the gate its link check's latest report and the providers' services, so a stuck
    pair its first call finds is healed there, and the same call answers healthy once the check passes."""
    federation = Federation(SERVICES)
    federation.stuck = {("providers[1]", "providers[0]")}
    monkeypatch.setattr(platform_health, "check_links", lambda *args, **kwargs: report_of(federation))
    platform = write_faults(tmp_path, heal="all_links", heal_settle_s=0, health_wait_s=0.2)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, fault_driver=federation)
    reading = health()
    assert reading.ok is True, reading.detail
    assert acted(federation, "restart_link") == [("restart_link", "providers[0]"), ("restart_link", "providers[1]")]
    assert health.gate_heal["outcome"] == "healed"
    assert health.links.ok is True


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
        check.report = federation.matrix()  # a stuck pair an earlier call found
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
    reading = gate()
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
    assert gate().ok is False
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
    assert gate().ok is False
    assert len(acted(federation, "restart_link")) == 4
    federation.clock.sleep(600)
    again = gate()
    assert (again.ok, again.deferred) == (False, False)
    assert "until a link check passes" in again.detail
    assert len(acted(federation, "restart_link")) == 4
    federation.stuck = set()  # an operator mended it
    federation.clock.sleep(600)
    assert gate().ok is True
    assert heal_lines(settings)[-1]["event"] == "links_passed"
    federation.stuck = {(SECOND, CRASHED)}
    federation.link_fixes = {}
    federation.clock.sleep(600)
    assert gate().ok is True
    assert len(acted(federation, "restart_link")) == 8


def test_o35_a_shared_holder_that_does_not_release_fails_the_gate_heal_closed(tmp_path):
    """O35: the heal's exclusive admission waits, within `health_wait_s`, for every shared holder (a campaign run, a
    tool outside the campaign through the same shared lock); one that does not let go fails the check closed, not
    deferred, with nothing done and nothing recorded, and the next check after it has let go heals."""
    gate, federation, _, settings = gate_of(tmp_path, heal="all_links")
    holder = FederationLock(settings).acquire(exclusive=False)
    try:
        reading = gate()
    finally:
        holder.close()
    assert (reading.ok, reading.deferred) == (False, False)
    assert "did not run" in reading.detail
    assert acted(federation, *ANY_ACTION) == []
    assert gate.last_heal is None
    assert heal_lines(settings) == []
    assert gate().ok is True
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
    and the same call heals nothing more."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    check.grow[2] = {(SECOND, OTHER)}
    reading = gate()
    assert reading.ok is False
    assert "providers[1]->providers[3]" in reading.detail
    assert len(acted(federation, "restart_link")) == 4


def test_o35_the_gate_never_opens_on_a_recheck_whose_link_check_did_not_pass(tmp_path):
    """O35: the check run again after the heal answers healthy without running its link check, so the latest report
    is still the stuck one: the gate stays closed."""
    gate, federation, check, _ = gate_of(tmp_path, heal="all_links")
    check.unlinked_from = 2
    reading = gate()
    assert reading.ok is False
    assert "did not pass again after the heal" in reading.detail
    assert len(acted(federation, "restart_link")) == 4


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
    assert gate().ok is False
    assert acted(federation, "restart_link") == []
    assert "heal_timeout_s" in gate.last_heal["why"]


def test_o35_the_gate_heal_fetches_nothing_inside_its_settle_and_closes_the_federation_for_it(tmp_path):
    """O35: no check, the heal's or the gate's, runs between the first link restart and the end of the settle; the
    settle is also written to the federation's drain record, so no admission in any process (after a crash
    included) is granted before it ends."""
    federation = Federation()
    federation.ping_s = 1.0  # the clock moves between the checks before the sequence and its restarts
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="all_links")
    assert gate().ok is True
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
    assert gate().ok is True
    verbs = [verb for verb, _, _ in federation.actions]
    assert verbs[0] == "arm_gate"
    assert acted(federation, "stop") == [("stop", READER)]
    plain, plain_federation, _, _ = gate_of(tmp_path / "link", heal="link")
    assert plain().ok is True
    assert "arm_gate" not in [verb for verb, _, _ in plain_federation.actions]


# --- the record ---------------------------------------------------------------------------------------------------


def test_o35_the_gate_heal_is_recorded_before_it_acts_and_when_it_ends_by_label(tmp_path):
    """O35: the heal is journalled before its first action (`healing`, with the gate's check that found the pair) and
    when it ends (`ended`, with the heal's record: mode, outcome, the matrices before and after, the settle window
    and every action with its UTC times), by label alone, and the gate's check that passed after it is journalled
    too (`links_passed`); the gate keeps the latest record (`last_heal`)."""
    gate, _, _, settings = gate_of(tmp_path, heal="all_links")
    assert gate().ok is True
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
