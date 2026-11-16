"""O06 on the Apptainer backend: the whole-time ceiling is enforced in the platform's own launch, which both
backends share, so a run on a batch node mints, records and serves its tokens for the ceiling it was given, as a
Docker run does."""

import asyncio
import contextlib
import dataclasses
import json
from types import SimpleNamespace

import pytest
from apptainer_support import make_settings
from apptainer_support import options as node_options
from fake_node import FakeNode
from sandbox_support import add_sandbox

from expcore.campaign.lease import LeasePool
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenRefused
from expcore_onedata.users import GROUP, JOURNAL, TokenJournal

CEILING_S = 450.0  # a study's own, not the default's 3 times a cap: the key an Apptainer run has to follow too


@pytest.fixture
def config(tmp_path, fake) -> PlatformConfig:
    """The platform document of this directory, its `[sandbox]` replaced by the Apptainer backend's settings: the slots
    and tool-server conditions are the document's, the sandbox is a node's."""
    loaded = load_platform(write_platform(tmp_path, add_sandbox))
    (tmp_path / "node").mkdir()
    return dataclasses.replace(loaded, sandbox=make_settings(tmp_path / "node"))


class Context:
    """A call context with no waits of its own."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


class Launcher:
    """Stands for the tool-server launcher: records the ceiling each start was given."""

    def __init__(self):
        self.started = []

    @contextlib.asynccontextmanager
    async def launch(self, ctx, condition, token, *, ceiling_s, extra_env=None, principal=None):
        self.started.append(SimpleNamespace(condition=condition, ceiling_s=ceiling_s))
        yield "the run's tools"


@pytest.fixture
def stage(config, issuer, slots, tmp_path):
    node, journal, launcher = FakeNode(), TokenJournal(tmp_path / "leases"), Launcher()
    platform = SandboxPlatform(
        config,
        issuer,
        node,
        slots=slots,
        journal=journal,
        events=lambda kind, detail: None,
        tool_condition=lambda factors: factors.get("tools"),
        tool_server=launcher,
        clock=lambda: NOW,
        registry=Registry(),
        sandbox_options={k: v for k, v in node_options().items() if k != "registry"},
    )
    pool = LeasePool(
        tmp_path / "leases",
        platform.lease_keys,
        platform.probe(),
        clock=lambda: 0.0,
        sleep=lambda s: None,
        quarantine_retry_s=0.0,
    )
    return SimpleNamespace(
        platform=platform, node=node, journal=journal, launcher=launcher, pool=pool, leases=tmp_path / "leases"
    )


def launched(stage, lease, body, *, condition, ceiling_s=CEILING_S):
    async def go():
        async with stage.platform.launch(
            Context(), run_key="run-1", level="via-gate", ceiling_s=ceiling_s, lease=lease, condition=condition
        ) as sandbox:
            await body(sandbox)
            return sandbox

    return asyncio.run(go())


def test_o06_an_apptainer_run_records_the_ceiling_its_tokens_were_minted_for(stage):
    """O06: the platform builds the node's sandbox, and its metadata and the lease journal say how long each token
    was minted for and the ceiling that lifetime follows, as a Docker run's do."""
    lease = stage.pool.acquire(stage.platform.lease_group({"data_path": "via-gate", "tools": "read"}), run_key="run-1")
    seen = {}

    async def body(sandbox):
        seen["metadata"] = sandbox.metadata()

    sandbox = launched(stage, lease, body, condition="read")
    assert isinstance(sandbox, ApptainerSandbox)
    tokens = stage.platform._config.tokens
    ttl = int(CEILING_S + tokens.margin_s + tokens.mint_headroom_s)
    for plane in ("data_path_token", "tool_server_token"):
        recorded = seen["metadata"][plane]
        assert (recorded["ttl_s"], recorded["ceiling_s"]) == (ttl, CEILING_S)
        assert recorded["expires_at"] >= NOW + CEILING_S + tokens.margin_s
    lines = [json.loads(line) for line in (stage.leases / JOURNAL).read_text().splitlines()]
    assert [(line["ttl_s"], line["ceiling_s"]) for line in lines[:2]] == [(ttl, CEILING_S)] * 2


def test_o06_an_apptainer_run_with_no_ceiling_to_name_gets_the_tool_server_the_ceiling_it_minted_for(stage):
    """O06: `tool_server` of an Apptainer run takes the ceiling its launch minted for, and refuses another before
    anything starts: a longer one would refuse a correct token, a shorter one would pass a token the run could
    outlive."""
    lease = stage.pool.acquire(stage.platform.lease_group({"data_path": "via-gate", "tools": "read"}), run_key="run-1")
    refused = []

    async def body(sandbox):
        async with stage.platform.tool_server(Context(), sandbox):
            pass
        for other in (CEILING_S * 3, CEILING_S / 2):
            with pytest.raises(ValueError, match="minted for a whole-time ceiling of 450 s") as caught:
                async with stage.platform.tool_server(Context(), sandbox, ceiling_s=other):
                    pass
            refused.append(caught.value)
        async with stage.platform.tool_server(Context(), sandbox, ceiling_s=CEILING_S):
            pass

    launched(stage, lease, body, condition="read")
    assert [started.ceiling_s for started in stage.launcher.started] == [CEILING_S, CEILING_S]
    assert len(refused) == 2


def test_o06_a_ceiling_the_mint_refuses_is_refused_on_the_apptainer_backend_before_a_sandbox_exists(stage):
    """O06: a ceiling that is not finite is refused by the mint before any sandbox is built, on a node as on Docker,
    so no tool plane is kept and no process but the driver's is left."""
    lease = stage.pool.acquire(GROUP, run_key="run-1")

    async def body(sandbox):
        raise AssertionError("the run's body never starts")

    with pytest.raises(TokenRefused, match="ceiling"):
        launched(stage, lease, body, condition=None, ceiling_s=float("inf"))
    assert not list(stage.platform._planes)
    assert list(stage.node.procs) == [999]
