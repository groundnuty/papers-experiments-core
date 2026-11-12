"""O11: a run holds the federation from before its infrastructure starts until after its post-run check.

The campaign admits each run through the platform's hook (`admission.run_admission`), so an outage run that queues for
the federation as a run's teardown ends cannot take it before the run's post-run check has looked: the check runs under
the run's own admission and sees the federation as the run left it, never deferred by the next run's outage.
"""

import threading
import time

from fault_support import FakeServices, write_faults
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.healthgate import Health
from expcore_onedata.admission import FederationLock, run_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.faults import load_faults

ONE_RUN = {"tasks": ("task-a",), "seeds": (1,)}


class Provider:
    """The rest of the platform's check: a provider of the support set that a run's failure takes down for one look."""

    def __init__(self):
        self.down, self.reported_at = False, None

    def __call__(self):
        if self.down:
            self.down, self.reported_at = False, time.monotonic()
            return Health(False, "declared provider(s) reported offline: providers[0]")
        return Health(True, "ok: the space is supported by exactly the 1 declared provider(s)")


def test_o11_a_run_whose_provider_failed_is_never_scored_on_a_deferral_when_an_outage_run_queues_after_it(tmp_path):
    """O11: a provider genuinely fails during run A; an outage run B queues for the federation as soon as A's executor
    has torn down. B gets the federation only after A's post-run check, which runs under A's admission, sees the
    failure and sets A aside: A is never scored on a deferred check."""
    (tmp_path / "federation").mkdir()
    platform = write_faults(tmp_path / "federation", health_wait_s=0.5)
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    provider, landed = Provider(), []
    health = FaultGate(settings, FakeServices(), inner=provider)

    def outage_run_b():
        admission = FederationLock(settings).acquire(exclusive=True, timeout_s=20)
        landed.append(time.monotonic())
        time.sleep(0.2)
        admission.close()

    executor = FakeExecutor()

    async def run_a(request):
        provider.down = True  # the provider fails while A runs
        record = await executor._run(request, "finish")
        threading.Thread(target=outage_run_b).start()  # A's teardown is over: B queues for the federation
        time.sleep(0.1)
        return record

    executor.default = run_a
    scheduler_settings = settings_for(recheck_s=0.01, health_timeout_s=10, workers=1)
    with build(
        tmp_path / "campaign",
        executor=executor,
        settings=scheduler_settings,
        health=health,
        plan=ONE_RUN,
        admission=run_admission(config),
    ) as scheduler:
        scheduler.run(max_dispatch=1)
        rows = scheduler.store.q("SELECT state, infra_kind, detail FROM attempts ORDER BY id")
    deadline = time.monotonic() + 20
    while not landed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rows[0]["state"] == "infra_fail", rows[0]["detail"]  # A was set aside, never scored
    assert "offline" in rows[0]["detail"]
    assert landed
    assert provider.reported_at is not None
    assert landed[0] >= provider.reported_at  # B was admitted only after A's post-run check had looked
