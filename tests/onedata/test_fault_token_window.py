"""O35, O06: no step that uses the run's token runs after a heal the token may not outlive.

The tool server's stop re-verifies the tool plane and examines the run's token, which must then be unexpired
(`toolserver._recheck`); a heal takes up to `heal_timeout_s`. So the injector checks at the start of its restore, and of
its start's recovery, that every step of the run that uses its tokens (the tool server's launch, to the end of that
re-verification, and the sandbox's, to the end of its stop) has ended: each launch notes its step on the run's
admission. The documented executor runs the agent's launches inside `FaultInjector.running`, so they have ended when
its exit restores, and no longer margin is needed. A restore that would begin while one still runs is not started, and
the run is set aside, never scored, unless platform.toml declares the long margin (`[tokens] margin_s` at least
`heal_token_margin_s`: 900 s plus two heals), which proceeds as before. The tests run each order on fakes, with the
token's wall clock moved by each phase.
"""

import asyncio
import contextlib
import shutil
import sys
import threading
import time

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, make_schema, open_store
from fault_support import injector_for
from heal_support import TARGET, four_providers, heal_injector, stale
from mcp import StdioServerParameters
from record_support import record_run

from expcore.campaign.attempts import is_charged
from expcore.campaign.executor import result_from_record
from expcore.campaign.factors import FactorSchema
from expcore.errors import InfraError
from expcore.records.build import RunIdentity
from expcore.records.injections import restore_refusal
from expcore.records.instruments import finish_instruments
from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata import toolserver
from expcore_onedata.admission import SANDBOX_STEP, TOOL_SERVER_STEP, FederationLock, token_step
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import (
    DEFAULT_HEAL_TIMEOUT_S,
    TOKEN_MARGIN_BASE_S,
    heal_token_margin_s,
    load_faults,
)
from expcore_onedata.faults import INJECTOR_FAILURE, PROVIDER_OUTAGE
from expcore_onedata.testing import CEILING_S, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import NOW as WALL_START
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokens import TokenIssuer, tool_server_spec
from expcore_onedata.toolserver import ENV_TOOLS, ToolServerLauncher

LONG_MARGIN_S = int(TOKEN_MARGIN_BASE_S + 2 * DEFAULT_HEAL_TIMEOUT_S)  # 900 + 2 x 4,200 = 9,300 s
STOPPED_FIRST = "stopped_first"  # the documented order: the launches inside `running`, stopped before its exit
NESTED = "nested"  # the earlier pattern: `running` inside both launches, so its exit restores while they run
SANDBOX_OPEN = "sandbox_open"  # the tool server stopped before the restore, the sandbox not
TOOL_SERVER_OPEN = "tool_server_open"  # the sandbox stopped before the restore, the tool server not


def platform_with(tmp_path, **faults):
    return write_platform(tmp_path, lambda data: four_providers(data, **faults))


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def test_o35_a_declared_heal_loads_with_the_base_token_margin_and_the_long_margin_is_opt_in(tmp_path):
    """O35, O06: the token's window is checked when the restore begins, not at load: a declared heal loads with the
    default margin (900 s), and the long margin a study may declare to restore while its launches run is the base and
    two heals of `heal_timeout_s`, 9,300 s with the default, and less with a shorter bound; a heal that is off asks
    nothing of the margin."""
    assert settings_of(platform_with(tmp_path, heal="all_links")).heal == "all_links"
    assert load_platform(platform_with(tmp_path, heal="all_links"), require_secrets=False).tokens.margin_s == 900
    assert heal_token_margin_s(settings_of(platform_with(tmp_path, heal="all_links"))) == LONG_MARGIN_S == 9300
    shorter = settings_of(platform_with(tmp_path, heal="all_links", heal_timeout_s=1000))
    assert heal_token_margin_s(shorter) == TOKEN_MARGIN_BASE_S + 2 * 1000
    assert heal_token_margin_s(settings_of(platform_with(tmp_path))) == 0.0


