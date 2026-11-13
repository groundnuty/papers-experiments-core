"""O34: a cell's deadline through the agent loop, as a study runs it. The loop gives each cell the agent's remaining
time as its cap, so the cell's deadline falls with the agent's own; the loop's watch then cuts the call and cancels its
wait for it while the deadline's escalation is still under way, which alone would make the attempt the agent's
`time_cap`. A cell a process of which outlived its kill must still end the run as `cell_unkillable`: `launch` settles
the deadline of a cell still running, and decides from it before anything else, on every way out of its body."""

import asyncio
import contextlib
import json
import threading
from types import SimpleNamespace

import pytest
from deadline_support import CELL_S, Clock, cell_processes, timers_by_the_clock
from fake_host import FakeHost
from loop_support import ScriptedBackend, call, turn
from sandbox_support import FAST, SleptClock

from expcore.campaign.lease import LeasePool
from expcore.config.models import Limits
from expcore.errors import InfraError
from expcore.loop.agent_loop import run_agent
from expcore.loop.config import AgentConfig
from expcore.loop.toolbox import ToolBox
from expcore_onedata.sandbox import cells as cells_module
from expcore_onedata.sandbox import deadline
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW
from expcore_onedata.users import GROUP, TokenJournal

CAP_S = 2.0  # the agent's time cap, and so the cap of its one cell
CEILING_S = 600.0
CODE = "open('/data/only-on-the-stopped-provider.bin').read()"  # a read that waits in the kernel on the mount


class Context:
    """A call context whose waits on infrastructure are only recorded."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.events.append(("enter", kind))
        try:
            yield
        finally:
            self.events.append(("exit", kind))


@pytest.fixture
def platform(config, issuer, fake, slots, tmp_path):
    """A platform over a fake host and the fake federation, as the provider's own tests build it, and a lease pool."""
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    clock = SleptClock()
    options = {"waits": FAST, "sample_period_s": None, "sleep": clock.sleep, "clock": clock}
    built = SandboxPlatform(
        config,
        issuer,
        host,
        slots=slots,
        journal=TokenJournal(tmp_path / "leases"),
        events=lambda kind, detail: None,
        registry=Registry(),
        sandbox_options=options,
        clock=lambda: NOW,
    )
    pool = LeasePool(
        tmp_path / "leases", built.lease_keys, built.probe(), clock=lambda: 0.0, sleep=lambda s: None,
        quarantine_retry_s=0.0,
    )  # fmt: skip
    return SimpleNamespace(platform=built, host=host, pool=pool)


def wedge_the_cell(sandbox, host) -> threading.Event:
    """The cell's python waits in the kernel and ignores its kill until the sidecar aborts the mount; the fake docker
    returns the cell once its deadline has fired, as the real one returns once its processes end or its container goes.
    The event is set when the deadline fires."""
    pids = cell_processes(SimpleNamespace(host=host), sandbox, wedged=True)
    firing, cells = threading.Event(), sandbox._cells
    kill = cells._kill_at_the_cap

    def the_harness_kills(*cell):  # the fake docker holds its lock while a cell runs: the kill proceeds once it returns
        firing.set()
        return kill(*cell)

    def runs_until_its_cap(container, argv, stdin):
        if stdin == CODE:
            assert firing.wait(30), "the cell's deadline never fired"
            return CmdResult(137, "", "")
        if container.name == sandbox.names.sidecar:  # the abort: the read fails with an errno, the pending kill takes
            host.unkillable.discard(pids["python"])
            host.files.pop(f"/proc/{pids['python']}/stat", None)
        return CmdResult(0)

    cells._kill_at_the_cap, host.exec_handler = the_harness_kills, runs_until_its_cap
    return firing


def run_the_agent(sandbox) -> object:
    """The agent loop over the sandbox's code tool, on the real clock, with one call that runs the cell."""
    limits = Limits(
        max_tool_rounds=5, time_cap_s=CAP_S, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
    )
    config = AgentConfig(limits)
    box = ToolBox([CodeExecutionProvider(sandbox)], cap_chars=limits.output_cap_chars)
    backend = ScriptedBackend([turn(call("call-1", "python", {"code": CODE}))])
    return run_agent(backend, box, "system", "task", config)


