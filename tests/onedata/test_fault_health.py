"""O11: the platform's standard health check holds every run back while an injection is unsettled, built in.

A run of a baseline condition uses no injector, so only the health gate stands between it and a federation with a
provider still down. `PlatformHealth` reads the fault journal itself whenever platform.toml has a `[faults]` table; a
study cannot leave the check out.
"""

import threading
import time

from campaign_support import keys_under, planned_store
from fault_support import FakeServices, injector_for, write_faults
from platform_support import NOW

from expcore.campaign.healthgate import FOUND, GO, HealthGate
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
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
    from platform_support import write_platform

    assert standard_health(write_platform(tmp_path), fake)().ok is True
