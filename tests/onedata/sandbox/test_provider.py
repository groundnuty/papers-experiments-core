"""The sandbox as the platform offers it: leased users, tokens by data path, waits, and the teardown probe (C19)."""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import NOW
from sandbox_support import FAST

from expcore.campaign.lease import LeasePool
from expcore.errors import InfraError
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.provider import DATA_USERS, SandboxPlatform, SandboxProbe
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.tokens import TokenRefused

CEILING_S = 600.0
KEY = "login-data"


class RecordingContext:
    """A call context that records each wait it is asked for, with a marker for when a body runs inside it."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        self.events.append(("enter", kind))
        try:
            yield
        finally:
            self.events.append(("exit", kind))


@pytest.fixture
def stage(config, issuer, fake):
    """A platform over a fake host and the fake federation, with the host knowing the configured images."""
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    options = {"waits": FAST, "sample_period_s": None, "sleep": lambda seconds: None}
    registry = Registry()
    platform = SandboxPlatform(config, issuer, host, registry=registry, sandbox_options=options)
    return SimpleNamespace(host=host, platform=platform, config=config, s=s, fake=fake, registry=registry)


def lease_pool(stage, tmp_path):
    return LeasePool(
        tmp_path / "leases",
        stage.platform.lease_keys,
        stage.platform.probe(),
        clock=lambda: 0.0,
        sleep=lambda s: None,
        quarantine_retry_s=0.0,
    )


def run_launch(stage, level, lease=None, body=None, ctx=None):
    ctx = ctx or RecordingContext()

    async def go():
        async with stage.platform.launch(
            ctx, run_key="run-1", level=level, ceiling_s=CEILING_S, lease=lease
        ) as sandbox:
            if body:
                await body(sandbox)
            return sandbox

    return asyncio.run(go()), ctx


def test_the_levels_of_the_data_path_factor_decide_the_lease_group_and_the_mode(stage):
    platform = stage.platform
    assert platform.mode({"data_path": "mounted"}) is DataPathMode.MOUNT
    assert platform.lease_group({"data_path": "mounted"}) == DATA_USERS
    assert platform.lease_group({"data_path": "via-gate"}) == DATA_USERS
    assert platform.lease_group({"data_path": "isolated"}) is None
    assert platform.lease_keys(DATA_USERS) == ["login-data", "login-data2"]
    assert platform.lease_keys("something else") == ()
    with pytest.raises(ValueError, match="does not map the level"):
        platform.mode({"data_path": "unmapped"})


def test_c19_launch_waits_only_for_the_start_and_the_teardown(stage, tmp_path):
    """C19, L04: the start and the stop are each inside the sandbox_lifecycle wait; the body is the agent's own time."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(DATA_USERS, run_key="run-1")
    seen_inside = []

    async def body(sandbox):
        seen_inside.append(list(ctx.events))

    ctx = RecordingContext()
    run_launch(stage, "via-gate", lease, body, ctx)
    assert seen_inside == [[("enter", "sandbox_lifecycle"), ("exit", "sandbox_lifecycle")]]  # closed before the body
    assert ctx.events == [("enter", "sandbox_lifecycle"), ("exit", "sandbox_lifecycle")] * 2
    assert pool.finish(lease) is True
    assert not stage.host.containers


@pytest.mark.parametrize(("level", "interface"), [("via-gate", "rest"), ("mounted", "oneclient")])
def test_o05_each_data_path_gets_a_read_only_token_of_its_own_interface_minted_by_the_leased_user(
    stage, level, interface, tmp_path
):
    """O05: the leased user mints its own token, with the interface of the path it is for."""
    lease = lease_pool(stage, tmp_path).acquire(DATA_USERS, run_key="run-1")
    sandbox, _ = run_launch(stage, level, lease)
    mint = [r for r in stage.fake.requests if r.path.endswith("/user/tokens/temporary")][-1]
    caveats = {c["type"]: c for c in json.loads(mint.body)["caveats"]}
    assert "data.readonly" in caveats
    assert caveats["interface"]["interface"] == interface
    assert sandbox.metadata()["data_path_token"]["subject"] == "u-data"
    assert "token" not in json.dumps(sandbox.metadata()["data_path_token"]).lower().replace("data_path_token", "")


def test_a_run_with_no_data_path_mints_no_token(stage):
    before = len(stage.fake.requests)
    sandbox, _ = run_launch(stage, "isolated")
    assert len(stage.fake.requests) == before
    assert sandbox.metadata()["data_path"] == "none"


def test_a_lease_key_that_is_not_a_configured_user_is_refused(stage):
    lease = SimpleNamespace(key="intruder", run_key="run-1")
    with pytest.raises(ValueError, match="not a login"):
        run_launch(stage, "via-gate", lease)
    assert not stage.host.containers


def test_o05_a_token_the_platform_cannot_vouch_for_refuses_the_run_before_any_container(stage, tmp_path):
    """O05: fail closed. A user without exactly view and read is refused, and nothing starts."""
    stage.fake.grant("u-data", ["space_view", "space_read_data", "space_manage_qos"])
    lease = lease_pool(stage, tmp_path).acquire(DATA_USERS, run_key="run-1")
    with pytest.raises(TokenRefused):
        run_launch(stage, "via-gate", lease)
    assert not stage.host.containers
    assert not stage.host.networks


