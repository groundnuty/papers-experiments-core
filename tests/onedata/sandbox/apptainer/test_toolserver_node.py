"""O56: the tool server of a run on an HPC node, started by the real launcher through the platform.

It is a process of the driver's on the node, as on Docker it is one on the host: outside every cell, in the node's PID
namespace no cell can address. Its environment is named whole (the MCP client's inherited variables unset through `env
-u`, so no value is ever an argument): the launcher's settings, `HOME` in a 0700 directory of the run's own, which is
also where it starts and which is removed once it has stopped, and the run's tag, by which the reaper of a dead driver
and the lease's probe find it, as they find the gate and the collector.
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import shutil
import stat
import sys
from types import SimpleNamespace

import pytest
from apptainer_support import make_settings
from apptainer_support import options as node_options
from fake_node import OWN_NS, FakeNode
from mcp import StdioServerParameters
from sandbox_support import add_sandbox

from expcore.campaign.lease import LeasePool
from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata import toolserver
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.apptainer import runs
from expcore_onedata.sandbox.apptainer.argv import RUN_TAG
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.testing import NOW, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.toolserver import ENV_TOOLS, MANAGED_ENV, ToolServerLauncher
from expcore_onedata.users import TokenJournal

CEILING_S = 450.0
RECORD = "FAKE_MCP_RECORD_ENVIRON"
DRIVER_ONLY = {"USER": "invented-user", "LOGNAME": "invented-user", "SHELL": "/bin/invented", "TERM": "invented"}


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    """The conditions' frozen manifests, made once for the module: freezing starts the stand-in server."""
    directory = tmp_path_factory.mktemp("manifests")
    for name, tools in (("read", "echo,add"), ("manage", "echo,add,fail")):
        params = StdioServerParameters(command=sys.executable, args=TOOL_SERVER_ARGS, env={ENV_TOOLS: tools})
        manifest = FrozenManifest.from_live(asyncio.run(live_manifest(params)))
        (directory / f"{name}.manifest.json").write_text(manifest.dump(), encoding="utf-8")
    return directory


@pytest.fixture
def config(tmp_path, fake, frozen):
    """The platform document of this directory with real frozen manifests, its `[sandbox]` the node's."""
    for name in ("read.manifest.json", "manage.manifest.json"):
        shutil.copy(frozen / name, tmp_path / name)
    loaded = load_platform(write_platform(tmp_path, add_sandbox))
    (tmp_path / "node").mkdir()
    return dataclasses.replace(loaded, sandbox=make_settings(tmp_path / "node"))


class Waits:
    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


class Spawns:
    """Records what the launcher hands the MCP client: the command, its arguments and the directory it starts in."""

    def __init__(self, monkeypatch):
        self.seen = []
        real = toolserver.McpToolProvider

        def recording(command, args=(), **kw):
            self.seen.append(SimpleNamespace(command=command, args=list(args), env=dict(kw.get("env") or {}),
                                             cwd=kw.get("cwd")))  # fmt: skip
            return real(command, args, **kw)

        monkeypatch.setattr(toolserver, "McpToolProvider", recording)


@pytest.fixture
def stage(config, issuer, slots, tmp_path, monkeypatch):
    for name, value in DRIVER_ONLY.items():  # what a driver's environment may hold, which the server must not inherit
        monkeypatch.setenv(name, value)
    node, events = FakeNode(), []
    launcher = ToolServerLauncher(config, issuer, events=lambda kind, detail: events.append(kind))
    platform = SandboxPlatform(
        config, issuer, node, slots=slots, journal=TokenJournal(tmp_path / "leases"),
        events=lambda kind, detail: None, tool_condition=lambda factors: factors.get("tools"), tool_server=launcher,
        clock=lambda: NOW, registry=Registry(),
        sandbox_options={k: v for k, v in node_options().items() if k != "registry"},
    )  # fmt: skip
    pool = LeasePool(tmp_path / "leases", platform.lease_keys, platform.probe(), clock=lambda: 0.0,
                     sleep=lambda s: None, quarantine_retry_s=0.0)  # fmt: skip
    return SimpleNamespace(platform=platform, node=node, pool=pool, spawns=Spawns(monkeypatch), tmp=tmp_path)


