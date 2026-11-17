"""O84: the tool server is told to read no `.env` file, and starts in an empty directory of the launch's own.

onedata-mcp read a `.env` when its config module was imported. python-dotenv's default search starts at the directory
of the file that calls it, the installed package, and walks up to `/`, so a `.env` in any ancestor of the package (on
a cluster account, its shared home) could set any `ONEDATA_*` setting the launch left unset, with nothing in the launch
saying so. The server's environment now says `ONEDATA_MCP_DOTENV=0` on both platforms (the server's own guard, which
stops that read), and the server starts in a directory that is new, empty and mode 0700 (FastMCP reads a `.env` from
the working directory for its own `FASTMCP_*` settings, and a launch that inherits the driver's reads the driver's).

The python-dotenv library is no dependency of the core, so the end-to-end test imports a stand-in config module whose
`load_dotenv` follows python-dotenv 1.2.2's default search (the directory of the file that calls it, up to `/`, the
first `.env`, nothing overridden) behind the guard onedata-mcp has; the library itself is exercised in onedata-mcp's own
tests. Names are invented.

`python -m expcore_onedata.toolmanifest freeze` and `check` start the server exactly as the launch does (the same
function builds its command, its environment and its private directory), so a manifest is frozen from what a run's
server sees; their end-to-end tests below use the same stand-in and the same planted `.env`.
"""

import asyncio
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from expcore.tools.manifest import FrozenManifest, live_manifest
from expcore_onedata import toolmanifest
from expcore_onedata.addresses import FORMAT
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.testing import CEILING_S, NOW, TOOL_SERVER_ARGS, write_platform
from expcore_onedata.testing import TOOL_SERVER_USER as TOOLS
from expcore_onedata.testing.fake_onedata_mcp import SETTINGS
from expcore_onedata.tokens import TokenIssuer, tool_server_spec
from expcore_onedata.toolserver import (
    ENV_TOOLS,
    MANAGED_ENV,
    OVERRIDDEN_ENV,
    ToolServerLauncher,
    server_argv,
    server_environment,
    server_start,
)

DOTENV = "ONEDATA_MCP_DOTENV"
NODE_NAMES = "ONEDATA_MCP_NODE_NAMES"
NAMES = '{"node-1.layout.test:8443": "provider-a"}'
LAUNCHER = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "toolshim" / "resolve_launch.py"
PLATFORMS = ("compose", "hpc")  # a plain platform, and one with `[addresses]` whose server starts through the launcher

STANDIN_CONFIG = '''\
"""A stand-in for onedata_mcp/config.py: its import reads a .env, unless the process environment says 0."""

import os
import sys
from pathlib import Path


def load_dotenv():
    """python-dotenv 1.2.2's default search: from the directory of the file that calls it up to /, the first .env."""
    caller = Path(sys._getframe(1).f_code.co_filename).resolve()
    for directory in (caller.parent, *caller.parent.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                name, found, value = line.partition("=")
                if found and name.strip() and not name.lstrip().startswith("#"):
                    os.environ.setdefault(name.strip(), value.strip())  # override=False
            return True
    return False


if os.environ.get("ONEDATA_MCP_DOTENV", "").strip() != "0":
    load_dotenv()
'''
PROBE = """\
import json
import os

import standin_config  # noqa: F401

print(json.dumps(os.environ.get("ONEDATA_MCP_NODE_NAMES")))
"""
WRAPPER = """\
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # a server started with -I has no script directory
import standin_config  # noqa: E402, F401  (its import is where a .env is read)

runpy.run_module("expcore_onedata.testing.fake_onedata_mcp", run_name="__main__")
"""


class Context:
    """The call context a launch needs: it opens and closes its waits and records nothing."""

    @contextlib.asynccontextmanager
    async def infrastructure_wait(self, kind):
        yield


@pytest.fixture
def events() -> list:
    """The (kind, detail) pairs the launcher's event sink is called with."""
    return []


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    """Each condition's frozen manifest of the stand-in server, made once for the module (a start takes seconds)."""
    directory = tmp_path_factory.mktemp("manifests")
    for condition, tools in (("read", "echo,add"), ("manage", "echo,add,fail")):
        params = StdioServerParameters(command=sys.executable, args=TOOL_SERVER_ARGS, env={ENV_TOOLS: tools})
        manifest = FrozenManifest.from_live(asyncio.run(live_manifest(params)))
        (directory / f"{condition}.manifest.json").write_text(manifest.dump(), encoding="utf-8")
    return directory


