"""O80: a launch-time or check-time git read never writes the clone it reads, and the places that run git say so."""

import ast
import os
from pathlib import Path

import pytest
from git_index import aged_copy, plain_status_writes_index

SRC = Path(__file__).resolve().parents[2] / "src"
SUBPROCESS_RUNNERS = {"run", "Popen", "check_output", "check_call", "call"}


def test_the_aged_copy_is_one_a_plain_git_status_writes(repo, tmp_path):
    """The other O80 tests are only worth passing if this setup makes an unprotected `git status` write the index."""
    (repo.path / "a.txt").write_text("one\n", encoding="utf-8")
    repo.commit("one")
    assert plain_status_writes_index(aged_copy(repo.path, tmp_path / "copy"))


def test_o80_git_env_keeps_the_environment_and_asks_for_no_optional_locks(monkeypatch):
    """O80: the environment git gets is the process's own plus the flag, so PATH and a step's allowlist stay."""
    from expcore.provenance.gitobjects import git_env

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")  # an inherited value does not win
    monkeypatch.setenv("EXPCORE_PROBE", "kept")
    before = dict(os.environ)
    env = git_env()
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["EXPCORE_PROBE"] == "kept"
    assert env["PATH"] == os.environ["PATH"]
    assert dict(os.environ) == before  # the process's own environment is not changed


def test_o80_git_env_leaves_git_configuration_alone_unless_asked_to_isolate_it(monkeypatch):
    """O80: isolation points the global and the system configuration at nothing, and only when asked."""
    from expcore.provenance.gitobjects import git_env

    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    plain, isolated = git_env(), git_env(isolate_config=True)
    assert "GIT_CONFIG_GLOBAL" not in plain
    assert "GIT_CONFIG_NOSYSTEM" not in plain
    assert isolated["GIT_CONFIG_GLOBAL"] == os.devnull
    assert isolated["GIT_CONFIG_NOSYSTEM"] == "1"
    assert isolated["GIT_OPTIONAL_LOCKS"] == "0"


def _git_calls(path: Path):
    """Each `subprocess` call in the file whose argument list starts with "git"."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
            continue
        owner = node.func.value
        argv = node.args[0]
        if (
            isinstance(owner, ast.Name)
            and owner.id == "subprocess"
            and node.func.attr in SUBPROCESS_RUNNERS
            and isinstance(argv, ast.List | ast.Tuple)
            and argv.elts
            and isinstance(argv.elts[0], ast.Constant)
            and argv.elts[0].value == "git"
        ):
            yield node


def test_o80_every_git_call_in_the_source_passes_the_environment_git_env_makes():
    """O80: a new place that runs git in `src/` must give it `env=git_env(...)`, or this fails and names it."""
    seen, missing = 0, []
    for path in sorted(SRC.rglob("*.py")):
        for call in _git_calls(path):
            seen += 1
            env = next((k.value for k in call.keywords if k.arg == "env"), None)
            named = isinstance(env, ast.Call) and getattr(env.func, "id", None) == "git_env"
            if not named:
                missing.append(f"{path.relative_to(SRC.parent)}:{call.lineno}")
    assert seen >= 4, "the scan found fewer git calls than the four helpers it guards"
    assert not missing, f"git runs without env=git_env(...) at: {', '.join(missing)}"


@pytest.mark.parametrize("module", ["pin", "codestamp", "core_identity", "gitobjects"])
def test_the_scan_sees_each_helper_that_runs_git(module):
    """The scan is not vacuous: it reaches the four modules that run git."""
    (path,) = list(SRC.rglob(f"{module}.py"))
    assert list(_git_calls(path)), f"{module}.py runs git, and the scan did not find it"
