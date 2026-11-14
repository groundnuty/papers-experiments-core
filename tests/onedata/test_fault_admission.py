"""O11: outage runs are exclusive with every other run through the federation's reader-writer lock, and the platform's
health check recovers what a crashed run left open before it admits anything.

Every run of a platform with `[faults]` holds the federation's lock: shared for a run of a baseline condition, exclusive
for a run whose condition uses an injector, from before its infrastructure starts until after its post-run check.
Every test runs against `FakeServices`; the lock is real, on the fixture's journal directory.
"""

import asyncio
import json
import threading
import time

import pytest
from fault_support import LOCAL_TARGET, OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults

from expcore.campaign.factors import load_factor_schema
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.admission import (
    FEDERATION_INTENT,
    FEDERATION_LOCK,
    AdmissionRefused,
    FederationBusy,
    FederationLock,
    require_admission,
    run_admission,
)
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import NOW
from expcore_onedata.tokens import TokenIssuer
from expcore_onedata.toolserver import ToolServerLauncher

OUTAGE_SERVICE, LOCAL_SERVICE = SERVICES["beta"], SERVICES["alpha"]
RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
OTHER_RUN = {"run_key": "task=task-b|model=m1|seed=1", "attempt": 1}


class Timeline(FakeServices):
    """Services that note, with the time, every stop and start that changes one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeline: list[tuple[str, str, float]] = []

    def stop(self, service):
        super().stop(service)
        self.timeline.append(("stop", service, time.monotonic()))

    def start(self, service):
        was_down = not self.running(service)
        super().start(service)
        if was_down:
            self.timeline.append(("start", service, time.monotonic()))


def federation(tmp_path):
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    return platform, FederationLock(settings)


def in_thread(work, name=None):
    """Run `work` in a thread (named `name`); return the thread and the list its result or exception lands in."""
    landed: list = []

    def run():
        try:
            landed.append(work())
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread, landed


class WatchedLock(FederationLock):
    """The federation's lock, noting when each named thread took the turnstile and the main lock (`taken`), and
    setting `at_turnstile[name]` once that thread holds the turnstile, so a test sequences arrivals on the lock's own
    state instead of on how soon a thread starts."""

    def __init__(self, settings, names):
        super().__init__(settings)
        self.taken: dict[tuple[str, str], float] = {}
        self.at_turnstile = {name: threading.Event() for name in names}

    def _try(self, name, mode):
        held = super()._try(name, mode)
        who = threading.current_thread().name
        if held is not None and who in self.at_turnstile:
            self.taken[who, name] = time.monotonic()
            if name == FEDERATION_INTENT:
                self.at_turnstile[who].set()
        return held


WAIT_S = 10.0  # the bound of every wait of a test that holds the lock between threads
GRACE_S = 0.3  # how long a party that must be held back is given to get in anyway (a secondary check)
PROBE_S = 0.2  # how long the test thread's own try for the lock is given before it counts as refused


def held_back(entered, names, grace_s=GRACE_S):
    """The names, after a grace, that are in despite the test not having let them: empty when each is held back. A
    thread that has not yet got as far as the lock is held back too, so this alone proves no exclusion."""
    time.sleep(grace_s)
    return [name for name in names if entered[name].is_set()]


def refused(lock, exclusive):
    """Whether a bounded try for the lock, made by the calling thread itself, is refused: it times out
    (`FederationBusy`, the turnstile let go on the way out) instead of being granted. Unlike a wait on another
    thread's progress, it holds back nothing vacuously: a thread that is slow to get anywhere cannot make it pass."""
    try:
        with lock.held(exclusive=exclusive, timeout_s=PROBE_S):
            return False
    except FederationBusy:
        return True


def overlapping(one, other):
    """Whether two holds, each its `in` and `out` times, share any instant."""
    return one["in"] < other["out"] and other["in"] < one["out"]


