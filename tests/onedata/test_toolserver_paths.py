"""O84 (#382): the tool server starts in an empty directory of its own, so a `[toolserver]` command or argument given
as a path relative to the working directory resolves to nothing there. `load_platform` refuses one when the file is
read, naming the key, instead of the first launch failing."""

import sys

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform

SENTINEL = "relative-sentinel-0042"  # a stand-in for a value of the file that no message may echo


@pytest.fixture(autouse=True)
def _admin_credential(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-token-0123456789")


def _load(tmp_path, **toolserver):
    return load_platform(write_platform(tmp_path, lambda data: data["toolserver"].update(toolserver)))


@pytest.mark.parametrize("command", [f"./{SENTINEL}/server", f"{SENTINEL}/venv/bin/python", f"../{SENTINEL}/python"])
def test_o84_a_relative_command_is_refused_at_load_naming_the_key(tmp_path, command):
    """O84: a `[toolserver] command` with a directory that does not begin at the root is refused when the platform is
    loaded, naming the file, the table and the key and never the value; it tells where a bare name is looked for."""
    with pytest.raises(ConfigError) as refused:
        _load(tmp_path, command=command, args=["-m", "server"])
    message = str(refused.value)
    assert "platform.toml" in message
    assert "[toolserver]" in message
    assert "key 'command'" in message
    assert "absolute" in message
    assert "PATH" in message
    assert SENTINEL not in message


@pytest.mark.parametrize(
    ("command", "args", "position"),
    [
        ("uv", [f"./{SENTINEL}.py"], 1),
        ("uv", ["run", "--project", "."], 3),
        ("uv", ["-m", "pkg", f"--config=../{SENTINEL}.toml"], 3),
        (sys.executable, ["server.py", "--quiet"], 1),
        (sys.executable, ["-m", "pkg", "--root", "~/data"], 4),
    ],
)
def test_o84_a_relative_path_in_the_arguments_is_refused_at_load_naming_the_key_and_its_position(
    tmp_path, command, args, position
):
    """O84: an argument that is a path relative to the working directory (`./x`, `../x`, `.`, `~/x`, an option's value
    of that kind, or the script of a Python server) is refused when the platform is loaded, naming the key `args` and
    the argument's position (counted from 1), never its value."""
    with pytest.raises(ConfigError) as refused:
        _load(tmp_path, command=command, args=args)
    message = str(refused.value)
    assert "platform.toml" in message
    assert "[toolserver]" in message
    assert "key 'args'" in message
    assert f"argument {position} " in message
    assert "absolute" in message
    assert SENTINEL not in message


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (sys.executable, ["-m", "expcore_onedata.testing.fake_onedata_mcp"]),
        ("python3", ["-m", "onedata_mcp", "--quiet"]),
        ("uv", ["run", "--project", "/srv/study", "onedata-mcp"]),
        (sys.executable, ["/srv/study/server.py", "--relay", "http://127.0.0.1:9", "--filter", "application/json"]),
        ("/opt/study/venv/bin/onedata-mcp", ["--config=/etc/onedata-mcp.toml"]),
    ],
)
def test_o84_absolute_paths_bare_names_modules_and_urls_still_load(tmp_path, command, args):
    """O84: the refusal is for a relative path alone: an absolute path, a bare name found on `PATH`, a module, an
    option, a URL and a value with a slash that is no path of the working directory load as they did, unchanged."""
    config = _load(tmp_path, command=command, args=args)
    assert (config.toolserver.command, config.toolserver.args) == (command, tuple(args))