@pytest.fixture(scope="module")
def frozen_manifests(tmp_path_factory):
    """The conditions' frozen manifests, made once for the module: freezing starts the stand-in server."""
    directory = tmp_path_factory.mktemp("manifests")
    for name, tools in (("read", "echo,add"), ("manage", "echo,add,fail")):
        params = StdioServerParameters(command=sys.executable, args=TOOL_SERVER_ARGS, env={ENV_TOOLS: tools})
        manifest = FrozenManifest.from_live(asyncio.run(live_manifest(params)))
        (directory / f"{name}.manifest.json").write_text(manifest.dump(), encoding="utf-8")
    return directory


class Waits:
    """The call context the tool server's launch waits in: any wait kind, nothing measured."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


class Outage:
    """One outage run on the four-provider federation, on a wall clock the token is examined against: the injector's
    start, the agent's run at its whole ceiling, the restore (and its heal) at `running`'s exit, `finish_instruments`,
    the tool server's stop and the sandbox's, in the order `run` is given. The sandbox stands in for
    `SandboxPlatform.launch`, which notes its step on the run's admission the same way (`test_fault_launch.py`); the
    tool server is the real launcher, on the fake Onezone."""

    def __init__(self, tmp_path, fake, frozen, *, margin_s=None, services=None):
        tmp_path.mkdir(parents=True, exist_ok=True)
        for name in ("read.manifest.json", "manage.manifest.json"):
            shutil.copy(frozen / name, tmp_path / name)
        tokens = None if margin_s is None else {"margin_s": margin_s}
        self.injector, self.services = heal_injector(tmp_path, services or stale(), tokens, heal="all_links")
        self.platform = tmp_path / "platform.toml"
        self.config = load_platform(self.platform)
        self.wall = [WALL_START]
        issuer = TokenIssuer(
            OnezoneClient(self.config, http=fake.client_http()), self.config, clock=lambda: self.wall[0]
        )
        self.launcher = ToolServerLauncher(self.config, issuer, events=lambda kind, detail: None)
        spec = tool_server_spec(self.config.space.id, self.config.condition("read"))
        self.token = issuer.mint(spec, TOOLS, ceiling_s=CEILING_S)
        self.steps: list[str] = []
        self.at_restore = None  # what happens to the wall clock as the restore begins
        self.heal_s = 0.0  # the wall seconds the heal takes, at its first action
        self.services.on_ping = self._healing

    def _healing(self, reader):
        if self.heal_s:
            self.wall[0] += self.heal_s
            self.steps.append("heal")
            self.heal_s = 0.0

    @contextlib.asynccontextmanager
    async def sandbox(self):
        with token_step(SANDBOX_STEP):  # as SandboxPlatform.launch does, from its mint to the end of its stop
            try:
                yield
            finally:
                self.steps.append("sandbox_stop")

    @contextlib.asynccontextmanager
    async def tool_server(self):
        async with self.launcher.launch(Waits(), "read", self.token, ceiling_s=CEILING_S):
            yield
        self.steps.append("tool_server_stopped")

    def run(self, schema, key, order=STOPPED_FIRST):
        """The attempt, in `order`, under the outage run's exclusive admission; returns the record, or raises."""
        identity = RunIdentity(key.text, key.factors, 1)
        injector = self.injector

        def agent():
            self.wall[0] += CEILING_S  # the agent's loop, at its whole-time ceiling
            self.steps.append("record_run")
            options = {
                "treatment_kinds": schema.treatment_kinds_of(key),
                "treatment_targets": schema.treatment_targets_of(key),
            }
            return record_run(["done"], {}, identity=identity, record_options=options)

        def restoring():
            if self.at_restore is not None:
                self.at_restore()

        async def finished(record):
            self.steps.append("restored")
            record = await asyncio.to_thread(finish_instruments, record, [injector])
            self.steps.append("finish_instruments")
            return record

        run = {"run_key": key.text, "attempt": 1}

        async def stopped_first():
            async with injector.running(run):
                async with self.sandbox(), self.tool_server():
                    record = await asyncio.to_thread(agent)
                restoring()
            return await finished(record)

        async def nested():
            async with self.sandbox(), self.tool_server():
                async with injector.running(run):
                    record = await asyncio.to_thread(agent)
                    restoring()
                return await finished(record)

        async def sandbox_open():
            async with self.sandbox():
                async with injector.running(run):
                    async with self.tool_server():
                        record = await asyncio.to_thread(agent)
                    restoring()
                return await finished(record)

        async def tool_server_open():
            async with self.tool_server():
                async with injector.running(run):
                    async with self.sandbox():
                        record = await asyncio.to_thread(agent)
                    restoring()
                return await finished(record)

        attempt = {
            STOPPED_FIRST: stopped_first,
            NESTED: nested,
            SANDBOX_OPEN: sandbox_open,
            TOOL_SERVER_OPEN: tool_server_open,
        }[order]
        lock = FederationLock(load_faults(self.platform, self.config))
        with lock.held(exclusive=True, timeout_s=5, target=TARGET):
            return asyncio.run(attempt())


