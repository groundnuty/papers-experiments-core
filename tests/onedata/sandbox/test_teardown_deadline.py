"""O39: the platform's teardown of a sandbox ends within its deadline, and says how it ended.

`SandboxPlatform` waited for `DockerSandbox.stop` without a bound, so a teardown wedged on a FUSE mount whose provider
is gone (a `docker rm` that never returns) held the run, the worker and the process for ever. Now the wait is bounded by
`teardown_deadline_s`: a teardown that ends in time is recorded `completed` (or `failed`, when it raised), and one still
running at the deadline is `cut`, with what it had left, and raises `InfraError("sandbox")`, so the run is
infrastructure and never the agent's doing. The teardown goes on in its thread, which a thread cannot be made to stop;
the lease's probe keeps the key quarantined until the sandbox is shown gone. The host is in memory; its `docker rm`
blocks until the test releases it.
"""

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from sandbox_support import FAST

from expcore.campaign.lease import LeasePool
from expcore.errors import InfraError
from expcore_onedata.sandbox.provider import TEARDOWN_DEADLINE_S, SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW
from expcore_onedata.users import GROUP, TokenJournal

CEILING_S = 600.0


class Context:
    """A call context that records the waits it is asked for."""

    def __init__(self):
        self.waits: list[str] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.waits.append(kind)
        yield


@pytest.fixture
def world(config, issuer, fake, slots, tmp_path):
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    journal = TokenJournal(tmp_path / "leases")
    released = threading.Event()  # set at the end of every test: a wedged teardown's thread is let go

    def build(**options):
        return SandboxPlatform(
            config,
            issuer,
            host,
            slots=slots,
            journal=journal,
            events=lambda kind, detail: None,
            registry=Registry(),
            sandbox_options={"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None},
            clock=lambda: NOW,
            **options,
        )

    yield SimpleNamespace(build=build, host=host, journal=journal, tmp=tmp_path, released=released)
    released.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(t.name == "sandbox-stop" for t in threading.enumerate()):
        time.sleep(0.05)


def wedge_removals(world):
    """Make every `docker rm` wait, like one on a FUSE mount whose provider is gone, until the test ends."""
    entered = threading.Event()
    original = world.host.docker

    def docker(*args, **kwargs):
        if args[0] == "rm":
            entered.set()
            assert world.released.wait(60), "the test never released the wedged removal"
        return original(*args, **kwargs)

    world.host.docker = docker
    return entered


def launch(platform, body=None, *, level="via-gate", lease=None):
    """Launch a run, run `body(sandbox)` in it, and return (the sandbox, the context); an error carries `.sandbox`."""
    ctx, box = Context(), []

    async def go():
        async with platform.launch(ctx, run_key="run-1", level=level, ceiling_s=CEILING_S, lease=lease) as sandbox:
            box.append(sandbox)
            if body:
                await body(sandbox)

    try:
        asyncio.run(go())
    except BaseException as error:
        error.sandbox = box[0] if box else None
        raise
    return box[0], ctx


def test_o39_the_deadline_has_a_default_and_is_a_positive_finite_number_of_seconds(world):
    assert TEARDOWN_DEADLINE_S == 600.0
    for bad in (0, -1, float("inf"), float("nan"), True, "600"):
        with pytest.raises(ValueError, match="teardown_deadline_s"):
            world.build(teardown_deadline_s=bad)


def test_o39_a_teardown_that_ends_in_time_is_recorded_completed_with_its_deadline_and_its_seconds(world):
    sandbox, ctx = launch(world.build(teardown_deadline_s=30))
    recorded = sandbox.metadata()["teardown"]
    assert (recorded["outcome"], recorded["deadline_s"]) == ("completed", 30)
    assert 0 <= recorded["elapsed_s"] < 30
    assert "left" not in recorded
    assert ctx.waits == ["sandbox_lifecycle"] * 2  # the start and the stop, as before


def test_o39_a_teardown_that_raised_is_recorded_failed_and_is_the_runs_infrastructure_error_as_before(world):
    box = []

    async def body(sandbox):
        box.append(sandbox)
        world.host.fail_remove.add(sandbox.names.gate)

    with pytest.raises(InfraError, match="could not be torn down") as caught:
        launch(world.build(), body)
    assert caught.value.kind == "sandbox"
    assert box[0].metadata()["teardown"]["outcome"] == "failed"


def test_o39_a_teardown_past_its_deadline_is_cut_with_what_it_left_and_the_run_is_infrastructure(world):
    """O39: the run's wait ends at the deadline, whatever the host does, with `InfraError("sandbox")`, and the record
    says the teardown was cut and what it had not removed: counts and a step, never a name."""
    entered = wedge_removals(world)
    platform = world.build(teardown_deadline_s=0.5)
    began = time.monotonic()
    with pytest.raises(InfraError) as caught:
        launch(platform)
    assert time.monotonic() - began < 30
    assert entered.is_set()
    error = caught.value
    assert error.kind == "sandbox"
    assert "cut at its deadline of 0.5 s" in str(error)
    recorded = error.sandbox.metadata()["teardown"]
    assert (recorded["outcome"], recorded["deadline_s"]) == ("cut", 0.5)
    registered = len(error.sandbox._containers)  # every container the start registered; none was removed yet
    assert registered >= 2
    assert len(world.host.containers) >= 2  # the wedged removal has not returned: the host still holds them
    assert recorded["left"] == {
        "step": "containers",
        "containers": registered,
        "network": True,
        "mounts": len(world.host.mounts),
    }
    assert error.sandbox.names.gate not in str(recorded)  # no name of the configuration is in it (O12)


def test_o39_a_cut_never_hides_the_bodys_own_error_but_is_still_recorded(world):
    wedge_removals(world)
    platform = world.build(teardown_deadline_s=0.5)

    async def body(sandbox):
        raise RuntimeError("the agent's loop failed")

    with pytest.raises(RuntimeError, match="the agent's loop failed") as caught:
        launch(platform, body)
    assert caught.value.sandbox.metadata()["teardown"]["outcome"] == "cut"


def test_o39_a_cancelled_run_is_still_cut_at_the_deadline_and_stays_cancelled(world):
    """O39: a cancellation does not shorten a teardown (it must finish what it removes), and it does not lengthen
    it past its deadline either."""
    removing = wedge_removals(world)
    platform = world.build(teardown_deadline_s=0.5)
    ctx, entered, box = Context(), asyncio.Event(), []

    async def go():
        async def run():
            async with platform.launch(ctx, run_key="run-1", level="via-gate", ceiling_s=CEILING_S) as sandbox:
                box.append(sandbox)
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.ensure_future(run())
        await entered.wait()
        task.cancel()  # the body ends; the teardown begins and wedges on its first removal
        while not removing.is_set():
            await asyncio.sleep(0.02)
        task.cancel()  # and again, while the teardown is waited for
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)

    began = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - began < 30
    assert box[0].metadata()["teardown"]["outcome"] == "cut"


def test_o39_a_cut_with_a_lease_keeps_the_key_quarantined_until_the_sandbox_is_shown_gone(world):
    """C19 with O39: the cut says nothing was proven; the probe still finds the sandbox's containers, so the key is not
    freed."""
    wedge_removals(world)
    platform = world.build(teardown_deadline_s=0.5)
    pool = LeasePool(
        world.tmp / "leases", platform.lease_keys, platform.probe(), clock=lambda: 0.0, sleep=lambda s: None
    )
    lease = pool.acquire(GROUP, run_key="run-1")
    with pytest.raises(InfraError):
        launch(platform, lease=lease)
    assert pool.finish(lease) is False
    assert lease.quarantined
