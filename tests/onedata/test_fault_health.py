"""O11: the platform's standard health check holds every run back while an injection is unsettled, built in.

A run of a baseline condition uses no injector, so only the health gate stands between it and a federation with a
provider still down. `PlatformHealth` reads the fault journal itself whenever platform.toml has a `[faults]` table; a
study cannot leave the check out.
"""

import threading
import time

from campaign_support import keys_under, planned_store
from fault_support import LOCAL_TARGET, FakeServices, faults_table, injector_for, write_faults
from platform_support import NOW, write_platform

from expcore.campaign.healthgate import FOUND, GO, HealthGate
from expcore_onedata.admission import FederationLock
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.tls import WARNING
from expcore_onedata.tokens import TokenIssuer

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


def standard_health(platform, fake, **options):
    """The platform's health check, built as a campaign builds it: from platform.toml alone."""
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    return PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW, **options)


def gate_for(tmp_path, health):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, schema, manifest = planned_store(campaign)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, health, lambda: next(clock))
    gate.load()
    return gate, store, keys_under(schema, manifest, "mount-read")[0]  # a condition that declares no fault


def test_o11_a_baseline_check_during_an_outage_run_waits_for_it_and_passes_recording_no_outage(tmp_path, fake):
    """O11: the outage run holds the federation exclusively; a baseline run's pre-run check takes it shared, so it
    waits for the outage run, outside the agent's clock, and then passes: the gate records no outage, and no attempt is
    charged to the baseline run."""
    platform = write_faults(tmp_path)
    health = standard_health(platform, fake)
    gate, store, baseline = gate_for(tmp_path, health)
    injector = injector_for(platform, FakeServices())
    injector.start(RUN)
    answered: list = []
    checking = threading.Thread(target=lambda: answered.append((gate.before_run(baseline), time.monotonic())))
    checking.start()
    time.sleep(0.3)
    assert answered == []  # waiting for the outage run
    assert injector.finish()["ok"] is True
    cleared_at = time.monotonic()
    checking.join(10)
    verdict, answered_at = answered[0]
    assert verdict.verdict == GO, verdict.detail
    assert answered_at >= cleared_at
    assert gate.closed is False
    assert store.q("SELECT COUNT(*) FROM attempts")[0][0] == 0


def test_o11_a_failed_recovery_closes_the_standard_gate_naming_the_target(tmp_path, fake):
    """O11: a provider a crashed run left down that cannot be brought back is the one fault the gate refuses for."""
    platform = write_faults(tmp_path)
    services = FakeServices()
    injector = injector_for(platform, services)
    injector.start(RUN)
    services.knobs["start_fails"] = True
    injector.clear()
    del injector
    health = standard_health(platform, fake, fault_driver=services)
    gate, _, baseline = gate_for(tmp_path, health)
    refused = gate.before_run(baseline)
    assert refused.verdict == FOUND
    assert "providers[1]" in refused.detail


def test_o11_without_a_faults_table_the_standard_check_reads_no_journal(tmp_path, fake):
    """O11: a platform that declares no fault injector has nothing to settle; its check is as before."""
    assert standard_health(write_platform(tmp_path), fake)().ok is True


def test_o11_a_baseline_check_whose_wait_runs_out_during_an_outage_is_deferred_and_charges_no_run(tmp_path, fake):
    """O11: an outage run holds the federation with a provider of the support set down. A baseline check whose bounded
    wait for the federation runs out never goes on to read the space, so the outage is no failure of the check: the
    run is deferred, the gate stays open and no attempt is charged."""
    platform = write_faults(tmp_path, health_wait_s=0.2)
    health = standard_health(platform, fake)
    gate, store, baseline = gate_for(tmp_path, health)
    injector = injector_for(platform, FakeServices(), target=LOCAL_TARGET)  # the provider that supports the space
    injector.start(RUN)
    fake.providers[fake.PROVIDER_ID]["online"] = False  # the outage, as the federation reports it
    try:
        verdict = gate.before_run(baseline)
    finally:
        fake.providers[fake.PROVIDER_ID]["online"] = True
        assert injector.finish()["ok"] is True
    assert verdict.verdict == "defer", verdict.detail
    assert gate.closed is False
    assert store.q("SELECT COUNT(*) FROM attempts")[0][0] == 0


def test_o11_a_deferred_reading_stays_deferred_with_the_insecure_tls_warning_appended(tmp_path, fake):
    """O11, O05: with `insecure_tls` every reading ends with its warning, and a reading deferred because an outage run
    holds the federation keeps its deferral: the warning changes the detail, never the verdict, so the run is still
    held without a charge rather than counted as an outage."""

    def edit(data):
        data["faults"] = faults_table(health_wait_s=0.2)
        data["tls"] = {"insecure_tls": True}

    platform = write_platform(tmp_path, edit)
    health = standard_health(platform, fake)
    injector = injector_for(platform, FakeServices())
    injector.start(RUN)
    try:
        reading = health()
    finally:
        assert injector.finish()["ok"] is True
    assert (reading.ok, reading.deferred) == (False, True), reading.detail
    assert reading.detail.endswith(f"; {WARNING}")
    after = health()
    assert (after.ok, after.deferred) == (True, False), after.detail
    assert after.detail.endswith(f"; {WARNING}")


class Watched:
    """A Onezone client that, at every query, notes whether an outage run could take the federation right then."""

    def __init__(self, inner, lock):
        self.inner, self.lock, self.free = inner, lock, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, *args, **kwargs):
        taken = self.lock.try_acquire(exclusive=True)
        self.free.append(taken is not None)
        if taken is not None:
            taken.close()
        return self.inner.query(*args, **kwargs)


def test_o11_the_check_holds_the_federation_while_it_reads_the_space(tmp_path, fake):
    """O11: the check holds the federation shared from before it reads the space until after it compares, so no
    injection can start in the middle of the check."""
    platform = write_faults(tmp_path)
    config = load_platform(platform)
    client = Watched(OnezoneClient(config, http=fake.client_http()), FederationLock(load_faults(platform, config)))
    health = PlatformHealth(config, client, TokenIssuer(client.inner, config, clock=lambda: NOW), clock=lambda: NOW)
    assert health().ok is True
    assert client.free == [False]  # the read of the space ran while the federation was held
