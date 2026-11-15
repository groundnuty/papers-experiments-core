"""O11: on a platform with `[faults]`, a run's sandbox starts only under the federation's admission.

The campaign admits a run before it launches anything (shared for a baseline condition, exclusive for an outage run),
and holds it until after the post-run check; `SandboxPlatform.launch` refuses to start without one, before it mints
a token or enters a wait, so a baseline run's sandbox never starts on a federation an outage run holds. Under it the
launch is a step of the run that uses its tokens (O35), noted on the admission until its stop has ended, so the fault
injector can tell, at a heal's start, whether it still runs.
"""

import asyncio
import contextlib
import threading
import time

import pytest
from fake_host import FakeHost
from fault_support import OUTAGE_TARGET, FakeClock, FakeServices, faults_table, injector_for
from sandbox_support import FAST, add_sandbox

from expcore.campaign.lease import LeasePool
from expcore.errors import InfraError
from expcore_onedata.admission import OUTAGE_RUN, SANDBOX_STEP, AdmissionRefused, FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.users import GROUP, TokenJournal

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
CEILING_S = 600.0


@pytest.fixture
def config(tmp_path, fake):
    """The sandbox directory's platform document with a `[faults]` table as well."""

    def edit(data):
        add_sandbox(data)
        data["faults"] = faults_table(admission_timeout_s=10, session_drain_s=320)
        data["tokens"] = {"margin_s": 1125}  # the fixture's restore, the slack and the drain (505 + 300 + 320 s)

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
    return build_platform(config, issuer, slots, tmp_path)


def build_platform(config, issuer, slots, tmp_path, **options):
    """A sandbox platform on a fake host; `events` collects what the platform records (it is required)."""
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    sandbox_options = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    journal = TokenJournal(tmp_path / "leases")
    built = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=journal,
        registry=Registry(),
        sandbox_options=sandbox_options,
        clock=lambda: NOW,
        **{"events": lambda kind, detail: None, **options},
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
        async with FederationLock(settings).admitted(exclusive=False, timeout_s=10):
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


def test_o11_a_cancelled_launch_whose_start_thread_outlives_its_wait_keeps_the_federation_until_the_thread_ends(
    config, issuer, slots, tmp_path
):
    """O11: a launch cancelled mid-start gives up waiting for its start thread after its bound, and the run's admission
    is left; the thread still creates and removes things, so the federation stays held until the thread has ended:
    no outage run can take it exclusively in the meantime."""
    built, host, _ = build_platform(config, issuer, slots, tmp_path, cancel_wait_s=0.05)
    lock = FederationLock(load_faults(config.file, config))
    pool = LeasePool(tmp_path / "leases", built.lease_keys, built.probe(), clock=lambda: 0.0, sleep=lambda s: None)
    lease = pool.acquire(GROUP, run_key="run-1")
    entered, release = threading.Event(), threading.Event()
    original = host.docker

    def slow(*args, **kwargs):
        if args[0] == "run" and "--init" in args:  # the agent's `docker run`, inside the start thread
            entered.set()
            assert release.wait(20), "the test never released the start thread"
        return original(*args, **kwargs)

    host.docker = slow
    seen: list = []

    async def go():
        async with lock.admitted(exclusive=False):
            task = asyncio.ensure_future(launched(built, TimedContext(), level="via-gate", lease=lease))
            assert await asyncio.to_thread(entered.wait, 20)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task  # gave up waiting for the thread after its bound
        while not release.is_set():  # the run has left its admission; its start thread still runs
            taken = lock.try_acquire(exclusive=True)
            seen.append(taken is None)
            if taken is not None:
                taken.close()
            if len(seen) == 5:
                release.set()
            await asyncio.sleep(0.02)
        deadline = time.monotonic() + 20
        while built._threads.active(lease.key):
            assert lock.try_acquire(exclusive=True) is None, "taken while the start thread still ran"
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        while (taken := lock.try_acquire(exclusive=True)) is None:
            assert time.monotonic() < deadline, "the thread never let the federation go"
            await asyncio.sleep(0.01)
        taken.close()

    asyncio.run(go())
    assert seen == [True] * 5


