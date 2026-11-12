"""The tool-server launch: allow-list and token from the condition, the frozen manifest, the wait kind, pre-checks."""

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from record_support import make_config
from tls_support import HAS_OPENSSL, TlsServer, make_federation_ca, swap_after_check

from expcore.campaign.attempts import classify
from expcore.campaign.executor import failure_result
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore.plugin import check_wait_kind
from expcore.tools.manifest import FrozenManifest, ManifestError, live_manifest
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.pin import PinError
from expcore_onedata.testing import ALPHA_ID, BASELINE, CEILING_S, MANAGE, NOW, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import DATA_PATH_USER as DATA
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.tls import CA_BUNDLE_ENV
from expcore_onedata.tokens import Plane, TokenIssuer, data_path_spec, token_window, tool_server_spec
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
    params = StdioServerParameters(command=sys.executable, args=TOOL_SERVER_ARGS, env={ENV_TOOLS: tools})
    path.write_text(FrozenManifest.from_live(asyncio.run(live_manifest(params))).dump(), encoding="utf-8")


class Events(list):
    """The event sink of a launcher in a test: it collects the (kind, detail) pairs it is called with."""

    def __call__(self, kind, detail):
        self.append((kind, detail))


@pytest.fixture
def events():
    return Events()


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
def world(tmp_path, fake, frozen_manifests, events):
    """A configuration whose conditions have real frozen manifests, with its client, issuer and launcher."""
    use_frozen(tmp_path, frozen_manifests)
    config = load_platform(write_platform(tmp_path))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    return config, issuer, ToolServerLauncher(config, issuer, events=events)


def read_token(issuer, config):
    return issuer.mint(tool_server_spec(config.space.id, config.condition("read")), TOOLS, ceiling_s=CEILING_S)


def start(launcher, ctx, condition, token, tmp_path, *, ceiling_s=CEILING_S, record=True, extra=None):
    """Launch, call echo once, leave; return the settings the server saw. `extra` adds to the server's environment."""
    seen = tmp_path / "seen.json"

    async def go():
        env = {"FAKE_MCP_RECORD": str(seen), **(extra or {})} if record else None
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
    fake.grant(TOOLS.user_id, MANAGE)
    token = issuer.mint(tool_server_spec(config.space.id, config.condition("manage")), TOOLS, ceiling_s=CEILING_S)
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


def test_l13_a_server_that_exposes_a_tool_outside_the_frozen_manifest_fails_the_start(world, tmp_path, events):
    """L13: the client checks the server's tools against the frozen manifest of the condition; the start fails."""
    config, issuer, _ = world
    # The read condition's server shows echo and add; point it at the manifest frozen with a third tool.
    drifted = load_platform(
        write_platform(
            tmp_path, lambda d: d["toolserver"]["conditions"]["read"].update(manifest="manage.manifest.json")
        )
    )
    launcher = ToolServerLauncher(drifted, issuer, events=events)
    with pytest.raises(ManifestError, match="fail"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path, record=False)