def test_o11_two_outage_runs_on_different_targets_never_overlap(tmp_path):
    """O11: the second injector waits for the federation, held exclusively by the first for its whole run, and takes
    its own target down only after the first target is back."""
    platform, _ = federation(tmp_path)
    services = Timeline()
    first, second = injector_for(platform, services), injector_for(platform, services, target=LOCAL_TARGET)
    first.start(RUN)
    thread, landed = in_thread(lambda: second.start(OTHER_RUN))
    time.sleep(0.3)
    assert services.running(LOCAL_SERVICE) is True  # still waiting for the federation
    assert first.finish()["ok"] is True
    thread.join(10)
    assert landed == [None]
    assert second.finish()["ok"] is True
    order = [(event, service) for event, service, _ in services.timeline]
    assert order == [
        ("stop", OUTAGE_SERVICE),
        ("start", OUTAGE_SERVICE),
        ("stop", LOCAL_SERVICE),
        ("start", LOCAL_SERVICE),
    ]


def test_o11_an_outage_run_waits_for_a_baseline_run_that_holds_the_federation(tmp_path):
    """O11: a baseline run holds the federation shared from before its infrastructure starts; an outage run takes
    its target down only after that run has let go."""
    platform, lock = federation(tmp_path)
    services = Timeline()
    injector = injector_for(platform, services)
    with lock.held(exclusive=False):
        thread, landed = in_thread(lambda: injector.start(RUN))
        time.sleep(0.3)
        assert services.running(OUTAGE_SERVICE) is True  # the baseline run is still on a whole federation
        released = time.monotonic()
    thread.join(10)
    assert landed == [None]
    assert services.timeline[0][:2] == ("stop", OUTAGE_SERVICE)
    assert services.timeline[0][2] >= released
    assert injector.finish()["ok"] is True


def test_o11_a_baseline_run_waits_for_an_outage_run_and_starts_on_a_whole_federation(tmp_path):
    """O11: while an outage run holds the federation, a baseline run's admission waits, and it is admitted only after
    the provider is back."""
    platform, lock = federation(tmp_path)
    services = Timeline()
    injector = injector_for(platform, services)
    injector.start(RUN)

    def admit():
        with lock.held(exclusive=False):
            return (time.monotonic(), services.running(OUTAGE_SERVICE))

    thread, landed = in_thread(admit)
    time.sleep(0.3)
    assert landed == []  # still waiting
    assert injector.finish()["ok"] is True
    thread.join(10)
    admitted_at, provider_up = landed[0]
    assert provider_up is True
    assert admitted_at >= services.timeline[-1][2]


def test_o11_a_baseline_admission_refuses_while_an_injection_is_unsettled(tmp_path):
    """O11: the unsettled test runs while the federation is held: a baseline run admitted after a failed clear sees the
    open injection and is refused as injector_failure."""
    platform, lock = federation(tmp_path)
    services = FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    assert injector.clear() is False
    del injector  # dropped: its locks go, the open injection stays

    async def admit():
        async with lock.admitted(exclusive=False):
            raise AssertionError("admitted onto a federation with a provider down")

    with pytest.raises(InfraError, match=r"unsettled") as caught:
        asyncio.run(admit())
    assert caught.value.kind == INJECTOR_FAILURE


def test_o11_an_injector_under_a_shared_admission_refuses_instead_of_upgrading(tmp_path):
    """O11: an outage run holds the federation exclusively for its whole run and never also shared, so there is no
    upgrade to deadlock on."""
    platform, lock = federation(tmp_path)
    services = FakeServices()
    injector = injector_for(platform, services)

    async def outage_run_admitted_shared():
        async with lock.admitted(exclusive=False), injector.running(RUN):
            pass

    with pytest.raises(AdmissionRefused, match="exclusive"):
        asyncio.run(outage_run_admitted_shared())
    assert services.running(OUTAGE_SERVICE) is True


def two_federations(tmp_path, **second):
    """Two platform documents with `[faults]`, each in a directory of its own; `second` changes the second's table."""
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    return write_faults(tmp_path / "one"), write_faults(tmp_path / "two", **second)


def test_o11_an_admission_for_one_federation_does_not_admit_a_run_of_another(tmp_path):
    """O11: an admission names the federation it holds (its lock file and its configuration's digest); held for one,
    it does not let a run of another start its infrastructure, nor an injector of another inject."""
    _, second = two_federations(tmp_path)
    lock = federation(tmp_path / "one")[1]
    config = load_platform(second, require_secrets=False)
    services = FakeServices()
    with lock.held(exclusive=True):
        with pytest.raises(AdmissionRefused, match="another federation"):
            require_admission(config)
        with pytest.raises(AdmissionRefused, match="another federation"):
            injector_for(second, services).start(RUN)
    assert services.running(OUTAGE_SERVICE) is True
    assert ("stop", OUTAGE_SERVICE) not in services.calls