def test_o35_a_sandbox_launch_is_a_token_step_of_its_run_until_its_stop_has_ended(platform, config):
    """O35, O11: from its mint to the end of its stop (the slot's revocation included) the launch is noted on the
    run's admission as a step that uses the run's tokens, so the injector's heal can tell that it still runs; once
    the launch has ended, so has the step."""
    built, host, _ = platform
    lock = FederationLock(load_faults(config.file, config))
    seen: dict[str, list] = {"start": [], "stop": []}
    original = host.docker

    def watched(*args, **kwargs):
        phase = "stop" if args[0] in ("stop", "rm", "kill") else "start" if args[0] == "run" else None
        if phase is not None:
            seen[phase].append(admission[0].open_steps())
        return original(*args, **kwargs)

    host.docker = watched
    admission: list = []

    async def go():
        async with lock.admitted(exclusive=False) as held:
            admission.append(held)
            assert held.open_steps() == ()
            async with built.launch(TimedContext(), run_key="run-1", level="isolated", ceiling_s=CEILING_S):
                seen["body"] = held.open_steps()
            seen["after"] = held.open_steps()

    asyncio.run(go())
    assert seen["body"] == (SANDBOX_STEP,)
    assert seen["start"]
    assert set(seen["start"]) == {(SANDBOX_STEP,)}
    assert seen["stop"]
    assert set(seen["stop"]) == {(SANDBOX_STEP,)}
    assert seen["after"] == ()


def test_o35_a_sandbox_launch_whose_start_fails_ends_its_token_step(platform, config):
    """O35: a launch that fails during its start ends its step as it leaves, so a heal after it is not skipped for a
    sandbox that is gone."""
    built, host, _ = platform
    lock = FederationLock(load_faults(config.file, config))
    original = host.docker

    def failing(*args, **kwargs):
        if args[0] == "run":
            raise RuntimeError("the daemon went away")
        return original(*args, **kwargs)

    host.docker = failing
    after: list = []

    async def go():
        async with lock.admitted(exclusive=False) as held:
            with pytest.raises(RuntimeError, match="the daemon went away"):
                await launched(built, TimedContext())
            after.append(held.open_steps())

    asyncio.run(go())
    assert after == [()]


def test_o35_a_teardown_cut_at_its_deadline_counts_its_sandbox_step_until_its_stop_thread_ends(
    config, issuer, slots, tmp_path
):
    """O35, O42: a stop still running at the teardown's deadline is cut and goes on in its thread after the launch has
    left, its mount or gate maybe still up, so the sandbox's step stays counted on the run's admission until that
    thread has ended: no heal begins under a sandbox that is still being torn down."""
    built, host, _ = build_platform(config, issuer, slots, tmp_path, teardown_deadline_s=0.3)
    lock = FederationLock(load_faults(config.file, config))
    entered, release = threading.Event(), threading.Event()
    original = host.docker

    def wedged(*args, **kwargs):
        if args[0] == "rm":  # a removal wedged on a FUSE mount whose provider is gone
            entered.set()
            assert release.wait(20), "the test never released the wedged removal"
        return original(*args, **kwargs)

    host.docker = wedged
    seen: list = []

    async def go():
        async with lock.admitted(exclusive=False) as held:
            with pytest.raises(InfraError, match="cut at its deadline"):
                await launched(built, TimedContext())
            assert entered.is_set()
            seen.append(held.open_steps())  # the launch has left; its stop's thread still runs
            release.set()
            deadline = time.monotonic() + 20
            while held.open_steps():
                assert time.monotonic() < deadline, "the stop's thread never ended its count"
                await asyncio.sleep(0.01)
            seen.append(held.open_steps())

    try:
        asyncio.run(go())
    finally:
        release.set()
    assert seen == [(SANDBOX_STEP,), ()]


def test_o35_a_cancelled_launch_counts_its_sandbox_step_until_its_start_thread_ends(config, issuer, slots, tmp_path):
    """O35: a launch cancelled mid-start stops waiting for its start thread after its bound and leaves, but the thread
    goes on with the run's data-path token, so the sandbox's step stays counted on the run's admission until the
    thread has ended."""
    built, host, _ = build_platform(config, issuer, slots, tmp_path, cancel_wait_s=0.05)
    lock = FederationLock(load_faults(config.file, config))
    pool = LeasePool(tmp_path / "leases", built.lease_keys, built.probe(), clock=lambda: 0.0, sleep=lambda s: None)
    lease = pool.acquire(GROUP, run_key="run-1")
    entered, release = threading.Event(), threading.Event()
    original = host.docker

    def slow(*args, **kwargs):
        if args[0] == "run" and "--init" in args:  # the agent's `docker run`, inside the start thread
            entered.set()
            assert release.wait(20), "the test never released the start thread"
        return original(*args, **kwargs)

    host.docker = slow
    seen: list = []

    async def go():
        async with lock.admitted(exclusive=False) as held:
            task = asyncio.ensure_future(launched(built, TimedContext(), level="via-gate", lease=lease))
            assert await asyncio.to_thread(entered.wait, 20)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task  # gave up waiting for the thread after its bound
            seen.append(held.open_steps())
            release.set()
            deadline = time.monotonic() + 20
            while held.open_steps():
                assert time.monotonic() < deadline, "the start thread never ended its count"
                await asyncio.sleep(0.01)
            seen.append(held.open_steps())

    asyncio.run(go())
    assert seen == [(SANDBOX_STEP,), ()]


