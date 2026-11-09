"""O11: on a platform with `[faults]`, a run's sandbox starts only under the federation's admission.

The executor admits a run before it launches anything (shared for a baseline condition, exclusive for an outage run),
and holds the admission until after the teardown; `SandboxPlatform.launch` refuses to start without one, before it mints
a token or enters a wait, so a baseline run's sandbox never starts on a federation an outage run holds.
"""

import asyncio
import contextlib
import threading
import time

import pytest
from fake_host import FakeHost
from fault_support import FakeServices, faults_table, injector_for
from platform_support import NOW, write_platform
from sandbox_support import FAST, add_sandbox

from expcore.campaign.lease import LeasePool
from expcore_onedata.admission import AdmissionRefused, FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.users import GROUP, TokenJournal

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
CEILING_S = 600.0


@pytest.fixture
def config(tmp_path, fake):
    """The sandbox directory's platform document with a `[faults]` table as well."""

    def edit(data):
        add_sandbox(data)
        data["faults"] = faults_table(admission_timeout_s=10)

    return load_platform(write_platform(tmp_path, edit))


class TimedContext:
    """A call context that notes each wait it is asked for, with the time it was entered."""

    def __init__(self):
        self.events: list[tuple[str, str, float]] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.events.append(("enter", kind, time.monotonic()))
        try:
            yield
        finally:
            self.events.append(("exit", kind, time.monotonic()))


@pytest.fixture
def platform(config, issuer, slots, tmp_path):
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    options = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    journal = TokenJournal(tmp_path / "leases")
    built = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=journal,
        registry=Registry(),
        sandbox_options=options,
        clock=lambda: NOW,
    )
    return built, host, journal


async def launched(platform, ctx, level="isolated", lease=None):
    async with platform.launch(ctx, run_key="run-1", level=level, ceiling_s=CEILING_S, lease=lease) as sandbox:
        return sandbox


def test_o11_a_sandbox_of_a_platform_with_faults_starts_only_under_an_admission(platform, fake, tmp_path):
    """O11: launched with no admission, the sandbox is refused before any token is minted or any wait entered."""
    built, host, journal = platform
    pool = LeasePool(tmp_path / "leases", built.lease_keys, built.probe(), clock=lambda: 0.0, sleep=lambda s: None)
    lease = pool.acquire(GROUP, run_key="run-1")
    ctx = TimedContext()
    with pytest.raises(AdmissionRefused, match="admission"):
        asyncio.run(launched(built, ctx, level="via-gate", lease=lease))
    assert ctx.events == []
    assert not host.containers
    assert not [r for r in fake.requests if r.path.endswith("/user/tokens/temporary")]
    assert journal.latest_expiry(lease.key) is None


def test_o11_a_baseline_runs_sandbox_cannot_start_while_an_outage_run_holds_the_federation(platform, config):
    """O11: an outage run holds the federation exclusively; a baseline run is admitted shared, so it waits before its
    sandbox starts, outside any wait of the agent's clock, and starts once the outage is cleared."""
    built, host, _ = platform
    settings = load_faults(config.file, config)
    injector = injector_for(config.file, FakeServices())
    injector.start(RUN)  # the outage run: it holds the federation exclusively until its restore is verified
    ctx = TimedContext()
    landed: list = []

    async def baseline_run():
        async with FederationLock(settings.journal).admitted(exclusive=False, timeout_s=10):
            return await launched(built, ctx)

    def run():
        try:
            landed.append(asyncio.run(baseline_run()))
        except BaseException as error:
            landed.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.3)
    assert landed == []
    assert ctx.events == []  # no sandbox start begun: the run waits for its admission
    assert not host.containers
    assert injector.finish()["ok"] is True
    cleared_at = time.monotonic()
    thread.join(10)
    assert not isinstance(landed[0], BaseException), landed[0]
    assert [(e[0], e[1]) for e in ctx.events] == [("enter", "sandbox_lifecycle"), ("exit", "sandbox_lifecycle")] * 2
    assert ctx.events[0][2] >= cleared_at  # the only waits are the start's and the stop's, after the outage
    assert not host.containers