def run(stage, body):
    """Launch a REST run with the `read` tool server's condition and run `body(sandbox)` inside it."""
    lease = stage.pool.acquire(stage.platform.lease_group({"data_path": "via-gate", "tools": "read"}), run_key="r-1")

    async def go():
        async with stage.platform.launch(
            Waits(), run_key="run-1", level="via-gate", ceiling_s=CEILING_S, lease=lease, condition="read"
        ) as sandbox:
            return await body(sandbox)

    return asyncio.run(go())


def serve(stage, record=None):
    """A body that starts the run's tool server, calls a tool, and notes what the sandbox held while it ran."""

    async def body(sandbox):
        extra = {RECORD: str(record)} if record is not None else None
        async with stage.platform.tool_server(Waits(), sandbox, extra_env=extra) as tools:
            echoed = await tools.execute("echo", {"text": "hi"}, timeout_s=30, ctx=Waits())
            home = sandbox.dir / "toolserver"
            during = {"home": home, "mode": stat.S_IMODE(os.stat(home).st_mode), "echo": json.loads(echoed.text)}
        return sandbox, during

    return body


def test_o56_the_tool_server_runs_on_the_node_in_an_environment_named_whole_with_a_home_of_the_runs_own(stage):
    """O56: nothing of the driver's environment reaches the server but `PATH` (the managed settings' own): no `USER`,
    `LOGNAME`, `SHELL` or `TERM`, though the driver has them; its `HOME` is a 0700 directory of the run's, where it also
    starts; the run's tag names its sandbox. Its tools answer, as on Docker."""
    record = stage.tmp / "environ.json"
    sandbox, during = run(stage, serve(stage, record))
    seen = json.loads(record.read_text(encoding="utf-8"))
    environ = seen["environ"]
    assert during["echo"] == {"result": "hi"}
    assert not set(DRIVER_ONLY) & set(environ)
    expected = {"HOME", "TMPDIR", "PATH", RUN_TAG, "FASTMCP_LOG_LEVEL", RECORD}
    # Python sets LC_CTYPE itself when it starts with no locale at all (PEP 538's coercion): the server's own, never
    # the driver's, which never sets one of these values
    assert environ.pop("LC_CTYPE", "C.UTF-8") in ("C.UTF-8", "C.utf8", "UTF-8")
    assert set(environ) - set(MANAGED_ENV) == expected
    assert environ["HOME"] == seen["cwd"] == str(during["home"])
    assert during["mode"] == 0o700
    assert environ[RUN_TAG] == sandbox.sid
    assert environ["PATH"] == os.environ["PATH"]


def test_o56_the_tool_servers_home_is_gone_once_it_has_stopped_and_the_sandbox_records_how_it_ran(stage):
    """O56: the home is removed when the server stops, while the sandbox still runs; the sandbox's metadata says where
    the server ran and what it inherited, by name and never a value."""

    async def body(sandbox):
        _, during = await serve(stage)(sandbox)
        return os.path.lexists(during["home"]), sandbox.metadata()

    left, meta = run(stage, body)
    assert left is False
    assert meta["tool_server_process"] == {"on": "node", "home": "run_directory", "tmpdir": "home", "run_tag": True,
                                           "inherited": ["PATH"]}  # fmt: skip


def test_o56_no_word_of_the_tool_servers_command_holds_its_token_and_the_wrapper_unsets_what_the_client_inherits(
    stage,
):
    """O56: the server is `env -u <each variable the MCP client inherits> -- <command> <args>`: the token reaches it
    through its environment alone, as on Docker, and `env -i`, which would take every value as an argument, is never
    used."""
    run(stage, serve(stage))
    [spawn] = stage.spawns.seen
    token = spawn.env[toolserver.ENV_ZONE_TOKEN]
    assert token
    assert not any(token in word for word in [spawn.command, *spawn.args])
    assert spawn.command == "env"
    assert "-i" not in spawn.args
    unset = [spawn.args[i + 1] for i, word in enumerate(spawn.args[: spawn.args.index("--")]) if word == "-u"]
    assert set(unset) == set(toolserver.inherited_by_the_client()) - {"HOME", "PATH"}
    assert spawn.args[spawn.args.index("--") + 1 :] == [sys.executable, *TOOL_SERVER_ARGS]
    assert str(spawn.cwd) == spawn.env["HOME"]


