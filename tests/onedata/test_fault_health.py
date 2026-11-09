"""O11: the platform's standard health check holds every run back while an injection is unsettled, built in.

A run of a baseline condition uses no injector, so only the health gate stands between it and a federation with a
provider still down. `PlatformHealth` reads the fault journal itself whenever platform.toml has a `[faults]` table; a
study cannot leave the check out.
"""

from campaign_support import keys_under, planned_store
from fault_support import FakeServices, injector_for, write_faults
from platform_support import NOW

from expcore.campaign.healthgate import FOUND, GO, HealthGate
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.tokens import TokenIssuer

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}


def standard_health(platform, fake):
    """The platform's health check, built as a campaign builds it: from platform.toml alone."""
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    return PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)


def test_o11_a_baseline_run_is_refused_by_the_standard_health_gate_while_an_injection_is_unsettled(tmp_path, fake):
    """O11: an outage condition's injector holds its provider down; the scheduler's gate, given nothing but the
    platform's standard check, refuses a run of a baseline condition until the injection is cleared."""
    platform = write_faults(tmp_path)
    health = standard_health(platform, fake)
    assert health().ok is True
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, schema, manifest = planned_store(campaign)
    baseline = keys_under(schema, manifest, "mount-read")[0]  # a condition that declares no fault
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, health, lambda: next(clock))
    gate.load()
    injector = injector_for(platform, FakeServices())
    injector.start(RUN)
    refused = gate.before_run(baseline)
    assert refused.verdict == FOUND
    assert "unsettled" in refused.detail
    assert "providers[1]" in refused.detail
    assert injector.finish()["ok"] is True
    assert health().ok is True
    assert gate.poll() is True  # the re-check opens the gate again
    assert gate.before_run(baseline).verdict == GO


def test_o11_without_a_faults_table_the_standard_check_reads_no_journal(tmp_path, fake):
    """O11: a platform that declares no fault injector has nothing to settle; its check is as before."""
    from platform_support import write_platform

    assert standard_health(write_platform(tmp_path), fake)().ok is True