def platform(tmp_path: Path, kind: str, args=TOOL_SERVER_ARGS):
    """A loaded platform of `kind` whose tool server is `python args`."""

    def edit(data):
        data["toolserver"].update(command=sys.executable, args=list(args))
        if kind == "hpc":
            data["addresses"] = {"file": "map.json", "resolver": str(LAUNCHER)}

    if kind == "hpc":
        document = {"format": FORMAT, "hosts": {"zone.test": {"address": "127.0.0.1", "nodes": []}}}
        (tmp_path / "map.json").write_text(json.dumps(document), encoding="utf-8")
        (tmp_path / "map.json").chmod(0o600)
    return load_platform(write_platform(tmp_path, edit))


def launching(tmp_path: Path, fake, frozen: Path, events: list, kind: str, args=TOOL_SERVER_ARGS):
    """The platform of `kind` with its issuer, launcher and a read condition's token."""
    for name in ("read.manifest.json", "manage.manifest.json"):
        shutil.copy(frozen / name, tmp_path / name)
    config = platform(tmp_path, kind, args)
    issuer = TokenIssuer(OnezoneClient(config, http=fake.client_http()), config, clock=lambda: NOW)
    token = issuer.mint(tool_server_spec(config.space.id, config.condition("read")), TOOLS, ceiling_s=CEILING_S)
    return config, ToolServerLauncher(config, issuer, events=lambda name, detail: events.append((name, detail))), token


def start(launcher: ToolServerLauncher, token, tmp_path: Path) -> dict:
    """Launch the server, call it once, leave; return what the server recorded about its start."""
    seen = tmp_path / "seen.json"

    async def go():
        ctx = Context()
        extra = {"FAKE_MCP_RECORD": str(seen)}
        async with launcher.launch(ctx, "read", token, ceiling_s=CEILING_S, extra_env=extra) as provider:
            await provider.execute("echo", {"text": "hi"}, timeout_s=10, ctx=ctx)

    asyncio.run(go())
    return json.loads(seen.read_text(encoding="utf-8"))


def plant(tmp_path: Path) -> Path:
    """A `.env` setting the node names in an ancestor directory of a server's code; return that code's directory."""
    code = tmp_path / "anc" / "srv"
    code.mkdir(parents=True)
    (tmp_path / "anc" / ".env").write_text(f"{NODE_NAMES}={NAMES}\n", encoding="utf-8")
    for name, text in (("standin_config.py", STANDIN_CONFIG), ("probe.py", PROBE), ("wrapper.py", WRAPPER)):
        (code / name).write_text(text, encoding="utf-8")
    return code