def test_o56_a_tool_server_left_running_holds_the_lease_and_a_dead_drivers_reaper_kills_it_by_the_runs_tag(
    stage,
):
    """O56: a server that outlives its launch, the driver killed outright say, carries the run's tag: the lease's probe
    holds the key while it lives, and the next start's reaper kills it with the run's other processes."""
    node = stage.node

    async def body(sandbox):
        return sandbox

    sandbox = run(stage, body)
    key = sandbox.spec.lease_key
    tag = f"{RUN_TAG}={sandbox.sid}"
    left = node._new_proc(OWN_NS, "onedata-mcp", environ=(tag,), role="toolserver")  # as the launch would leave it
    probe = stage.platform.sandbox_probe()
    assert probe.resource_gone(key, {}) is False
    runs.write(stage.platform._s.work_root, {"sid": sandbox.sid, "lease": key, "owner": {"pid": 1, "start": "0"},
                                             "helpers": [], "token_files": []})  # fmt: skip
    reaped = runs.reap(node, stage.platform._s.work_root, own="another")
    assert reaped["processes"] == 1
    assert left.alive is False
    assert probe.resource_gone(key, {}) is True


def test_o56_the_backends_settings_cannot_be_set_by_the_studys_extra_environment(stage):
    """O56: the home and the run's tag are the backend's, as the allow-list and the token are the condition's."""

    async def body(sandbox):
        with pytest.raises(ValueError, match="HOME"):
            async with stage.platform.tool_server(Waits(), sandbox, extra_env={"HOME": "/elsewhere"}):
                pass
        return stage.spawns.seen

    assert run(stage, body) == []


def test_o56_a_run_kept_as_evidence_while_its_tool_server_runs_keeps_no_file_of_the_servers_home(tmp_path):
    """O56, O05: a teardown that keeps the run's directory as evidence (a cell's deadline ended the run, say) while the
    tool server's home is still there removes that home first: what the server wrote there, its token among it maybe,
    is never evidence, and the server's own end then finds nothing left to remove."""
    from apptainer_support import make_spec, start

    from expcore_onedata.sandbox.apptainer.teardown import KEPT

    settings = make_settings(tmp_path)
    box = start(FakeNode(), settings, make_spec("via-gate"))
    with box.tool_server_process() as process:
        (process.home / "cache.json").write_text('{"token": "the tool-server token"}', encoding="utf-8")
        assert process.env == {RUN_TAG: box.sid, "TMPDIR": str(process.home / "tmp")}
        box.keep_evidence = True
        box.stop()
        kept = settings.work_root / KEPT / box.sid
        assert kept.is_dir()
        assert not (kept / "toolserver").exists()
        (process.home / ".cache").mkdir(parents=True)  # the server, still running, makes its home again
    assert not (kept / "toolserver").exists()
    assert not os.path.lexists(box.dir)  # nothing a probe would take for the run's directory is left
    assert stage_probe_gone(settings, box)


def stage_probe_gone(settings, box) -> bool:
    from expcore_onedata.sandbox.apptainer.runs import ApptainerProbe

    return ApptainerProbe(box.node, settings.work_root).resource_gone(box.spec.lease_key or "run-key-1", {})


def test_o56_the_tool_servers_home_holds_its_temporary_files_too(stage):
    """O56: the server's `TMPDIR` is a directory of its home, so its temporary files stay in the run's private
    directory and go with it, never on the node's shared `/tmp`."""
    record = stage.tmp / "environ.json"
    run(stage, serve(stage, record))
    environ = json.loads(record.read_text(encoding="utf-8"))["environ"]
    assert environ["TMPDIR"] == os.path.join(environ["HOME"], "tmp")