def test_a_token_of_the_other_plane_does_not_start_the_server(world, fake, tmp_path):
    """The tool server takes the tool-server plane's token only; nothing is spawned for another."""
    config, issuer, launcher = world
    data = issuer.mint(data_path_spec(config.space.id), DATA, ceiling_s=CEILING_S)
    assert data.plane is Plane.DATA_PATH
    with pytest.raises(ValueError, match="tool-server plane"):
        start(launcher, RecordingContext(), "read", data, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_a_token_that_no_longer_examines_cleanly_does_not_start_the_server(world, fake, tmp_path):
    """The token is examined again at launch, fail-closed: a token Onezone no longer knows cannot start the plane."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    fake.issued.clear()
    with pytest.raises(InfraError, match="could not be examined"):
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_o06_a_token_that_cannot_last_the_run_does_not_start_the_server(world, tmp_path):
    """O06: launching for a longer ceiling than the token was minted for is refused before the spawn."""
    config, issuer, launcher = world
    with pytest.raises(InfraError, match="shorter"):
        start(launcher, RecordingContext(), "read", read_token(issuer, config), tmp_path, ceiling_s=7200)
    assert not (tmp_path / "seen.json").exists()


REPLICATION = [*BASELINE, "space_schedule_replication"]


def with_replication_condition(data):
    """A third condition: view and read plus replication scheduling, beside the default read and manage ones."""
    data["toolserver"]["conditions"]["replicate"] = {
        "allow": ["echo", "add"],
        "rights": REPLICATION,
        "manifest": "manage.manifest.json",  # never read: every launch below is refused before the spawn
    }


@pytest.fixture
def three_conditions(tmp_path, fake, frozen_manifests, events):
    """A launcher over a configuration with the read, manage and replicate conditions, and the issuer it mints with."""
    use_frozen(tmp_path, frozen_manifests)
    config = load_platform(write_platform(tmp_path, with_replication_condition))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    return config, issuer, ToolServerLauncher(config, issuer, events=events)


def test_o05_o06_a_token_minted_for_one_conditions_rights_does_not_start_another_conditions_server(
    three_conditions, fake, tmp_path
):
    """O05, O06: a token minted for view, read and QoS management, which writes, carries the same caveats as one for
    view, read and replication, which also writes. Launching it for replication is refused before the spawn because
    the principal's privileges are not that condition's set; launching it for its own condition still works."""
    config, issuer, launcher = three_conditions
    fake.grant(TOOLS.user_id, MANAGE)
    token = issuer.mint(tool_server_spec(config.space.id, config.condition("manage")), TOOLS, ceiling_s=CEILING_S)
    caveats = fake.issued[token.reveal()]["caveats"]
    assert caveats == tool_server_spec(config.space.id, config.condition("replicate")).caveats.caveats(
        caveats[0]["validUntil"]
    )  # the examination of the caveats alone cannot tell the two conditions apart
    with pytest.raises(InfraError, match=r"privileges differ.*lacks space_schedule_replication") as caught:
        start(launcher, RecordingContext(), "replicate", token, tmp_path)
    assert "space_manage_qos" in str(caught.value)
    assert not (tmp_path / "seen.json").exists()
    tools, _, _ = start(launcher, RecordingContext(), "manage", token, tmp_path)
    assert tools == ["add", "echo", "fail"]


@pytest.mark.parametrize(
    ("direct", "effective", "reason"),
    [
        (MANAGE, MANAGE, "has also space_manage_qos"),
        (["space_view"], ["space_view"], "lacks space_read_data"),
        (BASELINE, [*BASELINE, "space_write_data"], r"effective.*has also space_write_data"),
        ([*BASELINE, "space_write_data"], BASELINE, r"direct.*has also space_write_data"),
    ],
    ids=["gained a right", "lost a right", "effective only", "direct only"],
)
def test_o05_o06_a_change_of_privileges_between_minting_and_launch_does_not_start_the_server(
    direct, effective, reason, world, fake, tmp_path
):
    """O05, O06: the token passed its examination when it was minted; the principal's privileges at launch are read
    again, and any difference from the condition's rights, in either direction, refuses the start."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    fake.grant(TOOLS.user_id, direct, effective)
    with pytest.raises(InfraError, match=reason):
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_o05_o06_the_plane_is_checked_before_the_spawn_and_again_after_the_stop(world, fake, tmp_path):
    """O05, O06: the launch reads the token's examination and then the principal's two privilege lists before the
    server starts, as admin reads, and the same three at teardown, so a run that held reads the plane twice."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    before = len(fake.requests)
    start(launcher, RecordingContext(), "read", token, tmp_path)
    one_check = [
        ("POST", "/api/v3/onezone/tokens/examine"),
        ("GET", f"/api/v3/onezone/spaces/{fake.SPACE_ID}/users/{TOOLS.user_id}/privileges"),
        ("GET", f"/api/v3/onezone/spaces/{fake.SPACE_ID}/effective_users/{TOOLS.user_id}/privileges"),
    ]
    assert [(r.method, r.path) for r in fake.requests[before:]] == one_check * 2
    assert {r.token for r in fake.requests[before:]} == {fake.ADMIN}


def test_o05_o06_a_principal_whose_privileges_cannot_be_read_does_not_start_the_server(world, fake, tmp_path):
    """O05, O06: unknown is not a pass. A principal Onezone has no privileges for is a refusal, not a start."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    del fake.privileges[(fake.SPACE_ID, TOOLS.user_id)]
    with pytest.raises(InfraError, match="privileges could not be read"):
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert not (tmp_path / "seen.json").exists()


def test_a_federation_that_cannot_answer_the_privilege_read_is_infrastructure_of_kind_tool_plane_integrity(
    world, fake, tmp_path, monkeypatch
):
    """An outage of Onezone while the principal's privileges are read at launch is an infrastructure failure, as it is
    while the token is examined, and nothing is spawned."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    reads = fake._admin_route
    monkeypatch.setattr(
        fake,
        "_admin_route",
        lambda method, route, sent: fake._error(503, "x") if method == "GET" else reads(method, route, sent),
    )
    with pytest.raises(InfraError) as caught:
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert caught.value.kind == "tool_plane_integrity"
    assert token.reveal() not in str(caught.value)
    assert not (tmp_path / "seen.json").exists()


def run_with(launcher, ctx, condition, token, between, *, ceiling_s=CEILING_S, body=None):
    """Launch, let `between` happen while the server runs, call a tool once (or run `body`), and leave."""

    async def go():
        async with launcher.launch(ctx, condition, token, ceiling_s=ceiling_s) as provider:
            between()
            if body is not None:
                body()
            await provider.execute("echo", {"text": "hi"}, timeout_s=10, ctx=ctx)

    asyncio.run(go())


@pytest.mark.parametrize(
    ("direct", "effective", "reason"),
    [
        (MANAGE, MANAGE, "has also space_manage_qos"),
        (["space_view"], ["space_view"], "lacks space_read_data"),
        (BASELINE, [*BASELINE, "space_write_data"], r"effective.*has also space_write_data"),
    ],
    ids=["granted", "revoked", "effective only"],
)
def test_o05_o06_a_change_of_privileges_between_launch_and_teardown_sets_the_run_aside(
    direct, effective, reason, world, fake, events
):
    """O05, O06: a grant after the launch-time check cannot be prevented, so it is detected at teardown. The run ends as
    infrastructure of the tool-server kind, a `tool_plane_rights_changed` event is recorded, and the attempt is
    classified as set aside, however the body went."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    ctx = RecordingContext()
    with pytest.raises(InfraError, match="rights changed during the run") as caught:
        run_with(launcher, ctx, "read", token, lambda: fake.grant(TOOLS.user_id, direct, effective))
    assert caught.value.kind == "tool_plane_integrity"
    assert re.search(reason, str(caught.value))
    assert [kind for kind, _ in events] == ["tool_plane_rights_changed"]
    assert events[0][1] == caught.value.detail
    assert events[0][1].startswith("conditions[0]:")
    assert "read" not in events[0][1].replace("space_read_data", "")
    assert token.reveal() not in str(caught.value)
    assert ctx.events[-2:] == [("open", WAIT_KIND), ("close", WAIT_KIND)]  # the teardown check is inside the wait
    outcome = failure_result(caught.value, ended_utc="2026-01-01T00:00:00Z", wall_s=1.0)
    assert outcome.outcome.name == "infrastructure"
    assert outcome.outcome.infra_kind == "tool_plane_integrity"
    assert classify(outcome).state == "infra_fail"


def test_o05_o06_a_plane_that_held_records_no_event_and_raises_nothing(world, events):
    """O05, O06: with no change the teardown check passes silently."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    run_with(launcher, RecordingContext(), "read", token, lambda: None)
    assert events == []


def test_o05_o06_a_change_is_detected_after_a_failure_of_the_body_too(world, fake, events):
    """O05, O06: a run that crashed in its body while the rights changed is set aside as infrastructure, not as a
    defect of its own code; a crash with no change keeps its own exception and records nothing."""
    config, issuer, launcher = world
    token = read_token(issuer, config)

    def crash():
        raise RuntimeError("the run failed")

    with pytest.raises(RuntimeError, match="the run failed"):
        run_with(launcher, RecordingContext(), "read", token, crash)
    assert events == []
    with pytest.raises(InfraError, match="rights changed during the run") as caught:
        run_with(launcher, RecordingContext(), "read", token, lambda: fake.grant(TOOLS.user_id, MANAGE), body=crash)
    assert caught.value.kind == "tool_plane_integrity"
    assert [kind for kind, _ in events] == ["tool_plane_rights_changed"]


def test_o05_o06_the_run_is_set_aside_even_when_the_event_cannot_be_written(world, fake):
    """O05, O06: a sink that fails does not hide the change; the InfraError is raised from the sink's failure."""
    config, issuer, _ = world

    def broken(kind, detail):
        raise OSError("the event log is full")

    launcher = ToolServerLauncher(config, issuer, events=broken)
    with pytest.raises(InfraError, match="rights changed during the run") as caught:
        run_with(
            launcher, RecordingContext(), "read", read_token(issuer, config), lambda: fake.grant(TOOLS.user_id, MANAGE)
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_o05_o06_a_plane_that_cannot_be_re_verified_at_teardown_is_set_aside_without_the_rights_event(
    world, fake, events, monkeypatch
):
    """O05, O06: unknown is not a pass. An outage while the privileges are read at teardown, or a token Onezone no
    longer knows, sets the run aside as infrastructure, but claims no change of rights."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    reads = fake._admin_route
    outage = lambda method, route, sent: fake._error(503, "x") if method == "GET" else reads(method, route, sent)  # noqa: E731
    with pytest.raises(InfraError, match="could not be reached at teardown") as caught:
        run_with(launcher, RecordingContext(), "read", token, lambda: monkeypatch.setattr(fake, "_admin_route", outage))
    assert caught.value.kind == "tool_plane_integrity"
    monkeypatch.setattr(fake, "_admin_route", reads)
    with pytest.raises(InfraError, match="failed its check at teardown") as caught:
        run_with(launcher, RecordingContext(), "read", token, fake.issued.clear)
    assert caught.value.kind == "tool_plane_integrity"
    assert events == []


def test_o06_the_token_at_teardown_only_has_to_be_unexpired(three_conditions, tmp_path):
    """O06: the run is over at teardown, so the token's remaining life is no longer held to the run's minimum (it
    would fail the launch-time window) but an expired token is still a plane that cannot be verified."""
    config, issuer, _ = three_conditions
    now = [NOW]
    ticking = TokenIssuer(issuer._client, config, clock=lambda: now[0])
    token = ticking.mint(tool_server_spec(config.space.id, config.condition("read")), TOOLS, ceiling_s=CEILING_S)
    window = token_window(CEILING_S, config.tokens)
    launcher = ToolServerLauncher(config, ticking, events=lambda kind, detail: None)
    run_with(launcher, RecordingContext(), "read", token, lambda: now.__setitem__(0, token.expires_at - 10))
    assert token.expires_at - (token.expires_at - 10) < window.min_ttl_s  # it would not pass the launch-time window
    now[0] = NOW
    with pytest.raises(InfraError, match="failed its check at teardown") as caught:
        run_with(launcher, RecordingContext(), "read", token, lambda: now.__setitem__(0, token.expires_at + 1))
    assert caught.value.kind == "tool_plane_integrity"


def test_o05_every_failure_of_the_launch_time_check_is_of_the_integrity_kind_and_spawns_nothing(
    world, fake, tmp_path, monkeypatch
):
    """O05: a token that fails its examination, a lifetime the run outlasts, privileges that differ from the condition's
    rights, a privilege read that fails and an outage of Onezone are all `tool_plane_integrity`, never `knowledge`,
    which a study may declare as treatment; and no server is started for any of them."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    reads = fake._admin_route
    attempts = [
        ("no longer examines", lambda: fake.issued.clear()),
        ("shorter", lambda: None),
        ("privileges differ", lambda: fake.grant(TOOLS.user_id, MANAGE)),
        ("privileges could not be read", lambda: fake.privileges.pop((fake.SPACE_ID, TOOLS.user_id))),
        (
            "could not be reached",
            lambda: monkeypatch.setattr(fake, "status_override", 503),
        ),
    ]
    for reason, prepare in attempts:
        prepare()
        ceiling = 7200 if reason == "shorter" else CEILING_S
        with pytest.raises(InfraError) as caught:
            start(launcher, RecordingContext(), "read", token, tmp_path, ceiling_s=ceiling)
        assert caught.value.kind == "tool_plane_integrity", reason
        assert "before the start" in str(caught.value)
        assert token.reveal() not in str(caught.value)
        assert not (tmp_path / "seen.json").exists(), reason
        monkeypatch.setattr(fake, "status_override", None)
        monkeypatch.setattr(fake, "_admin_route", reads)
        fake.grant(TOOLS.user_id, BASELINE)
        token = read_token(issuer, config)


def test_c01_a_rights_change_at_teardown_is_set_aside_and_never_scored_under_a_condition_declaring_knowledge(
    world, fake, events, tmp_path
):
    """C01, O05: a condition whose treatment is the tool server failing (`knowledge`) scores a server failure after the
    agent's first turn. A rights change found at teardown is not that failure: it is `tool_plane_integrity`, so the
    attempt is set aside and charged, whatever turns it completed, and the run is attempted again."""
    from campaign_support import LATER, NOW, keys_under, make_manifest, open_store, treatment_schema

    config, issuer, launcher = world
    with pytest.raises(InfraError) as caught:
        run_with(
            launcher, RecordingContext(), "read", read_token(issuer, config), lambda: fake.grant(TOOLS.user_id, MANAGE)
        )
    assert caught.value.kind == "tool_plane_integrity"
    assert [kind for kind, _ in events] == ["tool_plane_rights_changed"]

    schema = treatment_schema(("knowledge",))
    store = open_store(tmp_path / "campaign", schema)
    manifest = make_manifest(schema)
    store.sync_manifest(manifest.rows)
    text = keys_under(schema, manifest, "api-none")[0]
    key = next(row.key for row in manifest.rows if row.key.text == text)
    kinds = schema.treatment_kinds_of(key)
    assert kinds == ("knowledge",)

    def finish(error, turns):
        begun = store.begin_attempt(key.text, NOW, 1, "")
        result = failure_result(error, ended_utc=LATER, wall_s=1.0, treatment_kinds=kinds)
        status = store.finish_attempt(begun.id, dataclasses.replace(result, model_turns=turns), 3)
        row = store.q("SELECT state, charged, treatment, infra_kind FROM attempts WHERE id=?", (begun.id,))[0]
        return status, (row["state"], row["charged"], row["treatment"], row["infra_kind"])

    assert finish(caught.value, turns=3) == ("pending", ("infra_fail", 1, 0, "tool_plane_integrity"))
    server_down = InfraError("knowledge", "the tool server went down")
    assert finish(server_down, turns=3) == ("done", ("done", 0, 1, "knowledge"))  # the server failing is still scored


OUTSIDE = "sp-outside-0009"  # a space Onezone knows that the configuration does not declare
JOINED = "the token's scope differs from the plane's spaces: has also 1 space the configuration does not declare"
ADMIN = "the principal holds Onezone admin privileges: oz_spaces_add_relationships"


def manage_token(issuer, config, fake):
    """A writing condition's token, minted while its user is a member of the plane's space alone and no admin."""
    fake.grant(TOOLS.user_id, MANAGE)
    return issuer.mint(tool_server_spec(config.space.id, config.condition("manage")), TOOLS, ceiling_s=CEILING_S)


def joins_outside(fake):
    return lambda: fake.add_space(OUTSIDE, "unrelated-space", [ALPHA_ID])


def becomes_admin(fake):
    return lambda: fake.admin_privileges.update({TOOLS.user_id: ["oz_spaces_add_relationships"]})


BOUNDARY_CHANGES = [(joins_outside, JOINED), (becomes_admin, ADMIN)]


@pytest.mark.parametrize(("change", "reason"), BOUNDARY_CHANGES, ids=["joined an outside space", "became an admin"])
def test_o09_a_writing_planes_user_who_joined_a_space_outside_it_or_became_an_admin_starts_no_server(
    change, reason, world, fake, tmp_path
):
    """O09: a writing token is bounded by its user alone, so the launch reads its scope and the user's admin privileges
    again: a user who became a member of a space outside the plane, or an admin of Onezone, after the mint starts
    nothing, and the refusal names no space."""
    config, issuer, launcher = world
    token = manage_token(issuer, config, fake)
    change(fake)()
    with pytest.raises(InfraError, match=re.escape(f"failed its check before the start: {reason}")) as caught:
        start(launcher, RecordingContext(), "manage", token, tmp_path)
    assert caught.value.kind == "tool_plane_integrity"
    assert OUTSIDE not in str(caught.value)
    assert token.reveal() not in str(caught.value)
    assert not (tmp_path / "seen.json").exists()


@pytest.mark.parametrize(("change", "reason"), BOUNDARY_CHANGES, ids=["joined an outside space", "became an admin"])
def test_o09_a_writing_planes_user_who_joined_a_space_outside_it_or_became_an_admin_during_the_run_is_set_aside(
    change, reason, world, fake, events
):
    """O09: the same reads at teardown find a boundary that changed while the server ran; the run is set aside as
    infrastructure of the tool-plane kind. Its declared space rights did not change, so no rights event is recorded."""
    config, issuer, launcher = world
    token = manage_token(issuer, config, fake)
    with pytest.raises(InfraError, match=re.escape(f"failed its check at teardown: {reason}")) as caught:
        run_with(launcher, RecordingContext(), "manage", token, change(fake))
    assert caught.value.kind == "tool_plane_integrity"
    assert OUTSIDE not in str(caught.value)
    assert events == []


def test_o09_a_writing_plane_is_read_for_its_scope_and_admin_privileges_before_the_spawn_and_after_the_stop(
    world, fake, tmp_path
):
    """O09: a writing condition's launch adds two reads to the read-only plane's three: the user's admin privileges, as
    an admin read, and the token's scope, which Onezone answers to the token in the body and no header."""
    config, issuer, launcher = world
    token = manage_token(issuer, config, fake)
    before = len(fake.requests)
    start(launcher, RecordingContext(), "manage", token, tmp_path)
    one_check = [
        ("POST", "/api/v3/onezone/tokens/examine", fake.ADMIN),
        ("GET", f"/api/v3/onezone/spaces/{fake.SPACE_ID}/users/{TOOLS.user_id}/privileges", fake.ADMIN),
        ("GET", f"/api/v3/onezone/spaces/{fake.SPACE_ID}/effective_users/{TOOLS.user_id}/privileges", fake.ADMIN),
        ("GET", f"/api/v3/onezone/users/{TOOLS.user_id}/effective_privileges", fake.ADMIN),
        ("POST", "/api/v3/onezone/tokens/infer_access_token_scope", None),
    ]
    assert [(r.method, r.path, r.token) for r in fake.requests[before:]] == one_check * 2


def admin_read_fails(fake, monkeypatch):
    reads = fake._admin_route

    def failing(method, route, sent):
        return fake._error(503, "x") if route.endswith("/effective_privileges") else reads(method, route, sent)

    return lambda: monkeypatch.setattr(fake, "_admin_route", failing)


@pytest.mark.parametrize(
    ("bend", "words"),
    [
        (lambda fake, monkeypatch: lambda: setattr(fake, "scope_status", 503), "could not be reached"),
        (admin_read_fails, "could not be reached"),
        (lambda fake, monkeypatch: lambda: setattr(fake, "scope_hook", lambda a: {}), "failed its check"),
    ],
    ids=["scope outage", "admin read outage", "scope unreadable"],
)
@pytest.mark.parametrize("when", ["before the start", "at teardown"])
def test_o05_a_writing_planes_scope_or_admin_read_that_fails_is_an_outage_never_a_pass(
    bend, words, when, world, fake, tmp_path, monkeypatch, events
):
    """O05: unknown is not a pass. An outage of Onezone while the scope or the admin privileges are read, at launch or
    at teardown, sets the run aside in the outage's words; an answer that cannot be read fails the check."""
    config, issuer, launcher = world
    token = manage_token(issuer, config, fake)
    if when == "before the start":
        bend(fake, monkeypatch)()
        run = lambda: start(launcher, RecordingContext(), "manage", token, tmp_path)  # noqa: E731
    else:
        run = lambda: run_with(launcher, RecordingContext(), "manage", token, bend(fake, monkeypatch))  # noqa: E731
    with pytest.raises(InfraError, match=re.escape(f"{words} {when}")) as caught:
        run()
    assert caught.value.kind == "tool_plane_integrity"
    assert token.reveal() not in str(caught.value)
    assert events == []


def test_a_federation_that_cannot_examine_the_token_is_infrastructure_of_kind_tool_plane_integrity(
    world, fake, tmp_path
):
    """An outage of Onezone while the token is examined at launch is an infrastructure failure, not a defect."""
    config, issuer, launcher = world
    token = read_token(issuer, config)
    fake.status_override = 503
    with pytest.raises(InfraError) as caught:
        start(launcher, RecordingContext(), "read", token, tmp_path)
    assert caught.value.kind == "tool_plane_integrity"
    assert token.reveal() not in str(caught.value)


def test_a_condition_the_file_does_not_have_is_refused_without_naming_it_or_the_known_ones(world, tmp_path):
    config, issuer, launcher = world
    with pytest.raises(ConfigError, match="condition asked for is not declared") as caught:
        start(launcher, RecordingContext(), "nope", read_token(issuer, config), tmp_path)
    assert "nope" not in str(caught.value)
    assert "manage" not in str(caught.value)


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
    config = load_platform(write_platform(tmp_path, lambda d: d.update(tls={"insecure_tls": True})))
    client = OnezoneClient(config, http=fake.client_http())
    token = TokenIssuer(client, config, clock=lambda: NOW).mint(
        tool_server_spec(config.space.id, config.condition("read")), TOOLS, ceiling_s=CEILING_S
    )
    assert server_environment(config, config.condition("read"), token)["ONEDATA_ALLOW_INSECURE_TLS"] == "true"


@pytest.fixture(scope="module")
def federation(tmp_path_factory):
    """A CA made in the test and a certificate for 127.0.0.1 signed by it, which no default trusts."""
    return make_federation_ca(tmp_path_factory.mktemp("federation"))


@pytest.fixture(scope="module")
def stranger(tmp_path_factory):
    """Another CA, which the federation's certificate does not chain to."""
    return make_federation_ca(tmp_path_factory.mktemp("stranger"), name="unrelated-test-ca")


CA_NAME = "federation-ca.pem"
REFUSED = "refused (ConnectError)"
TLS_MODES = {  # the setting, the CA its bundle holds, and what the server's request to the federation comes to
    "verify-default": (None, "federation", REFUSED),
    "ca_file": ({"ca_file": CA_NAME}, "federation", "200"),
    "insecure_tls": ({"insecure_tls": True}, "federation", "200"),
    "ca_file-of-another-ca": ({"ca_file": CA_NAME}, "stranger", REFUSED),
}


def tls_launcher(tmp_path, fake, frozen_manifests, events, tls, ca):
    """A configuration with `[tls]` (its bundle the CA of `ca`), its issuer, launcher and a read condition's token."""
    use_frozen(tmp_path, frozen_manifests)
    (tmp_path / CA_NAME).write_bytes(ca.ca.read_bytes())
    config = load_platform(write_platform(tmp_path, lambda d: d.update(tls=tls) if tls else None))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    return config, ToolServerLauncher(config, issuer, events=events), read_token(issuer, config)


@pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the federation's test CA")
@pytest.mark.parametrize("label", TLS_MODES)
def test_o05_the_tool_server_is_given_the_tls_setting_as_the_federation_requires(
    tmp_path, fake, frozen_manifests, events, federation, stranger, label
):
    """O05: what the launcher passes makes a client that opens the federation as the real server does (httpx,
    verifying unless the insecure flag says otherwise) fail against a certificate no default trusts, and the same
    with a ca_file of an unrelated CA, and reach it with its own CA as ca_file or with insecure_tls. The bundle is the
    run's own read-only copy of the hashed bytes, removed when the server stops; the caller cannot set either."""
    tls, ca, expected = TLS_MODES[label]
    authority = {"federation": federation, "stranger": stranger}[ca]
    config, launcher, token = tls_launcher(tmp_path, fake, frozen_manifests, events, tls, authority)
    with TlsServer(federation) as server:
        probe = {"FAKE_MCP_TLS_PROBE": f"{server.url}/api/v3/onezone/configuration"}
        _, _, seen = start(launcher, RecordingContext(), "read", token, tmp_path, extra=probe)
    assert seen["tls_probe"] == expected
    copies = {seen[name] for name in CA_BUNDLE_ENV}
    assert len(copies) == 1
    if tls and "ca_file" in tls:
        copy = copies.pop()
        assert copy != str((tmp_path / CA_NAME).resolve())
        assert seen["ssl_cert_file_mode"] == "0o444"
        assert seen["ssl_cert_file_sha256"] == hashlib.sha256(authority.ca.read_bytes()).hexdigest()
        assert not os.path.exists(copy)  # the run's copy is gone with the server
    else:
        assert copies == {None}
    assert seen["ONEDATA_ALLOW_INSECURE_TLS"] == ("true" if label == "insecure_tls" else None)
    for name in (*CA_BUNDLE_ENV, "ONEDATA_ALLOW_INSECURE_TLS"):
        with pytest.raises(ValueError, match=name):
            server_environment(config, config.condition("read"), token, {name: "x"})


@pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the federation's test CA")
def test_o05_the_tool_server_is_given_the_verified_bytes_and_never_a_bundle_swapped_after_the_check(
    tmp_path, fake, frozen_manifests, events, monkeypatch, federation, stranger
):
    """O05: the bundle swapped for another CA's right after its check is not what the server is given: it refuses a
    server of the swapped-in CA and reaches the federation, whose CA was hashed."""
    config, launcher, token = tls_launcher(tmp_path, fake, frozen_manifests, events, {"ca_file": CA_NAME}, federation)
    swap_after_check(monkeypatch, tmp_path / CA_NAME, stranger.ca.read_bytes())
    with TlsServer(stranger) as impostor, TlsServer(federation) as genuine:
        probe = {"FAKE_MCP_TLS_PROBE": f"{impostor.url}/a,{genuine.url}/b"}
        _, _, seen = start(launcher, RecordingContext(), "read", token, tmp_path, extra=probe)
        reached = (impostor.seen, genuine.seen)
    assert (tmp_path / CA_NAME).read_bytes() == stranger.ca.read_bytes()  # swapped once checked
    assert seen["tls_probe"] == f"{REFUSED},200"
    assert reached == ([], [("GET", "/b")])
    assert seen["ssl_cert_file_sha256"] == config.tls.ca_sha256


LIVE_SERVER = os.environ.get("EXPCORE_LIVE_TOOLSERVER")  # the real tool server's command line
LIVE_TOOL = os.environ.get("EXPCORE_LIVE_TOOLSERVER_TOOL")  # one of its tools that reads from the zone or provider


@pytest.mark.skipif(
    not (LIVE_SERVER and LIVE_TOOL and HAS_OPENSSL),
    reason="EXPCORE_LIVE_TOOLSERVER (the real server's command) and EXPCORE_LIVE_TOOLSERVER_TOOL are not set",
)
@pytest.mark.parametrize("label", ["ca_file", "ca_file-of-another-ca"])
def test_o05_the_real_tool_server_trusts_the_ca_file_it_is_given_and_no_other(
    tmp_path, fake, events, federation, stranger, label
):
    """O05: the real server, launched as a run launches it with the zone and its provider at a TLS server whose
    certificate chains to the federation's CA, sends its tool's request there with that CA as ca_file, and sends
    nothing with an unrelated CA's (its handshake refused). Arguments of the tool: EXPCORE_LIVE_TOOLSERVER_ARGS, as
    JSON (default {})."""
    from expcore_onedata.tls import write_private_copy

    command, *args = shlex.split(LIVE_SERVER)
    arguments = json.loads(os.environ.get("EXPCORE_LIVE_TOOLSERVER_ARGS", "{}"))
    authority = {"ca_file": federation, "ca_file-of-another-ca": stranger}[label]
    (tmp_path / CA_NAME).write_bytes(authority.ca.read_bytes())
    with TlsServer(federation) as server:

        def edit(data):
            data["tls"] = {"ca_file": CA_NAME}
            data["zone"]["url"] = data["providers"]["alpha"]["url"] = server.url
            data["toolserver"].update(command=command, args=args)
            data["toolserver"]["conditions"]["read"]["allow"] = [LIVE_TOOL]

        config = load_platform(write_platform(tmp_path, edit))
        client = OnezoneClient(config, http=fake.client_http())
        issuer = TokenIssuer(client, config, clock=lambda: NOW)
        token = read_token(issuer, config)
        with tempfile.TemporaryDirectory() as private:
            ca_copy = write_private_copy(config.tls.bundle(), Path(private))
            env = server_environment(config, config.condition("read"), token, ca_copy=ca_copy)
            params = StdioServerParameters(command=command, args=args, env=env)
            config.condition("read").manifest.write_text(
                FrozenManifest.from_live(asyncio.run(live_manifest(params))).dump(), encoding="utf-8"
            )
        launcher = ToolServerLauncher(config, issuer, events=events)

        async def call():
            async with launcher.launch(RecordingContext(), "read", token, ceiling_s=CEILING_S) as provider:
                await provider.execute(LIVE_TOOL, arguments, timeout_s=60, ctx=RecordingContext())

        asyncio.run(call())
        reached = list(server.seen)
    if label == "ca_file":
        assert reached, "the real server sent nothing to the federation it was given the CA of"
    else:
        assert reached == []


def test_a_pinned_checkout_is_verified_before_the_spawn(world, make_repo, tmp_path, events):
    """The configured checkout must be at the pinned commit with no local change, or nothing is spawned (pin.py)."""
    config, issuer, _ = world
    clone = make_repo("server")
    clone.write("server.py", "print(1)\n")
    commit = clone.commit("pinned")

    def pin(data):
        data["toolserver"].update(clone=str(clone.path), commit=commit)

    pinned = load_platform(write_platform(tmp_path, pin))
    launcher = ToolServerLauncher(pinned, issuer, events=events)
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


def test_c19_a_token_of_another_user_than_the_leased_slots_tool_server_user_starts_nothing(world, tmp_path):
    """C19: the launcher is told the leased slot's tool-server user; a token minted for any other user is refused
    before any check, wait or spawn, and the slot's own token starts the server."""
    from expcore_onedata.credentials import Principal

    config, issuer, launcher = world
    token = read_token(issuer, config)  # minted by the tool-server user TOOLS
    ctx = RecordingContext()

    async def go(principal):
        async with launcher.launch(ctx, "read", token, ceiling_s=CEILING_S, principal=principal) as provider:
            return sorted(t.name for t in provider.tools())

    with pytest.raises(ValueError, match="leased slot"):
        asyncio.run(go(Principal("slot-x-tools", "u-slot-x-tools")))
    assert ctx.events == []
    assert asyncio.run(go(TOOLS)) == ["add", "echo"]
