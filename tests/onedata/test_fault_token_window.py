"""O35, O06: the run's token outlives the restore's heal, because a step after the restore reads it.

The documented executor runs the agent inside `FaultInjector.running`, inside the tool server's launch: `record_run`,
then `running`'s exit (the restore, and its heal), then the collection and `finish_instruments`, then the tool server's
stop, whose re-verification of the tool plane examines the run's token and must find it unexpired
(`toolserver._recheck`). So a token whose life ends inside a heal sets an outage run aside as `tool_plane_integrity`
though its outage and restore were sound. A declared heal is therefore refused at load unless `[tokens] margin_s`
outlives two heals beyond the base margin: the run's own restore's, and one at its start, which recovers an injection
an earlier run left. The tests run the executor's order on fakes, with the token's wall clock moved by each phase.
"""

import asyncio
import contextlib
import shutil
import sys

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, make_schema, open_store
from heal_support import HEAL_TIMEOUT_S, TARGET, four_providers, heal_injector, stale
from mcp import StdioServerParameters
from record_support import record_run

from expcore.campaign.attempts import is_charged
from expcore.campaign.executor import result_from_record
from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.records.build import RunIdentity
from expcore.records.instruments import finish_instruments
from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata.admission import FederationLock
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import (
    DEFAULT_SESSION_DRAIN_S,
    TOKEN_MARGIN_BASE_S,
    heal_token_margin_s,
    load_faults,
)
from expcore_onedata.faults import PROVIDER_OUTAGE
from expcore_onedata.testing import CEILING_S, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import NOW as WALL_START
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokens import TokenIssuer, tool_server_spec
from expcore_onedata.toolserver import ENV_TOOLS, ToolServerLauncher

# 900 + 2 x 4,200 + 320 = 9,620 s, at the bound the fixture declares
LEAST_MARGIN_S = int(TOKEN_MARGIN_BASE_S + 2 * HEAL_TIMEOUT_S + DEFAULT_SESSION_DRAIN_S)


def platform_with(tmp_path, margin_s, **faults):
    def edit(data):
        four_providers(data, **faults)
        data["tokens"] = {"margin_s": margin_s}

    return write_platform(tmp_path, edit)


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def test_o35_a_declared_heal_is_refused_at_load_unless_the_token_margin_outlives_two_heals_and_a_drain(tmp_path):
    """O35, O06: the tool server's teardown examines the run's token after the restore, so a declared heal needs a
    token margin of the base (900 s) plus two heals of `heal_timeout_s` (4,200 s each) and one session drain of
    `session_drain_s` (320 s): 9,620 s. Less is refused at load, naming the keys and the numbers; that much loads; a
    heal that is off asks nothing of the margin."""
    assert (TOKEN_MARGIN_BASE_S, LEAST_MARGIN_S) == (900, 9620)
    drained = {"session_drain_s": DEFAULT_SESSION_DRAIN_S}  # the fixture's [faults] declares no drain: this one does
    for margin in (900, LEAST_MARGIN_S - 1):
        with pytest.raises(
            ConfigError, match=rf"heal.*\[tokens\] margin_s.*at least 9620 s.*{margin} s.*4200.*session_drain_s.*320"
        ):
            settings_of(platform_with(tmp_path, margin, heal="all_links", **drained))
    assert settings_of(platform_with(tmp_path, LEAST_MARGIN_S, heal="all_links", **drained)).heal == "all_links"
    shorter = int(2 * 1000 + TOKEN_MARGIN_BASE_S + DEFAULT_SESSION_DRAIN_S)  # a shorter heal needs a shorter one
    shorter_heal = {"heal": "all_links", "heal_timeout_s": 1000, **drained}
    assert settings_of(platform_with(tmp_path, shorter, **shorter_heal)).heal_timeout_s == 1000
    assert settings_of(platform_with(tmp_path, 900)).heal == "off"
    shortest = 2 * 1000 + TOKEN_MARGIN_BASE_S  # without the drain it declares, a margin is too short
    with pytest.raises(ConfigError, match=r"session_drain_s \(320 s\)"):
        settings_of(platform_with(tmp_path, shortest, **shorter_heal))
    drainless = settings_of(platform_with(tmp_path, shortest, heal="all_links", heal_timeout_s=1000, session_drain_s=0))
    assert drainless.session_drain_s == 0


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


class Sandbox:
    """The sandbox's part of the order: its collection and its stop, which read no token (`DockerSandbox`)."""

    def __init__(self, steps):
        self.steps = steps

    def collect(self):
        self.steps.append("collect")

    def stop(self):
        self.steps.append("sandbox_stop")


