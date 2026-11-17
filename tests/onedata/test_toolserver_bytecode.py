"""O84: the tool server is told to write no bytecode, so starting it never changes the tree it certifies.

A Python that imports a module writes its `.pyc` into a `__pycache__` beside it. The server is a Python started from
trees a study freezes (its clone of the server, its virtualenv, the interpreter's own library). On a platform without
`[addresses] resolver` it was started as `<python> -m <module>` with neither `-B` nor `PYTHONDONTWRITEBYTECODE`, and a
`check` of its manifest, or a launch, wrote bytecode into the clone, the virtualenv and the interpreter's library: one
measured start left 22 files in the clone and over a thousand in the virtualenv, and each file changes a digest of the
tree. The resolve launcher already runs `-I -B`, so only the platform without it was affected.

The environment the platform builds for the server now says `PYTHONDONTWRITEBYTECODE=1` on every platform, as a managed
setting (a caller can neither give nor clear it). The tests start a real child Python from `server_start`, the one
definition the launch and `python -m expcore_onedata.toolmanifest` share, with nothing inherited from the test's own
environment, and look at the tree it imports from. Names are invented.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from expcore_onedata import toolmanifest
from expcore_onedata.addresses import FORMAT
from expcore_onedata.config import load_platform
from expcore_onedata.testing import write_platform
from expcore_onedata.toolserver import MANAGED_ENV, server_argv, server_environment, server_start

BYTECODE = "PYTHONDONTWRITEBYTECODE"  # spelled out, not imported: the test names the variable the interpreter reads
LAUNCHER = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "toolshim" / "resolve_launch.py"
PLATFORMS = ("compose", "hpc")  # a plain platform, and one with `[addresses]` whose server starts through the launcher
TOKEN = SimpleNamespace(reveal=lambda: "tok-0123456789")  # all `server_environment` asks of a token
SOMEWHERE = ["-m", "somewhere"]  # a server the test never starts

STAND_IN = """\
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # a server started with -I has no script directory
import helper  # noqa: E402, F401  (a sibling module: importing it is what writes a .pyc)
import lib.inner  # noqa: E402, F401  (a package: so does importing this)

print(json.dumps({"dont_write_bytecode": sys.dont_write_bytecode, "word": os.environ.get("PYTHONDONTWRITEBYTECODE")}))
"""
WRAPPER = """\
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # a server started with -I has no script directory
import helper  # noqa: E402, F401
import lib.inner  # noqa: E402, F401