def test_a_teardown_that_leaves_something_raises_a_sandbox_error_and_keeps_the_lease_quarantined(stage, tmp_path):
    """C19: nothing frees the key unless the probe shows the sandbox gone."""
    pool = lease_pool(stage, tmp_path)
    lease = pool.acquire(DATA_USERS, run_key="run-1")

    async def body(sandbox):
        stage.host.fail_remove.add(sandbox.names.gate)

    with pytest.raises(InfraError) as caught:
        run_launch(stage, "via-gate", lease, body)
    assert caught.value.kind == "sandbox"
    assert pool.finish(lease) is False  # the probe finds the gate container: the key stays held
    assert lease.quarantined
    stage.host.fail_remove.clear()
    for name in list(stage.host.containers):
        stage.host.docker("rm", "-f", name)
    for name in list(stage.host.networks):
        stage.host.docker("network", "rm", name)
    stage.host.mounts.clear()
    assert pool.settle_quarantines() == [KEY]


def test_a_body_that_fails_still_stops_the_sandbox_and_the_error_is_the_bodys(stage):
    async def body(sandbox):
        raise RuntimeError("the agent's loop failed")

    with pytest.raises(RuntimeError, match="the agent's loop failed"):
        run_launch(stage, "via-gate", None, body)
    assert not stage.host.containers


def test_a_body_that_fails_while_the_teardown_also_fails_keeps_the_bodys_error(stage):
    async def body(sandbox):
        stage.host.fail_remove.add(sandbox.names.gate)
        raise RuntimeError("the agent's loop failed")

    with pytest.raises(RuntimeError, match="the agent's loop failed"):
        run_launch(stage, "via-gate", None, body)


def probe_world(stage, tmp_path):
    """A started sandbox under a lease key, and a probe over the same host."""
    sandbox = None

    async def keep(s):
        nonlocal sandbox
        sandbox = s

    lease = lease_pool(stage, tmp_path).acquire(DATA_USERS, run_key="run-1")
    run_launch(stage, "mounted", lease, keep)
    return sandbox, SandboxProbe(stage.host, stage.s)


def test_c19_the_probe_says_exactly_true_only_when_container_network_and_mount_are_gone(stage, tmp_path):
    """C19: True only when nothing started with the key is left; the answer is True itself, not a truthy value."""
    _, probe = probe_world(stage, tmp_path)
    assert probe.resource_gone(KEY, {}) is True  # the launch has stopped it
    # a leftover of each kind, one at a time, makes the proof fail
    stage.host.docker("run", "-d", "--name", "leftover", "--label", f"org.test.lease={KEY}", "agent:1")
    assert probe.resource_gone(KEY, {}) is False
    stage.host.docker("rm", "-f", "leftover")
    stage.host.docker(
        "network", "create", "--subnet", "10.77.0.0/28", "--label", f"org.test.lease={KEY}", "leftover-net"
    )
    assert probe.resource_gone(KEY, {}) is False
    stage.host.docker("network", "rm", "leftover-net")
    stage.host.mounts[str(stage.s.work_root / f"{KEY}-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is False
    stage.host.mounts.clear()
    assert probe.resource_gone(KEY, {}) is True


def test_c19_the_probe_is_about_its_own_key_only(stage, tmp_path):
    _, probe = probe_world(stage, tmp_path)
    stage.host.docker("run", "-d", "--name", "other", "--label", "org.test.lease=login-data2", "agent:1")
    stage.host.mounts[str(stage.s.work_root / "login-data2-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is True
    assert probe.resource_gone("login-data2", {}) is False
    stage.host.mounts[str(stage.s.work_root / "login-data-extra-0123abcd" / "work")] = "ext4"
    assert probe.resource_gone(KEY, {}) is True  # a key that merely starts with this one is another key


def test_c19_a_probe_that_cannot_ask_fails_safe(stage, tmp_path):
    """C19: both questions fail safe: a docker that does not answer or an unreadable mount table is not proof."""
    _, probe = probe_world(stage, tmp_path)
    stage.host.docker = lambda *a, **k: CmdResult(1, "", "daemon down")
    assert probe.resource_gone(KEY, {}) is False
    del stage.host.docker
    original = stage.host.read_text

    def unreadable(path):
        if str(path) == "/proc/self/mountinfo":
            raise OSError("no mount table")
        return original(path)

    stage.host.read_text = unreadable
    assert probe.resource_gone(KEY, {}) is False


def test_c19_the_probe_asks_the_cores_check_whether_the_holder_is_gone(stage):
    calls = []
    probe = SandboxProbe(stage.host, stage.s, holder_gone=lambda owner: calls.append(owner) or None)
    assert probe.holder_gone("some-owner") is None
    assert calls == ["some-owner"]


def test_the_clock_of_the_token_follows_the_runs_ceiling(stage, tmp_path):
    """O06: the token's expiry is derived from the whole-time ceiling the launch is given."""
    lease = lease_pool(stage, tmp_path).acquire(DATA_USERS, run_key="run-1")
    sandbox, _ = run_launch(stage, "via-gate", lease)
    expires = sandbox.metadata()["data_path_token"]["expires_at"]
    assert expires >= NOW + CEILING_S + stage.config.tokens.margin_s
