"""O35, O06: no heal runs while a step that uses the run's token may need it after the heal.

The tool server's stop re-verifies the tool plane and examines the run's token, which must then be unexpired
(`toolserver._recheck`); a heal takes up to `heal_timeout_s`. So the restore always runs (its container start and its
session gate use no run token), and at the start of a heal, the restore's or the start's recovery's, the injector reads
the steps of the run that use its tokens (the tool server's launch, to the end of that re-verification, and the
sandbox's, to the end of its stop), which each launch counts on the run's admission. With one still running and no long
margin declared, the heal is skipped and the reading says why; a pair that stays stuck leaves the restore unverified,
never scored, for a later start with no step running to heal. An executor that stops both before the restore heals
with the default margin. At load, the margin must outlive the restore and its gate, one restore of it: a failed restore
is tried again only once no such step runs, and is otherwise left open for the next start's recovery, and a step that
uses a token is refused without the run's admission. The tests run each order on fakes, with the token's wall clock
moved by each phase.
"""

import asyncio
import contextlib
import shutil
import sys
import threading
import time

import pytest
from campaign_support import LATER, NOW, keys_under, make_manifest, make_schema, open_store
from fault_support import OUTAGE_TARGET, FakeClock, FakeServices, injector_for, write_faults
from heal_support import TARGET, HealServices, four_providers, heal_injector, stale
from mcp import StdioServerParameters
from record_support import record_run

from expcore.campaign.attempts import is_charged
from expcore.campaign.executor import result_from_record
from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.records.build import RunIdentity
from expcore.records.injections import restore_refusal
from expcore.records.instruments import finish_instruments
from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata import toolserver
from expcore_onedata.admission import (
    SANDBOX_STEP,
    TOOL_SERVER_STEP,
    AdmissionRefused,
    FederationLock,
    TokenStepRefused,
    begin_token_step,
    fault_settings_of,
    token_step,
)
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_settings import (
    DEFAULT_HEAL_TIMEOUT_S,
    TOKEN_SLACK_S,
    heal_token_margin_s,
    load_faults,
    restore_bound_s,
    restore_drain_s,
    restore_margin_derivation,
    restore_token_margin_s,
)
from expcore_onedata.faults import INJECTOR_FAILURE, PROVIDER_OUTAGE
from expcore_onedata.testing import CEILING_S, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import NOW as WALL_START
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tokens import TokenIssuer, tool_server_spec
from expcore_onedata.toolserver import ENV_TOOLS, ToolServerLauncher

DEFAULT_MARGIN_S = 900  # `[tokens] margin_s` by default
# The four-provider fixture's restore and gate: a stop of unknown outcome waited out (5 s, and a pause of 1 s past it),
# two starts each with its wait (2 x (10 s + 5 s + 1 s)) and a pause between them (1 s), the wait to serve (5 s + 1 s)
# and the session gate (240 s), and the slack (300 s): 585 s. The long margin adds two heals of 4,200 s.
FIXTURE_RESTORE_MARGIN_S = 585
LONG_MARGIN_S = FIXTURE_RESTORE_MARGIN_S + 2 * int(DEFAULT_HEAL_TIMEOUT_S)  # 8,985 s
NESTED = "nested"  # the documented order: `running` inside both launches, so its exit restores while they run
STOPPED_FIRST = "stopped_first"  # the other study's order: both launches stopped before `running`'s exit
SANDBOX_OPEN = "sandbox_open"  # the tool server stopped before the restore, the sandbox not
TOOL_SERVER_OPEN = "tool_server_open"  # the sandbox stopped before the restore, the tool server not


def platform_with(tmp_path, **faults):
    return write_platform(tmp_path, lambda data: four_providers(data, **faults))


def settings_of(platform):
    return load_faults(platform, load_platform(platform, require_secrets=False))