# --- the sandbox's sessions reported to the admission's drain (the session drain of an outage restore) -------------


def draining_lock(config):
    """The federation's lock whose exclusive admissions drain on a fake clock that their sleeps move: a test that leaves
    an armed drain would otherwise wait out the 320 s."""
    clock = FakeClock()
    return FederationLock(load_faults(config.file, config), clock=clock, sleep=clock.sleep)


def lease_for(built, tmp_path):
    pool = LeasePool(tmp_path / "leases", built.lease_keys, built.probe(), clock=lambda: 0.0, sleep=lambda s: None)
    return pool.acquire(GROUP, run_key="run-1")


@pytest.mark.parametrize(("level", "kind"), [("via-gate", "gate_stop"), ("mounted", "mount_end")])
def test_o11_a_launch_reports_its_sandboxs_session_end_to_the_exclusive_admissions_drain(
    platform, config, tmp_path, level, kind
):
    """O11: the sandbox begins its mount or its gate with the drain of the exclusive admission the run holds, and the
    launch's exit, once the stop has shown it gone, reports the time it ended: the drain then runs from it."""
    built, host, _ = platform
    lock = draining_lock(config)
    lease = lease_for(built, tmp_path)
    with lock.held(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET) as admission:
        admission.drain.arm(restore_started=time.monotonic())
        assert admission.drain.plan().basis == "restore_start"  # nothing has ended: unknown
        before = time.monotonic()
        asyncio.run(launched(built, TimedContext(), level=level, lease=lease))
        plan = admission.drain.plan()
        assert plan.basis == "session_end"
        assert before <= getattr(plan, kind) <= time.monotonic()
    assert not host.containers


def test_o11_a_launch_of_a_sandbox_with_no_data_path_leaves_no_session_to_drain(platform, config):
    """O11: no mount and no gate: the sandbox begins no session, and the drain has nothing to wait for."""
    built, _, _ = platform
    lock = draining_lock(config)
    with lock.held(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET) as admission:
        admission.drain.arm(restore_started=time.monotonic())
        asyncio.run(launched(built, TimedContext(), level="isolated"))
        assert admission.drain.plan().basis == "no_session"


def test_o11_a_launch_under_a_shared_admission_reports_nothing_and_still_runs(platform, config, tmp_path):
    """O11: a baseline run holds the federation shared, which has no drain: its sandbox starts and stops as before."""
    built, host, _ = platform
    lock = draining_lock(config)
    with lock.held(exclusive=False) as admission:
        assert admission.drain is None
        asyncio.run(launched(built, TimedContext(), level="via-gate", lease=lease_for(built, tmp_path)))
    assert not host.containers


def test_o11_a_launch_holds_a_share_of_the_admission_from_its_check_so_no_shortcut_section_begins(
    platform, config, tmp_path, monkeypatch
):
    """O11: from its admission check until its start thread holds a share of its own, the launch holds one, so the
    run's shortcut section, which seals the admission only while no worker holds a share, cannot begin while the
    sandbox is authorised and started (its token minted with the launch suspended); the share is given back after."""
    built, _, _ = platform
    lock = draining_lock(config)
    sealed: list[bool] = []
    with lock.held(exclusive=False) as admission:
        mint = built._mint_tracked

        def minting(*args):
            sealed.append(admission.seal())
            return mint(*args)

        monkeypatch.setattr(built, "_mint_tracked", minting)
        asyncio.run(launched(built, TimedContext(), level="via-gate", lease=lease_for(built, tmp_path)))
        assert sealed == [False]
        assert admission.seal() is True  # the launch's share and its start thread's are both given back
        admission.unseal()


def test_o11_a_stop_that_fails_leaves_the_sessions_end_unknown_and_the_drain_in_full(
    platform, config, tmp_path, monkeypatch
):
    """O11: a sandbox whose stop raised shows no end of its gate: the drain is unknown, not zero, and runs from the
    restore's start."""
    built, _, _ = platform
    lock = draining_lock(config)

    def failing(self, *, force=False):
        raise SandboxError("the sandbox could not be removed")

    monkeypatch.setattr(DockerSandbox, "stop", failing)
    with lock.held(exclusive=True, purpose=OUTAGE_RUN, target=OUTAGE_TARGET) as admission:
        admission.drain.arm(restore_started=time.monotonic())
        with pytest.raises(InfraError):
            asyncio.run(launched(built, TimedContext(), level="via-gate", lease=lease_for(built, tmp_path)))
        plan = admission.drain.plan()
        assert plan.basis == "restore_start"
        assert "gate" in plan.why