def test_o34_a_cell_that_outlives_its_kill_in_the_agent_loop_ends_the_run_as_cell_unkillable(platform):
    """O34: through `launch`, the agent loop and the code tool: the agent's one cell reads through the mount and its
    python ignores the kill at the cap until the mount is aborted. The loop sees only its own cap passing
    (`time_cap`); the attempt still ends `cell_unkillable`, raised by `launch`, not the agent's time cap, and the
    sandbox is stopped."""
    lease = platform.pool.acquire(GROUP, run_key="run-1")
    outcomes, kept = [], []

    async def go():
        async with platform.platform.launch(
            Context(), run_key="run-1", level="mounted", ceiling_s=CEILING_S, lease=lease
        ) as sandbox:
            kept.append(sandbox)
            wedge_the_cell(sandbox, platform.host)
            outcomes.append((await run_the_agent(sandbox)).outcome)

    with pytest.raises(InfraError) as ended:
        asyncio.run(go())
    assert [o.name for o in outcomes] == ["time_cap"]  # the loop alone would have scored the agent's time cap
    assert ended.value.kind == "cell_unkillable"
    sandbox = kept[0]
    assert sandbox.stopped is True
    record = sandbox._cells.ended
    assert record["outcome"] == "mount_aborted"
    assert [s["step"] for s in record["steps"]] == ["kill", "survived", "abort", "ended"]
    assert sandbox.metadata()["evidence_kept"]["relocated"] is True  # launch's stop keeps a set-aside run's disks


def test_o34_collect_settles_a_cell_still_running_before_it_decides(world, mounted):
    """O34: a cell the loop cut is still running, its own cap far off, when the body collects the run's outputs:
    `collect` enforces the cell's deadline first, and waits for it, so none fires while the agent is frozen and copied.
    A cell that outlives its kill, with an abort that fails, makes the collection `cell_unkillable`; without that, the
    quiesce took its read waiting in the kernel for a quiet process and the outputs were copied."""
    cell_processes(world, mounted, wedged=True)
    cells, firing = mounted._cells, threading.Event()
    kill = cells._kill_at_the_cap

    def the_harness_kills(*cell):
        firing.set()
        return kill(*cell)

    def runs_until_its_deadline(container, argv, stdin):
        if stdin == CODE:
            assert firing.wait(30), "the cell's deadline never fired"
            return CmdResult(137, "", "")
        return CmdResult(3, "", "mount: permission denied")  # the sidecar's abort fails

    cells._kill_at_the_cap, world.host.exec_handler = the_harness_kills, runs_until_its_deadline
    cut = threading.Thread(target=mounted.exec, args=(CODE, 120), daemon=True)
    cut.start()
    started = threading.Event()
    for _ in range(1000):
        if cells._running is not None:
            started.set()
            break
        firing.wait(0.01)
    assert started.is_set(), "the cell did not start"
    with pytest.raises(InfraError) as refused:
        mounted.collect(world.tmp_path / "out")
    assert refused.value.kind == "cell_unkillable"
    assert cells.ended["outcome"] == "sandbox_stopped"
    cut.join(30)
    assert not cut.is_alive()


# --------------------------------------------------------------------- the run's end comes before the gate's refusals
REFUSAL = {"status": 502, "upstream_status": 200, "gate_refusal": "credential_in_header", "client_saw": "5xx"}