def test_o11_one_lock_file_under_two_configurations_is_two_federations(tmp_path):
    """O11: a configuration that shares another's journal directory, and so its lock file, is still another
    federation: its digest differs, so the admission of the first does not admit it."""
    first, second = two_federations(tmp_path, journal="../one/fault-journal")
    lock = federation(tmp_path / "one")[1]
    with lock.held(exclusive=False):
        require_admission(load_platform(first, require_secrets=False))  # its own federation: admitted
        with pytest.raises(AdmissionRefused, match="another federation"):
            require_admission(load_platform(second, require_secrets=False))


def test_o11_a_tool_server_of_a_platform_with_faults_starts_only_under_an_admission(tmp_path, fake):
    """O11: every run of the platform holds the federation from before its infrastructure starts; the tool server
    refuses to start without that admission, before anything is spawned."""
    platform, lock = federation(tmp_path)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    launcher = ToolServerLauncher(config, TokenIssuer(client, config, clock=lambda: NOW), events=lambda *a: None)

    async def launch_unadmitted():
        async with launcher.launch(None, "read", None, ceiling_s=60.0):
            pass

    with pytest.raises(AdmissionRefused, match="admission"):
        asyncio.run(launch_unadmitted())
    with lock.held(exclusive=False):
        require_admission(config)  # held: the check passes


