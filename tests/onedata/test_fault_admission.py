"""O11: outage runs are exclusive with every other run through the federation's reader-writer lock, and the platform's
health check recovers what a crashed run left open before it admits anything.

Every run of a platform with `[faults]` holds the federation's lock: shared for a run of a baseline condition, exclusive
for a run whose condition uses an injector, from before its infrastructure starts until after its teardown. Every test
runs against `FakeServices`; the lock is real, on the fixture's journal directory.
"""

import asyncio
import threading
import time

import pytest
from fault_support import LOCAL_TARGET, OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults
from platform_support import NOW

from expcore.errors import InfraError
from expcore_onedata.admission import AdmissionRefused, FederationLock, require_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import INJECTOR_FAILURE, load_faults
from expcore_onedata.health import PlatformHealth
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
    return platform, FederationLock(settings.journal)


def in_thread(work):
    """Run `work` in a thread; return the thread and the list its result or exception lands in."""
    landed: list = []

    def run():
        try:
            landed.append(work())
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, landed


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


def test_o11_a_baseline_health_check_cannot_pass_while_an_injection_proceeds(tmp_path, fake):
    """O11: an injector that holds the federation has not journalled anything yet; the health check still fails,
    because it cannot take the federation shared while an injection proceeds."""
    platform = write_faults(tmp_path)
    services = SlowRead()
    health = standard_health(platform, fake, fault_driver=services)
    assert health().ok is True
    injector = injector_for(platform, services)
    thread, landed = in_thread(lambda: injector.start(RUN))
    assert services.reading.wait(10)
    verdict = health()
    services.release.set()
    thread.join(10)
    assert landed == [None]
    assert verdict.ok is False
    assert "in progress" in verdict.detail
    assert injector.finish()["ok"] is True
    assert health().ok is True


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