@pytest.mark.parametrize("by_the_code_tool", [False, True], ids=["the body returns", "the code tool raises the cell's"])
def test_o34_a_cell_that_outlives_its_kill_ends_a_rest_run_before_the_gates_refusals_do(platform, by_the_code_tool):
    """O34: in a REST run whose gate has refused an answer, a cell that outlives its kill (with no FUSE mount there is
    nothing to abort: the sandbox is stopped) ends the run as `cell_unkillable`, not `data_gate_integrity`: after a
    stop the gate's log is not the run's evidence, so its refusals do not end the run in place of the cell, whether the
    body returns or raises the code tool's own error for the cell."""
    kept = []

    async def body(sandbox):
        kept.append(sandbox)
        with open(sandbox.dir / "gatelog" / "requests.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(REFUSAL) + "\n")
        assert sandbox.gate_refusals() == ["credential_in_header"]  # the gate has refused something
        wedge_the_cell(sandbox, platform.host)
        if by_the_code_tool:
            await CodeExecutionProvider(sandbox).execute("python", {"code": CODE}, timeout_s=CELL_S, ctx=None)
        else:
            await asyncio.to_thread(sandbox.exec, CODE, CELL_S)

    async def go():
        async with platform.platform.launch(
            Context(), run_key="run-1", level="via-gate", ceiling_s=CEILING_S
        ) as sandbox:
            await body(sandbox)

    with pytest.raises(InfraError) as ended:
        asyncio.run(go())
    assert ended.value.kind == "cell_unkillable"
    assert "sandbox_stopped" in ended.value.detail
    assert "credential_in_header" not in ended.value.detail
    assert kept[0].stopped is True


# ------------------------------------------------------------------ an escalation that never decides, by the clock
def hang_the_escalation(sandbox, host, release: threading.Event) -> None:
    """The cell runs until its deadline fires, and the deadline's escalation then never returns, as one whose commands
    a docker daemon never answers, until `release`."""
    firing = threading.Event()

    def hangs(*cell):
        firing.set()
        release.wait(60)

    def runs_until_its_cap(container, argv, stdin):
        if stdin == CODE:
            assert firing.wait(30), "the cell's deadline never fired"
            return CmdResult(137, "", "")
        return CmdResult(0)

    sandbox._cells._kill_at_the_cap, host.exec_handler = hangs, runs_until_its_cap


def test_o34_a_deadline_that_never_decides_ends_the_run_as_cell_unkillable_within_its_bound(platform, monkeypatch):
    """O34: the agent loop has cut the call and the body is over while the cell's escalation never decides. `launch`
    settles it: it waits for the outcome the cell's margin and `SETTLE_SLACK_S` by the clock (60 s with the default
    grace, counted here by a fake clock) and no longer, and then ends the run as `cell_unkillable` through
    `survived_kill`: an outcome never known is not a clean kill, and without that the attempt would be scored the
    agent's own `time_cap`."""
    lease = platform.pool.acquire(GROUP, run_key="run-1")
    clock, release, kept = Clock(), threading.Event(), []
    decided = timers_by_the_clock(monkeypatch, clock)
    safety = threading.Timer(30, release.set)  # nothing outlasts a test: a settle that waits for ever fails instead
    safety.daemon = True
    safety.start()

    async def go():
        async with platform.platform.launch(
            Context(), run_key="run-1", level="mounted", ceiling_s=CEILING_S, lease=lease
        ) as sandbox:
            kept.append(sandbox)
            hang_the_escalation(sandbox, platform.host, release)
            cut = threading.Thread(target=sandbox.exec, args=(CODE, 120), daemon=True)  # its own cap is far off
            stop = sandbox.stop

            def stop_once_the_hung_deadline_and_the_cell_are_released():
                release.set()
                cut.join(30)
                stop()

            sandbox.stop = stop_once_the_hung_deadline_and_the_cell_are_released
            cut.start()
            while sandbox._cells._running is None:
                await asyncio.sleep(0.01)  # the body is over only once the cell runs

    try:
        with pytest.raises(InfraError) as ended:
            asyncio.run(go())
    finally:
        safety.cancel()
        release.set()
    sandbox = kept[0]
    assert ended.value.kind == "cell_unkillable"
    assert "survived_kill" in ended.value.detail
    assert sandbox._cells.ended["outcome"] == deadline.SURVIVED
    assert sandbox.stopped is True
    bound = deadline.margin_s(5.0) + cells_module.SETTLE_SLACK_S
    assert bound == 60.0
    assert set(decided[-1].bounds) == {bound}  # the cell's timer (the start's probes had theirs): every wait bounded
    assert clock.now >= 100.0 + bound  # the settle waited the whole bound before it decided: sooner would be a guess