@pytest.fixture
def outage_condition(tmp_path):
    """A store whose condition api-none takes providers[1] down, and one run key of it."""
    base = make_schema()
    declared = {"api-none": [{"kind": PROVIDER_OUTAGE, "target": TARGET}]}
    schema = FactorSchema(base.declared, base.order, base.conditions, treatment=declared)
    store = open_store(tmp_path, schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, "api-none")[0]
    return schema, store, next(row.key for row in manifest.rows if row.key.text == text)


def stored(store, key, record):
    """The store's row for the record's attempt: state, charge and kind."""
    begun = store.begin_attempt(key.text, NOW, 1, "")
    store.finish_attempt(begun.id, result_from_record(record, ended_utc=LATER), 3)
    row = store.q("SELECT state, charged, infra_kind FROM attempts")[0]
    return row["state"], row["charged"], row["infra_kind"]


def acted(services, *verbs):
    return [action for action in services.actions if action[0] in verbs]


def test_o35_an_executor_that_stops_the_tool_server_and_sandbox_before_the_restore_needs_no_long_margin(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the documented order with the default margin (900 s): the tool server's stop, its re-verification
    included, and the sandbox's end before `running`'s exit, so the token may expire as the restore begins; the heal
    then takes its whole bound, the restore is verified, and the store keeps the attempt done and uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    assert outage.config.tokens.margin_s == TOKEN_MARGIN_BASE_S
    outage.at_restore = lambda: outage.wall.__setitem__(0, outage.token.expires_at + 1)
    outage.heal_s = DEFAULT_HEAL_TIMEOUT_S
    record = outage.run(schema, key, STOPPED_FIRST)
    assert outage.steps == [
        "record_run",
        "tool_server_stopped",
        "sandbox_stop",
        "heal",
        "restored",
        "finish_instruments",
    ]
    assert outage.wall[0] > outage.token.expires_at  # expired before the heal, examined by nothing after it
    assert acted(outage.services, "restart_link")  # the heal ran
    assert record.instruments["fault_injector"]["restore_verified"] is True
    assert record.instrument_refusal is None
    assert stored(store, key, record) == ("done", 0, None)


@pytest.mark.parametrize(
    ("order", "running"),
    [
        (NESTED, [SANDBOX_STEP, TOOL_SERVER_STEP]),
        (SANDBOX_OPEN, [SANDBOX_STEP]),
        (TOOL_SERVER_OPEN, [TOOL_SERVER_STEP]),
    ],
)
def test_o35_a_restore_that_would_begin_while_a_token_step_runs_is_not_started_and_never_scored(
    tmp_path, fake, frozen_manifests, outage_condition, order, running
):
    """O35, O06: without the long margin, a restore that would begin while the run's tool server or its sandbox still
    runs is not started: the provider is not brought back and nothing heals, the injection stays open in the journal
    (`clear_failed`, `run_not_stopped`), and the record says the restore is not verified, so the attempt is set aside as
    `injector_failure`, never scored; the tool server's stop that follows finds its token valid."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    record = outage.run(schema, key, order)
    assert acted(outage.services, "start", "ping", "restart_link") == []
    assert "heal" not in outage.steps
    reading = record.instruments["fault_injector"]
    assert (reading["restore_verified"], reading["how"]) == (False, "run_not_stopped")
    refused = [(step["how"], step["steps"]) for step in reading["steps"] if step["step"] == "cleared"]
    assert refused == [("run_not_stopped", running)] * 2  # `running`'s exit, then `finish`: neither started it
    assert restore_refusal(record.instruments)["infra_kind"] == INJECTOR_FAILURE
    assert record.instrument_refusal["infra_kind"] == INJECTOR_FAILURE
    assert "not started" in record.instrument_refusal["detail"]
    assert is_charged(INJECTOR_FAILURE)
    (entry,) = outage.injector._journal.open_entries()
    assert (entry["target"], entry["clear_failed"]) == (TARGET, "run_not_stopped")
    assert not outage.services.running(outage.injector._service)


def test_o35_a_restore_refused_while_a_step_ran_starts_once_every_step_has_ended(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35: the check is made at every restore's start, so a later `finish` of the same injector, once the run's tool
    server and sandbox have both ended, brings the provider back, heals and verifies it."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    outage.run(schema, key, NESTED)
    with FederationLock(load_faults(outage.platform, outage.config)).held(exclusive=True, timeout_s=5):
        assert outage.injector.finish()["restore_verified"] is True
    assert acted(outage.services, "restart_link")
    assert outage.injector._journal.open_entries() == []


def test_o35_with_the_long_margin_declared_a_restore_inside_the_launches_proceeds_and_its_heals_fit(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the earlier order with the long margin declared (9,300 s) proceeds as before: a heal of its whole
    `heal_timeout_s` while the run starts (the recovery of an injection an earlier run left), the agent's loop at its
    whole ceiling, then the restore's heal of its whole bound: the tool server's stop still finds the token valid, the
    record carries the verified restore, and the store keeps the attempt done and uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests, margin_s=LONG_MARGIN_S)
    assert outage.config.tokens.margin_s == LONG_MARGIN_S
    outage.services.on_stop = lambda service: outage.wall.__setitem__(0, outage.wall[0] + DEFAULT_HEAL_TIMEOUT_S)
    outage.heal_s = DEFAULT_HEAL_TIMEOUT_S
    record = outage.run(schema, key, NESTED)
    assert outage.steps == [
        "record_run",
        "heal",
        "restored",
        "finish_instruments",
        "tool_server_stopped",
        "sandbox_stop",
    ]
    assert outage.wall[0] - WALL_START == 2 * DEFAULT_HEAL_TIMEOUT_S + CEILING_S
    assert outage.wall[0] < outage.token.expires_at
    assert record.instruments["fault_injector"]["restore_verified"] is True
    assert stored(store, key, record) == ("done", 0, None)


def test_o35_the_long_margin_rests_on_the_token_outliving_the_restore_since_the_tool_servers_stop_reads_it(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the dependence the check guards: in the earlier order, with the long margin, a token that expired as
    the restore began fails the tool server's stop that follows as `tool_plane_integrity`, a charged kind, though the
    restore and its heal were verified."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests, margin_s=LONG_MARGIN_S)
    outage.at_restore = lambda: outage.wall.__setitem__(0, outage.token.expires_at + 1)
    with pytest.raises(InfraError, match=r"at teardown.*expired") as caught:
        outage.run(schema, key, NESTED)
    assert caught.value.kind == "tool_plane_integrity"
    assert is_charged("tool_plane_integrity")
    assert outage.steps == ["record_run", "restored", "finish_instruments", "sandbox_stop"]
    assert outage.injector.snapshot()["restore_verified"] is True


def left_open(outage):
    """An earlier run's injection of the target, left open as by a crash: the provider down, its lock let go."""
    earlier = injector_for(outage.platform, outage.services, target=TARGET, clock=outage.services.clock)
    earlier.start({"run_key": "an-earlier-run", "attempt": 1})
    earlier._unlock()  # the process ends: the kernel drops its lock on the target, and its admission
    earlier._release_admission()
    assert not outage.services.running(outage.injector._service)
    outage.services.actions.clear()


def test_o35_a_starts_recovery_does_not_heal_while_the_runs_launches_run_without_the_long_margin(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the start recovers an injection an earlier run left, and may heal it; in the earlier order the run's
    tool server and sandbox already run then, so without the long margin the recovery restores without healing: a link
    that stays stuck fails the start closed as `injector_failure`, with no link restarted and the withheld heal in the
    reading."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    left_open(outage)
    with pytest.raises(InfraError) as caught:
        outage.run(schema, key, NESTED)
    assert caught.value.kind == INJECTOR_FAILURE
    assert "links_stuck" in caught.value.detail or "earlier run left open" in caught.value.detail
    assert acted(outage.services, "ping", "restart_link") == []
    withheld = [step for step in outage.injector.snapshot()["steps"] if step["step"] == "heal_withheld"]
    assert [step["steps"] for step in withheld] == [[SANDBOX_STEP, TOOL_SERVER_STEP]]


def test_o35_in_the_documented_order_the_starts_recovery_heals_before_any_token_step_begins(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35: in the documented order the injector starts before the sandbox and the tool server, so its recovery of an
    injection an earlier run left heals with no step of the run begun, and the run completes, done and uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    left_open(outage)
    record = outage.run(schema, key, STOPPED_FIRST)
    assert acted(outage.services, "restart_link")
    steps = [step["step"] for step in record.instruments["fault_injector"]["steps"]]
    assert "heal_withheld" not in steps
    assert record.instruments["fault_injector"]["restore_verified"] is True
    assert stored(store, key, record) == ("done", 0, None)


@pytest.mark.parametrize("check", ["_prepare", "_recheck"])
def test_o35_a_cancelled_tool_server_launch_counts_its_step_until_its_check_thread_ends(
    tmp_path, fake, frozen_manifests, monkeypatch, check
):
    """O35: a cancellation does not stop the worker thread that examines the run's token, before the start or at the
    stop, so the tool server's step stays counted on the run's admission until that thread has ended, though the
    launch has already left: no restore heals while the token is still being examined."""
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    entered, release = threading.Event(), threading.Event()
    real = getattr(toolserver, check)

    def held(*args):
        entered.set()
        assert release.wait(20), "the test never released the check"
        return real(*args)

    monkeypatch.setattr(toolserver, check, held)
    seen: list = []

    async def go(admission):
        async def launched():
            async with outage.tool_server():
                await asyncio.sleep(0)

        task = asyncio.ensure_future(launched())
        assert await asyncio.to_thread(entered.wait, 20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        seen.append(admission.open_steps())  # the launch has left; its check's thread still runs
        release.set()
        deadline = time.monotonic() + 20
        while admission.open_steps():
            assert time.monotonic() < deadline, "the check's thread never ended its count"
            await asyncio.sleep(0.01)
        seen.append(admission.open_steps())

    lock = FederationLock(load_faults(outage.platform, outage.config))
    with lock.held(exclusive=True, timeout_s=5) as admission:
        asyncio.run(go(admission))
    assert seen == [(TOOL_SERVER_STEP,), ()]
