"""O11: the tool server's check threads hold a share of the run's admission while they run.

The launcher reads Onezone in a worker thread before the spawn (`_prepare`) and again after the stop (`_recheck`). A
launch cancelled while one runs stops waiting for it, and its run may leave its admission at once; the thread goes on
reading Onezone for a run the federation no longer holds. Each check thread takes a share of the admission before it
starts and gives it up when it ends, so nothing of a run acts on the federation after its admission ends.

The lock is real, on the fixture's journal directory; the checks park on events the test holds, so no outcome rests on
how soon a thread runs.
"""

import asyncio
import contextlib
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fault_support import faults_table
from mcp import StdioServerParameters

from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata.admission import FederationLock
from expcore_onedata.admission_share import to_thread_holding_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.testing import CEILING_S, NOW, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokens import TokenIssuer, tool_server_spec
from expcore_onedata.toolserver import ENV_TOOLS, ToolServerLauncher

BOUND_S = 20.0
CHECKS = pytest.mark.parametrize("which", [1, 2], ids=["the check before the spawn", "the check at teardown"])


class Waits:
    """The call context the launch waits in: any wait kind, nothing measured."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


class GatedIssuer(TokenIssuer):
    """An issuer whose `examine`, the first read of a check thread, can park or fail: the `which`-th call after `arm`
    sets `entered` and waits for `release`, or raises. The platform's reads need no other gate, since both checks begin
    with this one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls, self.park_on, self.fail_on = 0, None, None

    def arm(self, *, park_on=None, fail_on=None):
        self.calls, self.park_on, self.fail_on = 0, park_on, fail_on

    def examine(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("the read of Onezone failed")
        if self.calls == self.park_on:
            self.entered.set()
            assert self.release.wait(BOUND_S), "the test never released the check thread"
        return super().examine(*args, **kwargs)


@pytest.fixture(scope="module")
def frozen_read(tmp_path_factory):
    """The read condition's frozen manifest, made once for the module: freezing starts the stand-in server."""
    directory = tmp_path_factory.mktemp("manifests")
    params = StdioServerParameters(command=sys.executable, args=TOOL_SERVER_ARGS, env={ENV_TOOLS: "echo,add"})
    manifest = FrozenManifest.from_live(asyncio.run(live_manifest(params)))
    (directory / "read.manifest.json").write_text(manifest.dump(), encoding="utf-8")
    return directory


class World:
    """A platform with `[faults]`, its federation's lock, a launcher on a gated issuer and a token for it to check."""

    def __init__(self, tmp_path, fake, frozen):
        shutil.copy(frozen / "read.manifest.json", tmp_path / "read.manifest.json")  # kept by `write_platform`
        platform = write_platform(tmp_path, lambda data: data.update(faults=faults_table()))
        config = load_platform(platform)
        self.issuer = GatedIssuer(OnezoneClient(config, http=fake.client_http()), config, clock=lambda: NOW)
        self.lock = FederationLock(load_faults(platform, config))
        self.launcher = ToolServerLauncher(config, self.issuer, events=lambda kind, detail: None)
        spec = tool_server_spec(config.space.id, config.condition("read"))
        self.token = self.issuer.mint(spec, TOOLS, ceiling_s=CEILING_S)

    async def launch(self, body=None):
        """Launch the read condition's server, run `body(provider)` inside, and stop it."""
        async with self.launcher.launch(Waits(), "read", self.token, ceiling_s=CEILING_S) as provider:
            if body is not None:
                body(provider)

    def held_by_another(self):
        """Whether the federation refuses an exclusive admission now (a bounded try: it takes nobody's progress)."""
        taken = self.lock.try_acquire(exclusive=True)
        if taken is not None:
            taken.close()
        return taken is None

    async def until_free(self):
        """Wait, bounded, until an exclusive admission can be taken."""
        deadline = time.monotonic() + BOUND_S
        while self.held_by_another():
            assert time.monotonic() < deadline, "the check thread never let the federation go"
            await asyncio.sleep(0.01)


@pytest.fixture
def world(tmp_path, fake, frozen_read):
    return World(tmp_path, fake, frozen_read)


@CHECKS
def test_o11_a_cancelled_launch_leaves_the_federation_held_while_its_check_thread_runs(world, which):
    """O11: a launch cancelled while a check thread reads Onezone (before the spawn, or at teardown) stops waiting for
    it, and its run leaves the admission; the thread still runs, so the federation stays held until it has ended and
    no outage run can take it in the meantime."""

    async def go():
        async with world.lock.admitted(exclusive=False):
            world.issuer.arm(park_on=which)
            task = asyncio.ensure_future(world.launch())
            assert await asyncio.to_thread(world.issuer.entered.wait, BOUND_S), "the check never began"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task  # gave up waiting for the thread
        try:  # the run has left its admission; the check thread still runs
            for _ in range(5):
                assert world.held_by_another(), "the federation was let go while the check thread still ran"
                await asyncio.sleep(0.02)
        finally:
            world.issuer.release.set()
        await world.until_free()

    asyncio.run(go())


@CHECKS
def test_o11_a_check_thread_that_raises_gives_up_its_share(world, which):
    """O11: a check that fails (an outage of Onezone, any exception) gives up its share, so the run's admission is
    as it was and is let go with its owner: nothing holds the federation for the thread that failed."""

    async def go():
        async with world.lock.admitted(exclusive=False) as admission:
            world.issuer.arm(fail_on=which)
            with pytest.raises(RuntimeError, match="read of Onezone failed"):
                await world.launch()
            assert admission.shares.count == 1  # only the owner's
        assert not world.held_by_another()

    asyncio.run(go())


def test_o11_a_launch_that_runs_its_course_takes_no_share_beyond_its_checks(world):
    """O11: both checks of a normal launch give their shares up before it returns: the admission holds the owner's
    share only, the server ran under it, and the federation is let go when the owner leaves."""
    seen = []

    async def go():
        async with world.lock.admitted(exclusive=False) as admission:
            await world.launch(lambda provider: seen.append(sorted(tool.name for tool in provider.tools())))
            assert admission.shares.count == 1
            assert admission.held
        assert not world.held_by_another()

    asyncio.run(go())
    assert seen == [["add", "echo"]]


def test_o11_a_launch_under_no_admission_runs_as_before(tmp_path, fake, frozen_read):
    """O11: a platform without `[faults]` has no admission to share: the checks run and the launch is unchanged."""
    shutil.copy(frozen_read / "read.manifest.json", tmp_path / "read.manifest.json")
    config = load_platform(write_platform(tmp_path))
    issuer = TokenIssuer(OnezoneClient(config, http=fake.client_http()), config, clock=lambda: NOW)
    token = issuer.mint(tool_server_spec(config.space.id, config.condition("read")), TOOLS, ceiling_s=CEILING_S)
    launcher = ToolServerLauncher(config, issuer, events=lambda kind, detail: None)
    seen = []

    async def go():
        async with launcher.launch(Waits(), "read", token, ceiling_s=CEILING_S) as provider:
            seen.append(sorted(tool.name for tool in provider.tools()))

    asyncio.run(go())
    assert seen == [["add", "echo"]]


def test_o11_a_check_thread_that_never_began_gives_up_its_share_when_its_task_is_cancelled(world):
    """O11: a task cancelled before the executor picked its work up never runs the thread, so nothing else would give
    that share up: the share is given up for it, once, and the work never runs."""
    ran = []

    async def go():
        loop = asyncio.get_running_loop()
        async with world.lock.admitted(exclusive=False) as admission:  # its wait runs in the default executor: first
            pool, free, busy = ThreadPoolExecutor(max_workers=1), threading.Event(), threading.Event()
            loop.set_default_executor(pool)
            blocker = loop.run_in_executor(None, lambda: (busy.set(), free.wait(BOUND_S)))
            deadline = time.monotonic() + BOUND_S
            try:
                while not busy.is_set():  # the pool's only worker is taken: the next work waits in its queue
                    assert time.monotonic() < deadline, "the pool's worker never began"
                    await asyncio.sleep(0.005)
                task = asyncio.ensure_future(to_thread_holding_admission(ran.append, "ran"))
                while admission.shares.count != 2:  # the task has taken its share and queued its work
                    assert time.monotonic() < deadline, "the task never took its share"
                    await asyncio.sleep(0.005)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                while admission.shares.count != 1:
                    assert time.monotonic() < deadline, "the share of a thread that never began was not given up"
                    await asyncio.sleep(0.005)
            finally:
                free.set()
                await blocker
                pool.shutdown(wait=True)
        assert not world.held_by_another()

    asyncio.run(go())
    assert ran == []