runpy.run_module("expcore_onedata.testing.fake_onedata_mcp", run_name="__main__")
"""


def platform(tmp_path: Path, kind: str, args: list[str]):
    """A loaded platform of `kind` whose tool server is `python args`; no credential is read, no manifest needed."""

    def edit(data):
        data["toolserver"].update(command=sys.executable, args=args)
        if kind == "hpc":
            data["addresses"] = {"file": "map.json", "resolver": str(LAUNCHER)}

    if kind == "hpc":
        document = {"format": FORMAT, "hosts": {"zone.test": {"address": "127.0.0.1", "nodes": []}}}
        (tmp_path / "map.json").write_text(json.dumps(document), encoding="utf-8")
        (tmp_path / "map.json").chmod(0o600)
    return load_platform(write_platform(tmp_path, edit), require_secrets=False, require_manifests=False)


def plant(directory: Path) -> Path:
    """A tree of the server's own code: a script and a wrapper, a sibling module and a package; return its directory."""
    code = directory / "srv"
    (code / "lib").mkdir(parents=True)
    (code / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (code / "lib" / "inner.py").write_text("VALUE = 1\n", encoding="utf-8")
    (code / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    (code / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
    (code / "wrapper.py").write_text(WRAPPER, encoding="utf-8")
    return code


def bytecode_under(tree: Path) -> list[str]:
    """Every cache directory and compiled file under `tree`, as paths relative to it."""
    return sorted(str(p.relative_to(tree)) for p in tree.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc")


def run_child(command: str, args: list[str], env: dict[str, str], cwd: Path) -> dict:
    """Run the server's command to its end with exactly `env` (nothing of the test's own) and return what it printed."""
    done = subprocess.run([command, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_the_servers_environment_says_write_no_bytecode_on_both_platforms(tmp_path, kind):
    """O84: the environment the platform builds for the server holds PYTHONDONTWRITEBYTECODE=1 on a Compose platform
    and on an HPC platform whose server starts through the resolve launcher, and the variable is one of the settings
    the launch fixes."""
    config = platform(tmp_path, kind, SOMEWHERE)
    env = server_environment(config, config.condition("read"), TOKEN)
    assert env[BYTECODE] == "1"
    assert BYTECODE in MANAGED_ENV
    _, args = server_argv(config)
    assert (kind == "hpc") == (str(LAUNCHER) in args)


@pytest.mark.parametrize("word", ["0", "1", ""])
def test_o84_a_caller_cannot_set_or_clear_the_bytecode_setting(tmp_path, word):
    """O84: PYTHONDONTWRITEBYTECODE is fixed by the launch, whatever a caller gives it: a caller's `extra_env` that
    names it is refused, so a caller cannot turn writing back on (with 0 or an empty value) or set it itself."""
    config = platform(tmp_path, "compose", SOMEWHERE)
    condition = config.condition("read")
    with pytest.raises(ValueError, match=BYTECODE):
        server_environment(config, condition, TOKEN, {BYTECODE: word})
    with pytest.raises(ValueError, match=BYTECODE), server_start(config, condition, TOKEN, {BYTECODE: word}):
        pass


@pytest.mark.parametrize("held", [True, False])
def test_o84_pass_env_refuses_the_bytecode_setting_whether_the_driver_holds_it_or_not(
    tmp_path, monkeypatch, capsys, held
):
    """O84: `python -m expcore_onedata.toolmanifest freeze|check --pass-env PYTHONDONTWRITEBYTECODE` is refused,
    naming it, whether or not the driver's environment holds it (a driver that runs Python with `-B` may); nothing is
    started and the manifest is not written."""
    config = platform(tmp_path, "compose", SOMEWHERE)
    if held:
        monkeypatch.setenv(BYTECODE, "1")
    else:
        monkeypatch.delenv(BYTECODE, raising=False)
    for action in ("freeze", "check"):
        assert toolmanifest.main([action, str(config.file), "read", "--pass-env", BYTECODE]) == 2
        error = capsys.readouterr().err
        assert BYTECODE in error
        assert "the launch sets" in error
    assert not config.condition("read").manifest.exists()


@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_a_real_server_child_started_by_the_platform_writes_no_bytecode_into_its_tree(tmp_path, kind):
    """O84: a real child Python started from `server_start`, which the launch and the manifest command share, on a
    Compose platform and through the resolve launcher on an HPC one, imports a sibling module and a package of its own
    tree and leaves no `__pycache__` and no `.pyc` anywhere in it, with nothing inherited from the test's environment.
    (The launcher runs `-I -B` already, so the HPC case guards that; the Compose case is the one that wrote files.)"""
    code = plant(tmp_path)
    config = platform(tmp_path, kind, [str(code / "stand_in.py")])
    with server_start(config, config.condition("read"), TOKEN) as begun:
        seen = run_child(begun.command, begun.args, begun.env, begun.cwd)
    assert seen == {"dont_write_bytecode": True, "word": "1"}
    assert bytecode_under(code) == []


def test_o84_the_same_child_without_the_setting_does_write_bytecode(tmp_path):
    """O84: the control. The child of the test above, started with the platform's environment minus the one setting,
    writes a `__pycache__` for what it imports, so the empty tree of that test is the setting's doing and not that the
    interpreter or the directory could not have written."""
    code = plant(tmp_path)
    config = platform(tmp_path, "compose", [str(code / "stand_in.py")])
    with server_start(config, config.condition("read"), TOKEN) as begun:
        env = {name: value for name, value in begun.env.items() if name != BYTECODE}
        seen = run_child(begun.command, begun.args, env, begun.cwd)
    assert seen == {"dont_write_bytecode": False, "word": None}
    assert {"__pycache__", "lib/__pycache__"} <= set(bytecode_under(code))


@pytest.mark.parametrize("kind", PLATFORMS)
def test_o84_freeze_and_check_of_a_real_server_write_no_bytecode_into_the_tree_they_certify(tmp_path, kind):
    """O84: `python -m expcore_onedata.toolmanifest freeze` and then `check` start a real MCP server (the stand-in,
    behind a wrapper that imports a sibling module and a package of its own tree) as the launch does, on both
    platforms, and the tree it runs from holds no bytecode afterwards."""
    code = plant(tmp_path)
    config = platform(tmp_path, kind, [str(code / "wrapper.py"), "serve"])
    for action in ("freeze", "check"):
        assert toolmanifest.main([action, str(config.file), "read"]) == 0
    assert bytecode_under(code) == []