def at_default_bounds(data, heal, margin_s, **faults):
    """The four-provider federation with `[faults]` at its default bounds and drain (the fixture shortens them)."""
    four_providers(data, heal=heal)
    for key in (
        "settle_timeout_s",
        "ready_timeout_s",
        "clear_attempts",
        "command_timeout_s",
        "poll_s",
        "session_drain_s",
    ):
        data["faults"].pop(key, None)
    data["faults"].update(faults)
    data["tokens"] = {"margin_s": margin_s}


# the derivation at the default bounds, as the refusal spells it: the stop's wait and its pause (180 + 2), three
# starts with their waits and pauses (3 x (120 + 180 + 2)), two pauses between them (2 x 2), the wait to serve and its
# pause (180 + 2), the session gate (240), the slack (300) and the session drain (320), which a clear that takes the
# federation itself waits out before it restores; its own drain ends inside the restore's bound (1,514 s)
DEFAULT_DERIVATION = r"182 \+ 906 \+ 4 \+ 182 \+ 240 \+ 300 \+ 320 = 2134"
DRAIN_WORDS = (
    "a drain record still standing, which a clear that takes the federation itself waits out before it restores"
)


@pytest.mark.parametrize("tokens", [None, {}, {"max_excess_s": 600}], ids=["no table", "empty table", "no key"])
def test_o35_a_platform_with_faults_that_declares_no_token_margin_is_refused_naming_the_line_to_add(tmp_path, tokens):
    """O35, O06: a token's lifetime is the study's choice, declared and never implied: with `[faults]`, a platform
    that declares no `[tokens] margin_s` (no table, an empty one, or one without the key) is refused at load, naming
    the least figure, its derivation term by term, and the line to add; no default is derived."""

    def edit(data):
        at_default_bounds(data, "off", DEFAULT_MARGIN_S)
        if tokens is None:
            data.pop("tokens")
        else:
            data["tokens"] = dict(tokens)

    expected = (
        r"\[tokens\] margin_s is not declared.*the least is 2134 s.*settle_timeout_s 180 s.*command_timeout_s 120 s.*"
        r"ready_timeout_s 180 s.*poll_s 2 s.*gate_timeout_s 240 s.*slack 300 s.*session_drain_s 320 s.*"
        + DEFAULT_DERIVATION
        + r".*add: \[tokens\] margin_s = 2134"
    )
    with pytest.raises(ConfigError, match=expected):
        settings_of(write_platform(tmp_path, edit))


@pytest.mark.parametrize("heal", ["off", "all_links"])
def test_o35_a_token_margin_the_restore_and_gate_could_outlast_is_refused_with_its_derivation(tmp_path, heal):
    """O35, O06: the restore always runs, inside the run's token window in the documented order, so `[tokens]
    margin_s` must outlive it: a stop of unknown outcome waited out (settle_timeout_s 180 s), three starts each with its
    wait for the new identity (3 x (command_timeout_s 120 s + settle_timeout_s 180 s)) and two pauses between them, the
    wait to serve (ready_timeout_s 180 s), each wait one pause (poll_s 2 s) past its bound, and the session gate
    (gate_timeout_s 240 s), 1,514 s at the defaults, 300 s of slack, and the session drain (session_drain_s 320 s):
    2,134 s. A margin of 900 s is refused with that derivation and the line to change, heal declared or not; the least,
    2,134 s, loads."""
    expected = (
        r"\[tokens\] margin_s = 900 is too small: the least is 2134 s.*settle_timeout_s 180 s.*3 starts.*"
        r"command_timeout_s 120 s.*ready_timeout_s 180 s.*poll_s 2 s.*gate_timeout_s 240 s.*slack 300 s.*"
        r"session_drain_s 320 s.*" + DEFAULT_DERIVATION + r".*set: \[tokens\] margin_s = 2134"
    )
    with pytest.raises(ConfigError, match=expected):
        settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, heal, DEFAULT_MARGIN_S)))
    with pytest.raises(ConfigError, match=r"margin_s = 2133 is too small: the least is 2134 s"):
        settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, heal, 2133)))
    settings = settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, heal, 2134)))
    assert (restore_token_margin_s(settings), TOKEN_SLACK_S, restore_drain_s(settings)) == (2134, 300, 320)


