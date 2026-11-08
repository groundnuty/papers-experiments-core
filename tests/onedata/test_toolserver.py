"""The tool-server launch: allow-list and token from the condition, the frozen manifest, the wait kind, pre-checks."""

import asyncio
import contextlib
import json
import shutil
import sys
import time

import pytest
from mcp import StdioServerParameters
from platform_support import CEILING_S, FAKE_TOOL_SERVER, MANAGE, NOW, write_platform
from record_support import make_config

from expcore.errors import InfraError
from expcore.plugin import check_wait_kind
from expcore.tools.manifest import FrozenManifest, ManifestError, live_manifest
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.pin import PinError
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import Plane, TokenIssuer, data_path_spec, tool_server_spec
from expcore_onedata.toolserver import (
    ENV_PROVIDER_HOST,
    ENV_PROVIDER_TOKEN,
    ENV_TOOLS,
    ENV_ZONE_HOST,
    ENV_ZONE_TOKEN,
    WAIT_KIND,
    ToolServerLauncher,
    server_environment,
)


class RecordingContext:
    """A call context that logs the waits opened and closed, and checks the kind as the real one does."""

    def __init__(self):
        self.events = []

    def infrastructure_wait(self, kind):
        check_wait_kind(kind)
        return self._wait(kind)

    @contextlib.asynccontextmanager
    async def _wait(self, kind):
        self.events.append(("open", kind))
        try:
            yield
        finally:
            self.events.append(("close", kind))


def freeze(path, tools):
    """Freeze the manifest of the stand-in server with this allow-list into a file."""
    params = StdioServerParameters(command=sys.executable, args=[FAKE_TOOL_SERVER], env={ENV_TOOLS: tools})
    path.write_text(FrozenManifest.from_live(asyncio.run(live_manifest(params))).dump(), encoding="utf-8")


@pytest.fixture(scope="module")
def frozen_manifests(tmp_path_factory):
    """Freeze each condition's manifest once for the module, because starting a server for it takes seconds."""
    directory = tmp_path_factory.mktemp("manifests")
    freeze(directory / "read.manifest.json", "echo,add")
    freeze(directory / "manage.manifest.json", "echo,add,fail")
    return directory


def use_frozen(tmp_path, directory):
    """Put the module's frozen manifests where this test's platform.toml looks for them."""
    for name in ("read.manifest.json", "manage.manifest.json"):
        shutil.copy(directory / name, tmp_path / name)


@pytest.fixture
def world(tmp_path, fake, frozen_manifests):
    """A configuration whose conditions have real frozen manifests, with its client, issuer and launcher."""
    use_frozen(tmp_path, frozen_manifests)
    config = load_platform(write_platform(tmp_path))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    return config, issuer, ToolServerLauncher(config, issuer)


def read_token(issuer, config):
    return issuer.mint(tool_server_spec(config.space.id, config.condition("read")), "u-tools", ceiling_s=CEILING_S)


def start(launcher, ctx, condition, token, tmp_path, *, ceiling_s=CEILING_S, record=True):
    """Launch, call echo once, leave; return the settings the server saw."""
    seen = tmp_path / "seen.json"

    async def go():
        env = {"FAKE_MCP_RECORD": str(seen)} if record else None
        async with launcher.launch(ctx, condition, token, ceiling_s=ceiling_s, extra_env=env) as provider:
            tools = sorted(t.name for t in provider.tools())
            echoed = await provider.execute("echo", {"text": "hi"}, timeout_s=10, ctx=ctx)
        return tools, echoed

    tools, echoed = asyncio.run(go())
    return tools, echoed, json.loads(seen.read_text()) if record else None


def test_the_server_starts_with_the_conditions_allow_list_the_token_and_the_pinned_hosts(world, fake, tmp_path):
    """The child gets the condition's allow-list, the plane's token for both settings and the local provider."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    tools, echoed, seen = start(launcher, RecordingContext(), "read", token, tmp_path)
    assert tools == ["add", "echo"]
    assert json.loads(echoed.text) == {"result": "hi"}
    assert seen[ENV_TOOLS] == "echo,add"
    assert seen[ENV_ZONE_TOKEN] == seen[ENV_PROVIDER_TOKEN] == token.reveal()
    assert (seen[ENV_ZONE_HOST], seen[ENV_PROVIDER_HOST]) == ("https://zone.test", "https://alpha.test")


def test_each_condition_gets_its_own_allow_list_and_manifest(world, fake, tmp_path):
    """Two conditions differing in tool exposure and rights run from one configuration side by side."""
    config, issuer, launcher = world
    fake.grant("u-tools", MANAGE)
    token = issuer.mint(tool_server_spec(config.space.id, config.condition("manage")), "u-tools", ceiling_s=CEILING_S)
    tools, _, seen = start(launcher, RecordingContext(), "manage", token, tmp_path)
    assert tools == ["add", "echo", "fail"]
    assert seen[ENV_TOOLS] == "echo,add,fail"


def test_the_start_and_the_stop_are_each_one_toolserver_lifecycle_wait_and_nothing_else_is_a_wait(world, tmp_path):
    """The only wait kind used is toolserver_lifecycle: one around the start, one around the stop, none between."""
    config, issuer, launcher = world
    ctx = RecordingContext()
    start(launcher, ctx, "read", read_token(issuer, config), tmp_path)
    assert ctx.events == [("open", WAIT_KIND), ("close", WAIT_KIND), ("open", WAIT_KIND), ("close", WAIT_KIND)]
    assert WAIT_KIND == "toolserver_lifecycle"


def test_a_real_timer_measures_the_lifecycle_waits_and_no_other_kind(world, tmp_path):
    """Against the attempt's real timer, the waits are measured under the one kind, twice, and calls add none."""
    config, issuer, launcher = world
    timer = make_config().timer(time.monotonic)
    timer.start()
    start(launcher, timer, "read", read_token(issuer, config), tmp_path)
    assert set(timer.infra_wait_by_kind) == {"toolserver_lifecycle"}
    assert [label for label, _ in timer.readings if label.startswith("wait_start")] == [
        "wait_start:toolserver_lifecycle"
    ] * 2