def import_standin(code: Path, word: str | None) -> str | None:
    """What a plain child that imports the stand-in config finds in ONEDATA_MCP_NODE_NAMES, started from an empty
    directory with nothing of ours in its environment: the control, which shows the `.env` is there to be found."""
    elsewhere = code.parents[1] / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), **({DOTENV: word} if word is not None else {})}
    done = subprocess.run(
        [sys.executable, str(code / "probe.py")], env=env, cwd=elsewhere, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def assert_private_empty_directory(seen: dict) -> None:
    """The server was started in an empty directory of mode 0700 owned by the driver's user, not the driver's own."""
    assert seen["cwd_entries"] == []
    assert seen["cwd_mode"] == oct(stat.S_IRWXU)
    assert seen["cwd_uid"] == os.getuid()
    assert os.path.realpath(seen["cwd"]) != os.path.realpath(os.getcwd())


@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_the_servers_environment_says_no_dotenv_on_both_platforms(tmp_path, fake, frozen, events, kind):
    """O84: the environment the platform builds for the server holds ONEDATA_MCP_DOTENV=0 on a Compose platform and on
    an HPC platform whose server starts through the resolve launcher; the word is no part of the launcher's argv."""
    config, _, token = launching(tmp_path, fake, frozen, events, kind)
    env = server_environment(config, config.condition("read"), token)
    assert env[DOTENV] == "0"
    assert NODE_NAMES not in env
    command, args = server_argv(config)
    assert (kind == "hpc") == (str(LAUNCHER) in args)
    assert not [a for a in (command, *args) if DOTENV in a]


@pytest.mark.parametrize("word", ["0", "1", ""])
def test_o84_a_caller_cannot_set_or_clear_the_word(tmp_path, fake, frozen, events, word):
    """O84: ONEDATA_MCP_DOTENV is one of the server's own settings, fixed by the launch, whatever a caller gives."""
    config, _, token = launching(tmp_path, fake, frozen, events, "compose")
    with pytest.raises(ValueError, match=DOTENV):
        server_environment(config, config.condition("read"), token, {DOTENV: word})


def test_o84_the_server_starts_in_a_new_empty_private_directory_each_time_and_it_is_gone_after(
    tmp_path, fake, frozen, events
):
    """O84: each start gets a directory of its own, empty and mode 0700, which is not the driver's working directory and
    is removed once the server has stopped; a second start gets another."""
    _, launcher, token = launching(tmp_path, fake, frozen, events, "compose")
    first = start(launcher, token, tmp_path)
    second = start(launcher, token, tmp_path)
    for seen in (first, second):
        assert_private_empty_directory(seen)
        assert not os.path.exists(seen["cwd"])
    assert first["cwd"] != second["cwd"]


@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_a_real_server_process_started_by_the_launch_reads_no_dotenv_file_above_its_code(
    tmp_path, fake, frozen, events, kind
):
    """O84: a real child process started by the launch, on a Compose platform and through the resolve launcher on an HPC
    one, imports a stand-in config that reads a `.env` unless the environment says 0, with a `.env` in an ancestor
    directory of its code that sets ONEDATA_MCP_NODE_NAMES: it finds the word, loads nothing, and runs in an empty
    private directory. The same import without the word finds the `.env` (the control)."""
    code = plant(tmp_path)
    assert import_standin(code, None) == NAMES  # the control: unguarded, the `.env` above the code is read
    assert import_standin(code, "1") == NAMES
    assert import_standin(code, "0") is None
    _, launcher, token = launching(tmp_path, fake, frozen, events, kind, [str(code / "wrapper.py"), "serve"])
    seen = start(launcher, token, tmp_path)
    assert seen[DOTENV] == "0"
    assert seen[NODE_NAMES] is None
    assert_private_empty_directory(seen)


# -- the manifest tool: `python -m expcore_onedata.toolmanifest freeze|check PLATFORM CONDITION` --------------------

RECORD = "FAKE_MCP_RECORD"
TOKEN_VARIABLE = "MANIFEST_TEST_TOKEN"
FROZEN_ACTIONS = ("freeze", "check")


def manifest_tool(config, action: str, *argv: str, condition: str = "read") -> int:
    """Run the tool's command line on a loaded platform's file, as a study would."""
    return toolmanifest.main([action, str(config.file), condition, *argv])


def recorded(tmp_path: Path, monkeypatch, name: str = "tool-seen.json") -> Path:
    """Have the stand-in server write what it saw to a file, which the tool passes it by name; return that file."""
    seen = tmp_path / name
    monkeypatch.setenv(RECORD, str(seen))
    return seen


@pytest.mark.parametrize("action", FROZEN_ACTIONS)
@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_freeze_and_check_start_the_server_with_the_environment_and_directory_the_launch_gives_it(
    tmp_path, fake, frozen, events, monkeypatch, kind, action
):
    """O84: the server a manifest is frozen or checked from receives, setting by setting, what a launch of the same
    condition gives it (the allow-list, the hosts, the token, the word), on a Compose platform and on an HPC platform
    whose server starts through the resolve launcher, and starts in a new empty directory of mode 0700 that is gone
    once it has stopped; the old tool gave it only what `--pass-env` named, in the driver's working directory."""
    code = plant(tmp_path)
    config, launcher, token = launching(tmp_path, fake, frozen, events, kind, [str(code / "wrapper.py"), "serve"])
    launched = start(launcher, token, tmp_path)
    seen_file = recorded(tmp_path, monkeypatch)
    monkeypatch.setenv(TOKEN_VARIABLE, token.reveal())
    argv = ["--pass-env", RECORD, "--token-env", TOKEN_VARIABLE]
    assert manifest_tool(config, action, *argv) == 0
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    for name in SETTINGS:
        assert seen[name] == launched[name], name
    assert seen[ENV_TOOLS] == "echo,add"
    assert seen[DOTENV] == "0"
    assert_private_empty_directory(seen)
    assert not os.path.exists(seen["cwd"])


@pytest.mark.parametrize("action", FROZEN_ACTIONS)
@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_freeze_and_check_of_a_real_server_read_no_dotenv_file_above_its_code(
    tmp_path, fake, frozen, events, monkeypatch, kind, action
):
    """O84: a real child started by the tool, with the stand-in config behind the same guard and a `.env` in an
    ancestor directory of its code that sets ONEDATA_MCP_NODE_NAMES, loads nothing: the manifest is frozen, and
    checked, from the server a run would have, not from the one the file would have configured."""
    code = plant(tmp_path)
    assert import_standin(code, None) == NAMES  # the control: unguarded, the `.env` above the code is read
    config, _, _ = launching(tmp_path, fake, frozen, events, kind, [str(code / "wrapper.py"), "serve"])
    seen_file = recorded(tmp_path, monkeypatch)
    assert manifest_tool(config, action, "--pass-env", RECORD) == 0
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen[DOTENV] == "0"
    assert seen[NODE_NAMES] is None
    assert_private_empty_directory(seen)


def test_o84_freeze_writes_the_tools_the_condition_allows_to_the_manifest_it_declares(tmp_path, fake, frozen, events):
    """O84: the tools frozen are the ones the server shows under the condition's allow-list, which the launch sets and
    the old tool left to the caller, and they go to the file the condition names, which need not exist yet."""
    config, _, _ = launching(tmp_path, fake, frozen, events, "compose")
    declared = config.condition("manage").manifest
    declared.unlink()
    assert manifest_tool(config, "freeze", condition="manage") == 0
    assert [t["name"] for t in FrozenManifest.load(declared).tools] == sorted(config.condition("manage").allow)
    assert manifest_tool(config, "check", condition="manage") == 0
    shutil.copy(frozen / "read.manifest.json", declared)  # the read condition's tools are not the manage condition's
    assert manifest_tool(config, "check", condition="manage") == 1


@pytest.mark.parametrize("name", [*MANAGED_ENV, *OVERRIDDEN_ENV])
def test_o84_a_setting_the_launch_fixes_cannot_be_passed_to_the_manifest_tool(
    tmp_path, fake, frozen, events, monkeypatch, capsys, name
):
    """O84: every variable the launch sets itself (the managed settings, the word among them, and the two it always
    overrides) is refused by `--pass-env` with a message that names it, whatever the driver's environment holds, set
    or not; the server is never started and nothing is written."""
    config, _, _ = launching(tmp_path, fake, frozen, events, "compose")
    seen_file = recorded(tmp_path, monkeypatch)
    monkeypatch.setenv(name, os.environ.get(name, "1"))
    before = config.condition("read").manifest.read_bytes()
    for action in FROZEN_ACTIONS:
        assert manifest_tool(config, action, "--pass-env", RECORD, "--pass-env", name) == 2
        error = capsys.readouterr().err
        assert name in error
        assert "the launch sets" in error
    assert not seen_file.exists()
    assert config.condition("read").manifest.read_bytes() == before


def test_o84_the_word_is_refused_by_the_manifest_tool_when_the_driver_does_not_hold_it_either(
    tmp_path, fake, frozen, events, monkeypatch, capsys
):
    """O84: the refusal does not depend on the variable being set: naming the word is refused as a fixed setting, not
    reported as missing."""
    config, _, _ = launching(tmp_path, fake, frozen, events, "compose")
    monkeypatch.delenv(DOTENV, raising=False)
    assert manifest_tool(config, "freeze", "--pass-env", DOTENV) == 2
    assert "the launch sets" in capsys.readouterr().err


def test_o84_a_variable_that_is_not_set_cannot_be_passed_to_the_manifest_tool(
    tmp_path, fake, frozen, events, monkeypatch, capsys
):
    """O84: a name given to `--pass-env` or `--token-env` that the driver's environment lacks is an error naming it,
    not a quiet omission: a freeze with a variable missing is not the launch's environment."""
    config, _, _ = launching(tmp_path, fake, frozen, events, "compose")
    monkeypatch.delenv("MANIFEST_TEST_MISSING", raising=False)
    for option in ("--pass-env", "--token-env"):
        assert manifest_tool(config, "freeze", option, "MANIFEST_TEST_MISSING") == 2
        assert "MANIFEST_TEST_MISSING" in capsys.readouterr().err


def test_o84_without_a_token_variable_the_server_gets_the_fixed_placeholder_for_both_hosts(
    tmp_path, fake, frozen, events, monkeypatch
):
    """O84: a freeze needs no credential (the tools do not depend on the token), so without `--token-env` both of the
    server's token settings hold the tool's fixed placeholder, which is no token of any run."""
    config, _, token = launching(tmp_path, fake, frozen, events, "compose")
    seen_file = recorded(tmp_path, monkeypatch)
    assert manifest_tool(config, "check", "--pass-env", RECORD) == 0
    seen = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen["ONEDATA_ONEZONE_TOKEN"] == seen["ONEDATA_ONEPROVIDER_TOKEN"] == toolmanifest.FREEZE_TOKEN
    assert token.reveal() != toolmanifest.FREEZE_TOKEN


def test_o84_the_launch_and_the_manifest_tool_build_the_servers_start_in_one_place(tmp_path, fake, frozen, events):
    """O84: `server_start`, which both the launch and the tool use, yields the command and arguments `server_argv`
    gives, the environment `server_environment` builds (key by key, a caller's own variable included) and a private
    empty directory, each time a new one."""
    config, _, token = launching(tmp_path, fake, frozen, events, "compose")
    condition, extra = config.condition("read"), {"OTHER": "kept"}
    directories = []
    for _ in range(2):
        with server_start(config, condition, token, extra) as begun:
            assert (begun.command, begun.args) == server_argv(config)
            assert begun.env == server_environment(config, condition, token, extra)
            assert begun.env["OTHER"] == "kept"
            assert sorted(os.listdir(begun.cwd)) == []
            assert stat.S_IMODE(begun.cwd.stat().st_mode) == stat.S_IRWXU
            directories.append(begun.cwd)
    assert directories[0] != directories[1]
    assert not any(d.exists() for d in directories)