def test_o35_a_least_margin_with_a_fraction_of_a_second_rounds_up_to_the_whole_second_the_line_names(tmp_path):
    """O35: `margin_s` is a whole number of seconds, so a least figure with a fraction (poll_s 0.5 s) names the next
    whole second as the least and in the line to add, and that margin loads."""
    with pytest.raises(ConfigError, match=r"the least is 2124 s.*= 2123\.5.*\[tokens\] margin_s = 2124"):
        settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 900, poll_s=0.5)))
    assert settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 2124, poll_s=0.5)))


def test_o35_a_large_least_margin_is_named_in_full_never_in_exponent_form(tmp_path):
    """O35: the least and the line to write are whole numbers written out in full, which TOML reads as an integer, and
    the derivation keeps every digit: a session gate of 1,000,000 s needs exactly 1,001,894 s."""
    with pytest.raises(ConfigError) as caught:
        settings_of(
            write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 900, gate_timeout_s=1_000_000))
        )
    message = str(caught.value)
    assert "the least is 1001894 s" in message
    assert "182 + 906 + 4 + 182 + 1000000 + 300 + 320 = 1001894" in message
    assert message.endswith("[tokens] margin_s = 1001894")
    assert "e+" not in message


def test_o35_the_refusal_names_the_session_drain_a_clear_that_takes_the_federation_itself_waits(tmp_path):
    """O35, O11: the injector can always hold an exclusive admission of its own, whatever its run holds: a clear that
    finds none held takes the federation itself, and that wait passes a drain record still standing, up to
    `session_drain_s`, before it restores. So the least counts the drain, and the refusal names it with its declared
    value: 320 s at the defaults, for a least of 2,134 s. The clear's own drain after the restore runs from the
    restore's start and ends inside the restore's 1,514 s, so it adds nothing here."""
    with pytest.raises(ConfigError) as caught:
        settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", DEFAULT_MARGIN_S)))
    detail = str(caught.value).split("the least is", 1)[1]
    assert detail.startswith(" 2134 s, the restore, its gate and the session drain before the tool server's stop")
    assert f"slack 300 s + {DRAIN_WORDS} (session_drain_s 320 s): " in detail
    assert "+ 300 + 320 = 2134" in detail
    assert "past the restore's bound" not in detail


def test_o35_a_session_drain_of_zero_takes_its_term_out_of_the_least(tmp_path):
    """O35, O11: `session_drain_s = 0` declares no drain: a clear then takes no admission and waits for none, so the
    derivation has no drain term and the least is the restore's bound and the slack, 1,814 s at the other defaults."""

    def undrained(margin_s):
        return write_platform(tmp_path, lambda data: at_default_bounds(data, "off", margin_s, session_drain_s=0))

    with pytest.raises(ConfigError) as caught:
        settings_of(undrained(DEFAULT_MARGIN_S))
    detail = str(caught.value).split("the least is", 1)[1]
    assert detail.startswith(" 1814 s, the restore and its gate before")
    assert "182 + 906 + 4 + 182 + 240 + 300 = 1814" in detail
    assert "session_drain_s" not in detail
    assert DRAIN_WORDS not in detail
    settings = settings_of(undrained(1814))
    assert (restore_drain_s(settings), restore_token_margin_s(settings)) == (0, 1814)