def test_the_wait_around_the_stop_is_entered_when_the_body_fails(world, tmp_path):
    """A failure inside the run still stops the server inside its wait."""
    config, issuer, launcher = world
    ctx = RecordingContext()

    async def go():
        async with launcher.launch(ctx, "read", read_token(issuer, config), ceiling_s=CEILING_S):
            raise RuntimeError("the run failed")

    with pytest.raises(RuntimeError, match="the run failed"):
        asyncio.run(go())
    assert ctx.events[-2:] == [("open", WAIT_KIND), ("close", WAIT_KIND)]
    assert len(ctx.events) == 4


def test_l13_a_server_that_exposes_a_tool_outside_the_frozen_manifest_fails_the_start(world, tmp_path):
    """L13: the client checks the server's tools against the frozen manifest of the condition; the start fails."""
    config, issuer, _ = world
    # The read condition's server shows echo and add; point it at the manifest frozen with a third tool.
    drifted = load_platform(
        write_platform(
            tmp_path, lambda d: d["toolserver"]["conditions"]["read"].update(manifest="manage.manifest.json")
        )
    )
    launcher = ToolServerLauncher(drifted, issuer)
    with pytest.raises(ManifestError, match="fail"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path, record=False)


def test_a_token_of_the_other_plane_does_not_start_the_server(world, fake, tmp_path):
    """The tool server takes the tool-server plane's token only; nothing is spawned for another."""
    config, issuer, launcher = world
    data = issuer.mint(data_path_spec(config.space.id), "u-data", ceiling_s=CEILING_S)
    assert data.plane is Plane.DATA_PATH
    with pytest.raises(ValueError, match="tool-server plane"):
        start(launcher, RecordingContext(), "read", data, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_a_token_that_no_longer_examines_cleanly_does_not_start_the_server(world, fake, tmp_path):
    """The token is examined again at launch, fail-closed: a revoked token cannot start the plane."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    fake.issued.clear()
    with pytest.raises(TokenRefused, match="could not be examined"):
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_o06_a_token_that_cannot_last_the_run_does_not_start_the_server(world, tmp_path):
    """O06: launching for a longer ceiling than the token was minted for is refused before the spawn."""
    config, issuer, launcher = world
    with pytest.raises(TokenRefused, match="shorter"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path, ceiling_s=7200)
    assert not (tmp_path / "seen.json").exists()


def test_a_federation_that_cannot_examine_the_token_is_infrastructure_of_kind_knowledge(world, fake, tmp_path):
    """An outage of Onezone while the token is examined at launch is an infrastructure failure, not a defect."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    fake.status_override = 503
    with pytest.raises(InfraError) as caught:
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert caught.value.kind == "knowledge"
    assert token.reveal() not in str(caught.value)


def test_a_condition_the_file_does_not_have_is_named(world, tmp_path):
    config, issuer, launcher = world
    with pytest.raises(Exception, match="no condition 'nope'"):
        start(launcher, RecordingContext(), "nope", read_token(issuer, config), tmp_path)


def test_the_allow_list_and_the_other_managed_settings_cannot_be_set_by_the_caller(world):
    config, issuer, _ = world
    token = read_token(issuer, config)
    condition = config.condition("read")
    for name in (ENV_TOOLS, ENV_ZONE_TOKEN, ENV_PROVIDER_HOST):
        with pytest.raises(ValueError, match=name):
            server_environment(config, condition, token, {name: "x"})
    env = server_environment(config, condition, token, {"OTHER": "kept"})
    assert env["OTHER"] == "kept"
    assert "ONEDATA_ALLOW_INSECURE_TLS" not in env


def test_insecure_tls_is_a_setting_of_the_file(tmp_path, fake, frozen_manifests):
    use_frozen(tmp_path, frozen_manifests)
    config = load_platform(write_platform(tmp_path, lambda d: d["toolserver"].update(insecure_tls=True)))
    client = OnezoneClient(config, http=fake.client_http())
    token = TokenIssuer(client, config, clock=lambda: NOW).mint(
        tool_server_spec(config.space.id, config.condition("read")), "u-tools", ceiling_s=CEILING_S
    )
    assert server_environment(config, config.condition("read"), token)["ONEDATA_ALLOW_INSECURE_TLS"] == "true"


def test_a_pinned_checkout_is_verified_before_the_spawn(world, make_repo, tmp_path):
    """The configured checkout must be at the pinned commit with no local change, or nothing is spawned (pin.py)."""
    config, issuer, _ = world
    clone = make_repo("server")
    clone.write("server.py", "print(1)\n")
    commit = clone.commit("pinned")

    def pin(data):
        data["toolserver"].update(clone=str(clone.path), commit=commit)

    pinned = load_platform(write_platform(tmp_path, pin))
    launcher = ToolServerLauncher(pinned, issuer)
    start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path)  # clean and at the commit
    (tmp_path / "seen.json").unlink()
    clone.write("server.py", "print(2)\n")  # a local change
    with pytest.raises(PinError, match="local changes"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path)
    assert not (tmp_path / "seen.json").exists()
    clone.git("checkout", "--", "server.py")
    clone.write("other.py", "x = 1\n")
    other = clone.commit("moved on")
    assert other != commit
    with pytest.raises(PinError, match="not at the pinned commit"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path)