class SlowRead(FakeServices):
    """A service whose first reading, the one an injector takes before it journals anything, waits for the test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reading, self.release = threading.Event(), threading.Event()

    def observe(self, service):
        if not self.reading.is_set():
            self.reading.set()
            self.release.wait(10)
        return super().observe(service)


def standard_health(platform, fake, **options):
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    return PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW, **options)


def test_o11_a_baseline_health_check_waits_for_an_injection_in_progress_and_passes_after_it(tmp_path, fake):
    """O11: an injector that holds the federation has not journalled anything yet; the health check takes the
    federation shared, so it waits for the injection and its clear and only then reads the journal and passes."""
    platform = write_faults(tmp_path)
    services = SlowRead()
    health = standard_health(platform, fake, fault_driver=services)
    assert health().ok is True
    injector = injector_for(platform, services)
    thread, landed = in_thread(lambda: injector.start(RUN))
    assert services.reading.wait(10)
    checking, verdicts = in_thread(health)
    time.sleep(0.3)
    assert verdicts == []  # waiting: an injection proceeds
    services.release.set()
    thread.join(10)
    assert landed == [None]
    assert injector.finish()["ok"] is True
    checking.join(10)
    assert verdicts[0].ok is True, verdicts[0].detail


def writer_between(holds, lock, present, later):
    """The outage run's hold against the others, from the times each recorded: it overlaps none, it is in only after
    every present reader is out, and each later reader is in only after it is out and took the turnstile only once the
    outage run had the main lock."""
    writer = holds["writer"]
    assert [name for name in present + later if overlapping(holds[name], writer)] == [], holds
    assert writer["in"] >= max(holds[name]["out"] for name in present), holds
    assert all(holds[name]["in"] >= writer["out"] for name in later), holds
    assert all(lock.taken[name, FEDERATION_INTENT] >= lock.taken["writer", FEDERATION_LOCK] for name in later)


def test_o11_a_writer_is_admitted_once_the_readers_at_its_arrival_finish_and_later_readers_wait_for_it(tmp_path):
    """O11: a turnstile in front of the federation's lock: readers present when an outage run arrives finish, the
    outage run gets in, and readers arriving after it wait for it, so a steady stream of readers cannot starve it. No
    hold overlaps the outage run's.

    Every party holds until the test lets it go, so the contention is the test's to keep and not a matter of sleeps a
    slow thread could outlast: the present readers hold until `release_present`, the outage run until `release_writer`.
    The deciding checks are tries for the lock that the test thread makes itself (`refused`), which rest on no other
    thread's progress, and so cannot pass for a thread that is slow, or paused after taking the turnstile and before it
    asks for the lock: an exclusive request made while the present readers hold must be refused (before the outage run
    starts, so a lock that grants an exclusive request as a shared one is caught), and a shared request made once the
    outage run holds the turnstile must be refused while the present readers, who hold shared, still hold (a turnstile
    that queues nobody, or let go before the main lock, lets it through). A shared request is refused again while the
    outage run holds. The sleeps of `held_back` are a secondary check of the same, and the times each hold recorded
    are checked against the others."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    present, later = ["first", "second"], [f"later{i}" for i in range(3)]
    lock = WatchedLock(settings, [*present, "writer", *later])
    holds: dict[str, dict[str, float]] = {}
    entered = {name: threading.Event() for name in lock.at_turnstile}
    trying = {name: threading.Event() for name in later}
    release_present, release_writer = threading.Event(), threading.Event()

    def holder(name, exclusive, release=None):
        def run():
            if name in trying:
                trying[name].set()  # about to ask for the lock
            with lock.held(exclusive=exclusive, timeout_s=3 * WAIT_S):
                holds[name] = {"in": time.monotonic()}
                entered[name].set()
                if release is not None:
                    assert release.wait(WAIT_S), f"{name} was never let go"
                holds[name]["out"] = time.monotonic()

        return in_thread(run, name=name)

    threads: list = []
    try:
        threads += [holder(name, False, release_present) for name in present]
        assert all(entered[name].wait(WAIT_S) for name in present)  # both readers are on the federation
        assert refused(lock, exclusive=True), "an exclusive request was granted with readers on the federation"
        threads.append(holder("writer", True, release_writer))
        assert lock.at_turnstile["writer"].wait(WAIT_S)  # the outage run holds the turnstile; the readers still hold
        assert refused(lock, exclusive=False), "a reader arriving after the outage run was let past its turnstile"
        assert not entered["writer"].is_set(), "the outage run was admitted with readers on the federation"
        threads += [holder(name, False) for name in later]
        assert all(trying[name].wait(WAIT_S) for name in later)
        assert held_back(entered, later) == [], "a reader that arrived after the outage run passed it"
        release_present.set()
        assert entered["writer"].wait(WAIT_S)  # the readers it waited for are gone: it is in
        assert refused(lock, exclusive=False), "a reader was granted the federation while the outage run holds it"
        assert held_back(entered, later) == [], "a reader that arrived after the outage run got in while it holds"
        release_writer.set()
        for thread, landed in threads:
            thread.join(WAIT_S)
            assert not thread.is_alive()
            assert landed == [None]
    finally:  # a failed check must not leave a thread parked on the lock or on an Event
        release_present.set()
        release_writer.set()
        for thread, _ in threads:
            thread.join(WAIT_S)
    writer_between(holds, lock, present, later)


def test_o11_the_first_health_check_recovers_a_crashed_runs_injection_and_admits_runs(tmp_path, fake):
    """O11: a run crashed with its provider down; at the next campaign's start the health check takes the federation
    exclusively, brings the provider back with a new identity, and then passes."""
    platform = write_faults(tmp_path)
    services = FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    injector.clear()
    del injector  # the crash: nothing will clear it, and its locks go with it
    services.knobs["start_fails"] = False
    before = services.identity(OUTAGE_SERVICE)
    health = standard_health(platform, fake, fault_driver=services)
    verdict = health()
    assert verdict.ok is True, verdict.detail
    assert services.running(OUTAGE_SERVICE) is True
    assert services.identity(OUTAGE_SERVICE) != before


def test_o11_a_failed_recovery_keeps_every_run_blocked_naming_the_target(tmp_path, fake):
    """O11: a provider that cannot be brought back keeps the gate closed, the target named by its label only."""
    platform = write_faults(tmp_path)
    services = FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    injector.clear()
    del injector
    health = standard_health(platform, fake, fault_driver=services)
    verdict = health()
    assert verdict.ok is False
    assert OUTAGE_TARGET in verdict.detail
    assert OUTAGE_SERVICE not in verdict.detail
    assert services.running(OUTAGE_SERVICE) is False