def test_o35_a_session_drain_longer_than_the_restore_adds_what_its_own_drain_outlasts(tmp_path):
    """O35, O11: the clear's own drain, after its restore, runs from the restore's start, so it adds only what outlasts
    the restore's bound: with `session_drain_s` of 2,000 s against the default bound of 1,514 s, the least is the
    bound, the slack, the standing drain waited before the restore (2,000 s) and the 486 s its own drain outlasts the
    restore: 4,300 s, each term named with its values."""

    def drained(margin_s):
        return write_platform(tmp_path, lambda data: at_default_bounds(data, "off", margin_s, session_drain_s=2000))

    with pytest.raises(ConfigError) as caught:
        settings_of(drained(DEFAULT_MARGIN_S))
    message = str(caught.value)
    past = "its own drain past the restore's bound (session_drain_s 2000 s - 1514 s)"
    assert f"{DRAIN_WORDS} (session_drain_s 2000 s) + {past}: " in message
    assert "182 + 906 + 4 + 182 + 240 + 300 + 2000 + 486 = 4300" in message
    assert message.endswith("[tokens] margin_s = 4300")
    with pytest.raises(ConfigError, match=r"margin_s = 4299 is too small: the least is 4300 s"):
        settings_of(drained(4299))
    settings = settings_of(drained(4300))
    assert restore_bound_s(settings) == 1514
    assert restore_drain_s(settings) == 2000 + (2000 - 1514)


def test_o35_a_platform_without_faults_needs_no_token_margin_declared(tmp_path):
    """O35, O06: without `[faults]` nothing changes: a platform that declares no `[tokens]` loads with the default
    margin, and no fault settings are read."""
    platform = write_platform(tmp_path, lambda data: data.pop("tokens", None))
    config = load_platform(platform, require_secrets=False)
    assert config.tokens.margin_s == 900
    assert fault_settings_of(config) is None


def test_o35_a_long_pause_counts_against_the_token_margin_since_each_wait_ends_one_pause_past_its_bound(tmp_path):
    """O35, O06: each wait checks its bound only after a reading, so a restore can end one pause past each bound: with
    `poll_s` of 2,000 s, a margin of 2,134 s, enough for the default pause, is refused, and the figure counts the pause
    after the stop's wait, after each start's wait and between them, and after the wait to serve."""
    with pytest.raises(ConfigError, match=r"margin_s = 2134 is too small: the least is .* s.*poll_s 2000 s"):
        settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 2134, poll_s=2000)))
    slow = settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 20000, poll_s=2000)))
    assert restore_token_margin_s(slow) == 2134 - 7 * 2 + 7 * 2000  # seven pauses with three starts


def test_o35_the_fixtures_restore_fits_the_default_margin_and_the_long_margin_adds_two_heals(tmp_path):
    """O35: the four-provider fixture's shorter bounds fit the default margin, so a declared heal loads with it; the
    long margin is that restore's margin plus two heals of `heal_timeout_s` and the session drain the start's recovery
    waits out (the fixture declares none), and nothing for a heal that is off. A drain declared counts in the restore's
    margin too (the clear's: 320 s for the standing record, and the 35 s its own drain outlasts the fixture's 285 s
    restore), and once more for the start's recovery in the long margin."""
    settings = settings_of(platform_with(tmp_path, heal="all_links"))
    assert load_platform(platform_with(tmp_path, heal="all_links"), require_secrets=False).tokens.margin_s == 900
    assert restore_token_margin_s(settings) == FIXTURE_RESTORE_MARGIN_S
    assert heal_token_margin_s(settings) == LONG_MARGIN_S == 585 + 2 * 4200
    drained = settings_of(platform_with(tmp_path, heal="all_links", session_drain_s=320, tokens={"margin_s": 940}))
    assert restore_bound_s(drained) == 285
    assert restore_token_margin_s(drained) == FIXTURE_RESTORE_MARGIN_S + 320 + (320 - 285) == 940
    assert heal_token_margin_s(drained) == 940 + 2 * 4200 + 320
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
        self.at_finish = None  # what happens after `running`'s exit, before `finish_instruments`
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

    def run(self, schema, key, order=NESTED):
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
            if self.at_finish is not None:
                self.at_finish()
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