class Outage:
    """One outage run on the four-provider federation, as the documented executor runs it, on a wall clock the token
    is examined against: the injector's start, the agent's run at its whole ceiling, `running`'s exit (the restore and
    its heal), the collection, `finish_instruments`, the tool server's stop and the sandbox's."""

    def __init__(self, tmp_path, fake, frozen):
        tmp_path.mkdir(parents=True, exist_ok=True)
        for name in ("read.manifest.json", "manage.manifest.json"):
            shutil.copy(frozen / name, tmp_path / name)
        self.injector, self.services = heal_injector(tmp_path, stale(), heal="all_links")
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
        self.sandbox = Sandbox(self.steps)
        self.at_restore = None  # what happens to the wall clock as the restore begins
        self.heal_s = 0.0  # the wall seconds the heal takes, at its first action
        self.services.on_ping = self._healing

    def _healing(self, reader):
        if self.heal_s:
            self.wall[0] += self.heal_s
            self.steps.append("heal")
            self.heal_s = 0.0

    def run(self, schema, key):
        """The attempt, under the outage run's exclusive admission; returns the record, or raises what it raised."""
        identity = RunIdentity(key.text, key.factors, 1)

        def agent():
            self.wall[0] += CEILING_S  # the agent's loop, at its whole-time ceiling
            self.steps.append("record_run")
            options = {
                "treatment_kinds": schema.treatment_kinds_of(key),
                "treatment_targets": schema.treatment_targets_of(key),
            }
            return record_run(["done"], {}, identity=identity, record_options=options)

        async def attempt():
            async with self.launcher.launch(Waits(), "read", self.token, ceiling_s=CEILING_S):
                async with self.injector.running({"run_key": key.text, "attempt": 1}):
                    record = await asyncio.to_thread(agent)
                    if self.at_restore is not None:
                        self.at_restore()
                self.steps.append("restored")
                self.sandbox.collect()
                record = await asyncio.to_thread(finish_instruments, record, [self.injector])
                self.steps.append("finish_instruments")
            self.steps.append("tool_server_stopped")
            return record

        lock = FederationLock(load_faults(self.platform, self.config))
        try:
            with lock.held(exclusive=True, timeout_s=5, target=TARGET):
                return asyncio.run(attempt())
        finally:
            self.sandbox.stop()


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


def test_o35_the_tool_servers_teardown_after_the_restore_reads_the_run_token_so_one_expired_there_fails_it(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the dependence, in the executor's order: the run's token expires as the restore begins; the restore
    and its heal are verified and `finish_instruments` records them, but the tool server's stop, which follows, finds
    the token expired and fails the run as `tool_plane_integrity`, a charged kind, though nothing was wrong with it."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    outage.at_restore = lambda: outage.wall.__setitem__(0, outage.token.expires_at + 1)
    with pytest.raises(InfraError, match=r"at teardown.*expired") as caught:
        outage.run(schema, key)
    assert caught.value.kind == "tool_plane_integrity"
    assert is_charged("tool_plane_integrity")
    assert outage.steps == ["record_run", "restored", "collect", "finish_instruments", "sandbox_stop"]
    assert outage.injector.snapshot()["restore_verified"] is True  # the outage itself was sound


def test_o35_with_the_margin_load_demands_a_run_whose_heals_take_their_whole_bounds_completes_and_records(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the coordinator's order with the margin the load check demands (`heal_token_margin_s`; the fixture
    declares no session drain, so 9,300 s): a heal of its whole
    `heal_timeout_s` while the run starts (the recovery of an injection an earlier run left), the agent's loop at its
    whole ceiling, then the restore's heal of its whole bound: the tool server's stop still finds the token valid, the
    record carries the verified restore, and the store keeps the attempt done and uncharged, never infrastructure."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    assert outage.config.tokens.margin_s == heal_token_margin_s(settings_of(outage.platform))
    outage.services.on_stop = lambda service: outage.wall.__setitem__(0, outage.wall[0] + HEAL_TIMEOUT_S)
    outage.heal_s = HEAL_TIMEOUT_S
    record = outage.run(schema, key)
    assert outage.steps == [
        "record_run",
        "heal",
        "restored",
        "collect",
        "finish_instruments",
        "tool_server_stopped",
        "sandbox_stop",
    ]
    assert outage.wall[0] - WALL_START == 2 * HEAL_TIMEOUT_S + CEILING_S
    assert outage.wall[0] < outage.token.expires_at
    assert record.instruments["fault_injector"]["restore_verified"] is True
    begun = store.begin_attempt(key.text, NOW, 1, "")
    store.finish_attempt(begun.id, result_from_record(record, ended_utc=LATER), 3)
    row = store.q("SELECT state, charged, infra_kind FROM attempts")[0]
    assert (row["state"], row["charged"], row["infra_kind"]) == ("done", 0, None)