def matched_design(tmp_path):
    """A study file with an outage condition and a control that declares `exclusive = true`, and its schema."""
    path = tmp_path / "study.toml"
    path.write_text(
        "[factors.fault]\nlevels = ['outage', 'none', 'baseline']\n\n"
        "[identity]\norder = ['task', 'fault', 'model', 'seed']\n\n"
        "[conditions.outage]\nfault = 'outage'\n"
        "infrastructure_is_treatment = [{ kind = 'provider_outage', target = 'providers[1]' }]\n\n"
        "[conditions.control]\nfault = 'none'\nexclusive = true\n\n"
        "[conditions.baseline]\nfault = 'baseline'\n",
        encoding="utf-8",
    )
    return path, load_factor_schema(path)


def test_o11_an_exclusive_control_holds_the_federation_exclusively_and_never_overlaps_another_run(tmp_path):
    """O11: a condition that declares `exclusive = true` runs alone on the federation, as an outage run does, with no
    fault: its admission is exclusive and named as an exclusive run in the holder record, a baseline run waits for it,
    and it waits for a baseline run already on the federation."""
    platform, _ = federation(tmp_path)
    config = load_platform(platform, require_secrets=False)
    _, schema = matched_design(tmp_path)
    admit = run_admission(config, schema)
    settings = load_faults(platform, config)
    timeline: list[tuple[str, str, float]] = []

    def run(name, exclusive, hold_s, entered):
        with admit((), {}, threading.Event(), exclusive) as mode:
            timeline.append((name, "in", time.monotonic()))
            entered.set()
            if exclusive:
                record = json.loads((settings.journal / "federation.holder").read_text(encoding="utf-8"))
                timeline.append((name, f"{mode}:{record['purpose']}", time.monotonic()))
            time.sleep(hold_s)
            timeline.append((name, "out", time.monotonic()))
        return mode

    # the run that arrives second starts once the first is in, not after a sleep a slow thread start could outlast
    control_in, baseline_in = threading.Event(), threading.Event()
    control, landed = in_thread(lambda: run("control", True, 0.4, control_in))
    assert control_in.wait(10)
    baseline, after = in_thread(lambda: run("baseline", False, 0.1, threading.Event()))
    control.join(10)
    baseline.join(10)
    assert landed == ["exclusive"]
    assert after == ["shared"]
    marks = {(name, what): at for name, what, at in timeline}
    assert ("control", "exclusive:exclusive_run") in marks
    assert marks["baseline", "in"] >= marks["control", "out"]  # it waited for the control to leave

    first, _ = in_thread(lambda: run("baseline", False, 0.4, baseline_in))
    assert baseline_in.wait(10)
    second, _ = in_thread(lambda: run("control", True, 0.1, threading.Event()))
    first.join(10)
    second.join(10)
    marks = {(name, what): at for name, what, at in timeline[-5:]}
    assert marks["control", "in"] >= marks["baseline", "out"]  # the control waited for the run already on it


def test_c34_an_outage_run_with_no_declared_target_is_refused_before_it_takes_the_federation(tmp_path):
    """C34: an outage run's admission names its target in the holder record; a run that arrives with the kind and no
    target is refused, so the federation is never held exclusively for an outage that names no provider."""
    platform, _ = federation(tmp_path)
    config = load_platform(platform, require_secrets=False)
    _, schema = matched_design(tmp_path)
    admit = run_admission(config, schema)
    with (
        pytest.raises(AdmissionRefused, match=r"provider_outage.*no target"),
        admit(("provider_outage",), {}, threading.Event(), False),
    ):
        raise AssertionError("the run must not be admitted")
    with admit(("provider_outage",), {"provider_outage": OUTAGE_TARGET}, threading.Event(), False) as mode:
        assert mode == "exclusive"


def test_o11_an_exclusive_condition_on_a_platform_without_faults_is_refused_naming_the_file_and_key(tmp_path):
    """O11: without `[faults]` there is no federation lock to hold, so a condition that declares `exclusive = true` is
    refused, naming the study file and the key."""
    from expcore_onedata.testing import write_platform

    config = load_platform(write_platform(tmp_path), require_secrets=False)
    _, schema = matched_design(tmp_path)
    with pytest.raises(ConfigError, match=r"study\.toml.*\[conditions\.control\].*exclusive.*\[faults\]"):
        run_admission(config, schema)
    assert run_admission(config) is None  # no schema asked for: no lock, and nothing to refuse