def test_o35_an_executor_that_stops_the_tool_server_and_sandbox_before_the_restore_heals_with_the_default_margin(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the other study's order with the default margin (900 s): the tool server's stop, its re-verification
    included, and the sandbox's end before `running`'s exit, so the token may expire as the restore begins; the heal
    then runs for its whole bound, the restore is verified, and the store keeps the attempt done and uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    assert outage.config.tokens.margin_s == DEFAULT_MARGIN_S
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
    steps = [step["step"] for step in record.instruments["fault_injector"]["steps"]]
    assert "heal_skipped" not in steps
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
def test_o35_while_a_token_step_runs_the_restore_runs_its_heal_is_skipped_and_recorded_and_it_stays_unverified(
    tmp_path, fake, frozen_manifests, outage_condition, order, running
):
    """O35, O06: without the long margin, a heal that would begin while the run's tool server or its sandbox still
    runs is skipped: the provider is brought back and passes its gate, the reading records the skip with the steps
    that ran, the margin and the long margin, nothing heals, and the stuck pair leaves the restore unverified
    (`links_stuck`): the record is kept and refused as `injector_failure`, never scored, the injection stays open for
    the health gate, and the tool server's stop that follows finds its token valid. `finish` does not try the restore
    again while the step still runs, so one restore ran inside the token's window."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    record = outage.run(schema, key, order)
    assert ("start", TARGET) in acted(outage.services, "start")  # the restore ran
    assert outage.services.running(outage.injector._service)  # and the provider is back up
    assert acted(outage.services, "ping", "restart_link") == []
    assert "heal" not in outage.steps
    reading = record.instruments["fault_injector"]
    assert (reading["restore_verified"], reading["how"]) == (False, "links_stuck")
    (step,) = [step for step in reading["steps"] if step["step"] == "heal_skipped"]  # `running`'s exit alone
    assert (step["steps"], step["margin_s"], step["long_margin_s"]) == (running, DEFAULT_MARGIN_S, LONG_MARGIN_S)
    assert len([step for step in reading["steps"] if step["step"] == "cleared"]) == 1
    (retry,) = [step for step in reading["steps"] if step["step"] == "restore_retry"]
    assert (retry["ok"], retry["path"], retry["steps"]) == (False, "left_open", running)
    assert restore_refusal(record.instruments)["infra_kind"] == INJECTOR_FAILURE
    assert record.instrument_refusal["infra_kind"] == INJECTOR_FAILURE
    assert is_charged(INJECTOR_FAILURE)
    (entry,) = outage.injector._journal.open_entries()
    assert (entry["target"], entry["clear_failed"], entry.get("healed")) == (TARGET, "links_stuck", None)


def test_o35_the_next_start_with_no_token_step_running_heals_what_a_skipped_heal_left(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35: after a skipped heal the injection stays open with its pair stuck; once the run has ended, the next start
    under a fresh exclusive admission with no step of its run running recovers it, heals it and verifies it, and its
    own outage then goes ahead."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    outage.run(schema, key, NESTED)
    outage.injector._unlock()  # the run's process ends: the kernel drops its injector's lock on the target
    outage.services.actions.clear()
    following = injector_for(outage.platform, outage.services, target=TARGET, clock=outage.services.clock)
    with FederationLock(load_faults(outage.platform, outage.config)).held(exclusive=True, timeout_s=5):
        following.start({"run_key": "the-next-run", "attempt": 1})
        (recovered,) = [step for step in following.snapshot()["steps"] if step["step"] == "recovered"]
        assert following.clear() is True
    assert recovered["ok"] is True
    assert "heal_skipped" not in recovered
    assert acted(outage.services, "restart_link")  # the heal ran, with no step of a run counted
    assert outage.injector._journal.open_entries() == []


def test_o35_with_the_long_margin_declared_a_heal_inside_the_launches_runs_and_its_heals_fit(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the documented order with the long margin declared heals as before: a heal of its whole
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


def test_o35_the_long_margin_rests_on_the_token_outliving_the_heal_since_the_tool_servers_stop_reads_it(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the dependence the skip guards: in the documented order, with the long margin, a token that expired
    as the restore began fails the tool server's stop that follows as `tool_plane_integrity`, a charged kind, though the
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


def test_o35_a_starts_recovery_skips_its_heal_while_the_runs_launches_run_without_the_long_margin(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O06: the start recovers an injection an earlier run left, and may heal it; in the documented order the
    run's tool server and sandbox already run then, so without the long margin the recovery restores and skips its
    heal: the provider is back up, the skip is in the reading, and the pair that stays stuck fails the start closed as
    `injector_failure`, with no link restarted."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    left_open(outage)
    with pytest.raises(InfraError) as caught:
        outage.run(schema, key, NESTED)
    assert caught.value.kind == INJECTOR_FAILURE
    assert "earlier run left open" in caught.value.detail
    assert outage.services.running(outage.injector._service)
    assert acted(outage.services, "ping", "restart_link") == []
    (recovered,) = [step for step in outage.injector.snapshot()["steps"] if step["step"] == "recovered"]
    assert recovered["how"] == "links_stuck"
    assert recovered["heal_skipped"]["steps"] == [SANDBOX_STEP, TOOL_SERVER_STEP]


def test_o35_a_starts_recovery_with_no_token_step_begun_heals(tmp_path, fake, frozen_manifests, outage_condition):
    """O35: in the other study's order the injector starts before the sandbox and the tool server, so its recovery of an
    injection an earlier run left heals with no step of the run begun, and the run completes, done and uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    left_open(outage)
    record = outage.run(schema, key, STOPPED_FIRST)
    assert acted(outage.services, "restart_link")
    (recovered,) = [step for step in record.instruments["fault_injector"]["steps"] if step["step"] == "recovered"]
    assert "heal_skipped" not in recovered
    assert record.instruments["fault_injector"]["restore_verified"] is True
    assert stored(store, key, record) == ("done", 0, None)


@pytest.mark.parametrize("check", ["_prepare", "_recheck"])
def test_o35_a_cancelled_tool_server_launch_counts_its_step_until_its_check_thread_ends(
    tmp_path, fake, frozen_manifests, monkeypatch, check
):
    """O35: a cancellation does not stop the worker thread that examines the run's token, before the start or at the
    stop, so the tool server's step stays counted on the run's admission until that thread has ended, though the
    launch has already left: no heal starts while the token is still being examined."""
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


def test_o35_a_token_step_without_the_runs_admission_is_refused_naming_it(tmp_path):
    """O35, O11: on a platform with `[faults]`, a step that uses the run's tokens (the tool server's checks, the
    sandbox's mint and start) starts only under the run's admission, held now: without one it is refused before it
    starts, failing closed with an `AdmissionRefused` that names the step and what it was about to do, so no step runs
    that the injector's heal cannot see and no token is minted before the run's admission was granted."""
    config = load_platform(platform_with(tmp_path), require_secrets=False)
    with pytest.raises(TokenStepRefused) as caught:
        begin_token_step(SANDBOX_STEP, config, "the sandbox's mint")
    assert isinstance(caught.value, AdmissionRefused)
    assert caught.value.step == SANDBOX_STEP
    assert "the sandbox's mint (the run's sandbox step) uses the run's tokens" in str(caught.value)
    assert "starts only under the run's admission" in str(caught.value)


def test_o35_without_faults_a_token_step_needs_no_admission(tmp_path):
    """O35: a platform without `[faults]` has no injector and no admission, so a step that uses the run's tokens starts
    without one and nothing is counted."""
    config = load_platform(write_platform(tmp_path), require_secrets=False)
    end = begin_token_step(TOOL_SERVER_STEP, config, "the tool server's check before its start")
    end()


def test_o35_the_tool_servers_check_at_its_stop_without_the_runs_admission_is_refused_naming_it(
    tmp_path, fake, frozen_manifests
):
    """O35, O11: a run whose admission was let go while its tool server still ran cannot re-verify the plane at the
    stop uncounted: the check that examines the token is refused before its thread starts, naming the step, and the
    launch fails closed with that refusal."""
    outage = Outage(tmp_path / "federation", fake, frozen_manifests)
    lock = FederationLock(load_faults(outage.platform, outage.config))

    async def go(admission):
        async with outage.tool_server():
            admission.close()  # the run's admission is let go while its tool server still runs

    with lock.held(exclusive=True, timeout_s=5) as admission, pytest.raises(TokenStepRefused) as caught:
        asyncio.run(go(admission))
    assert caught.value.step == TOOL_SERVER_STEP
    assert "the tool server's check at its stop (the run's tool_server step)" in str(caught.value)
    assert "tool_server_stopped" not in outage.steps


def serving_only_after_finish(outage):
    """The restore at `running`'s exit finds its provider back but never serving (`not_serving`), and it serves from
    `finish_instruments` on: a retry then verifies it, with no link stuck."""
    outage.services.stuck = set()
    outage.at_restore = lambda: outage.services.knobs.__setitem__("never_serves", True)
    outage.at_finish = lambda: outage.services.knobs.__setitem__("never_serves", False)


def test_o35_o11_with_the_launches_stopped_first_a_failed_exit_clear_is_retried_once_the_steps_have_ended(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O11: in the other study's order the sandbox and the tool server stop before `running`'s exit, so when the
    restore there fails, `finish` tries it again with no step of the run running, outside the token's window: the
    provider serves, the restore is verified, the reading names the path taken and why, and the attempt is done and
    uncharged."""
    schema, store, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests, services=HealServices())
    serving_only_after_finish(outage)
    record = outage.run(schema, key, STOPPED_FIRST)
    reading = record.instruments["fault_injector"]
    cleared = [step for step in reading["steps"] if step["step"] == "cleared"]
    assert [(step["ok"], step["how"]) for step in cleared] == [(False, "not_serving"), (True, "found_restored")]
    (retry,) = [step for step in reading["steps"] if step["step"] == "restore_retry"]
    assert (retry["ok"], retry["path"], retry["steps"]) == (True, "after_steps", [])
    assert "no step of the run that uses its tokens still runs" in retry["why"]
    assert reading["restore_verified"] is True
    assert outage.services.running(outage.injector._service)
    assert outage.injector._journal.open_entries() == []
    assert stored(store, key, record) == ("done", 0, None)


def test_o35_o11_in_the_documented_order_a_failed_exit_clear_is_left_open_for_the_next_starts_recovery(
    tmp_path, fake, frozen_manifests, outage_condition
):
    """O35, O11: in the documented order the tool server and the sandbox still run at `finish`, inside the token's
    window, so a restore that failed at `running`'s exit is not tried again there: one restore ran in the window, as
    the least margin counts. The reading names the path and the steps that ran, the injection stays open in the
    journal, its target's lock passed to the run's admission, the record is refused as `injector_failure` and never
    scored (#245), and the tool server's stop finds its token valid. Once the run has ended, the next start's recovery
    restores it."""
    schema, _, key = outage_condition
    outage = Outage(tmp_path / "federation", fake, frozen_manifests, services=HealServices())
    serving_only_after_finish(outage)
    record = outage.run(schema, key, NESTED)
    assert outage.steps[-2:] == ["tool_server_stopped", "sandbox_stop"]  # the token examined, still valid
    reading = record.instruments["fault_injector"]
    cleared = [step for step in reading["steps"] if step["step"] == "cleared"]
    assert [(step["ok"], step["how"]) for step in cleared] == [(False, "not_serving")]
    (retry,) = [step for step in reading["steps"] if step["step"] == "restore_retry"]
    assert (retry["ok"], retry["path"], retry["steps"]) == (False, "left_open", [SANDBOX_STEP, TOOL_SERVER_STEP])
    assert "the next start's recovery" in retry["why"]
    assert (reading["restore_verified"], reading["how"]) == (False, "not_serving")
    assert restore_refusal(record.instruments)["infra_kind"] == INJECTOR_FAILURE  # the campaign sets it aside
    assert record.instrument_refusal["infra_kind"] == INJECTOR_FAILURE
    assert "stays open in the journal" in record.instrument_refusal["detail"]
    assert outage.injector._lock is None  # handed over: its lock went with the run's admission, so recovery may act
    (entry,) = outage.injector._journal.open_entries()
    assert (entry["target"], entry["clear_failed"]) == (TARGET, "not_serving")
    following = injector_for(outage.platform, outage.services, target=TARGET, clock=outage.services.clock)
    with FederationLock(load_faults(outage.platform, outage.config)).held(exclusive=True, timeout_s=5):
        following.start({"run_key": "the-next-run", "attempt": 1})
        (recovered,) = [step for step in following.snapshot()["steps"] if step["step"] == "recovered"]
        assert following.clear() is True
    assert (recovered["ok"], recovered["how"]) == (True, "found_restored")
    assert outage.injector._journal.open_entries() == []


def test_o35_o11_an_injection_handed_to_recovery_is_restored_by_nothing_of_its_run_while_a_token_step_runs(tmp_path):
    """O35, O11: an injection handed to recovery is not restored inside its run's token window by any other path: its
    target's lock goes only with the run's admission, so while the run's tool server still runs, a later `clear`
    reports it still down, `recover` restores nothing and a second `start` of the injector fails closed as
    `injector_failure`, with no start or serving read of the provider. Once the run's admission has ended, the next
    start's recovery restores it."""
    services = FakeServices()
    platform = write_faults(tmp_path)
    clock = FakeClock()
    injector = injector_for(platform, services, clock=clock)
    lock = FederationLock(load_faults(platform, load_platform(platform, require_secrets=False)))
    with lock.held(exclusive=True, timeout_s=5), token_step(TOOL_SERVER_STEP):
        injector.start({"run_key": "the-run", "attempt": 1})
        services.knobs["never_serves"] = True
        assert injector.clear() is False  # the restore at the exit: back, never serving
        services.knobs["never_serves"] = False
        assert injector.clear() is False  # handed to recovery: the tool server still runs
        assert injector.clear() is False  # and still down, as far as this injector knows
        acted_before = [call for call in services.calls if call[0] in ("start", "serves")]
        assert injector.recover() == [OUTAGE_TARGET]  # held for its run: nothing restored
        with pytest.raises(InfraError) as caught:
            injector.start({"run_key": "the-run", "attempt": 2})
        assert caught.value.kind == INJECTOR_FAILURE
        assert [call for call in services.calls if call[0] in ("start", "serves")] == acted_before
    (entry,) = injector._journal.open_entries()
    assert entry["clear_failed"] == "not_serving"
    following = injector_for(platform, services, clock=clock)
    with lock.held(exclusive=True, timeout_s=5):
        following.start({"run_key": "the-next-run", "attempt": 1})
        (recovered,) = [step for step in following.snapshot()["steps"] if step["step"] == "recovered"]
        assert following.clear() is True
    assert (recovered["ok"], recovered["how"]) == (True, "found_restored")
    assert injector._journal.open_entries() == []


def test_o35_the_least_margin_counts_one_restore_since_none_is_retried_inside_the_window(tmp_path):
    """O35, O06: no restore is tried again inside the token's window, so the least margin counts one restore and its
    gate, unchanged: each of the restore's terms once, 2,134 s at the defaults."""
    settings = settings_of(write_platform(tmp_path, lambda data: at_default_bounds(data, "off", 2134)))
    derivation = restore_margin_derivation(settings)
    for term in ("a stop of unknown outcome", "starts each with its wait", "the wait to serve", "the session gate ("):
        assert derivation.count(term) == 1, term
    assert restore_token_margin_s(settings) == 2134
