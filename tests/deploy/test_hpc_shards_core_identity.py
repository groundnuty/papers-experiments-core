"""C06: `shards.py plan`, `lines` and `step` refuse a core that names no code before a shard reaches its node.

`expcore run` refuses to start on a core whose commit is unknown (a copy without `.git` and without a build identity,
which is what a `git archive` installs) or whose installed source differs from its build's (`core_check.require_core`).
The step used to find that out on the node, after the shard was dispatched. These tests build the cores a site can
have as real interpreters, each in a virtual environment of its own with no pip: a core beside nothing that names it, a
wheel's files with the build identity its build wrote, the same edited since, and a checkout of the core's project
reached through a `.pth` file, as an editable install is. `plan` and `lines` ask the manifest's interpreter (a process
of its own, as the step will run); `step` reads its own. Every path, commit and job here is invented, and nothing runs
srun.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest
from fingerprint_support import standin_python

from expcore.provenance import core_identity

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
COMMIT, TREE = "ab" * 20, "cd" * 20
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}
# What an operator is told to do, in the words of the message (the lead's wording, so a rewording shows here).
REMEDY = (
    "use a git checkout of the core, or a wheel built with its build identity "
    "(hatch build writes expcore/_build_identity.json)"
)


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_core_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_core_identity"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_core_identity", None)


# ------------------------------------------------------------------------------------------------ the cores a site has
def _git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"


def _environment(where: Path) -> tuple[Path, Path]:
    """A virtual environment with no pip: its interpreter and its site directory."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(where)
    return where / "bin" / "python", next((where / "lib").glob("python*/site-packages"))


def _identity_module(parent: Path) -> Path:
    """The package `expcore` below `parent`, holding the identity module alone (it imports nothing of the core)."""
    package = parent / "expcore"
    (package / "provenance").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provenance" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(core_identity.__file__, package / "provenance" / "core_identity.py")
    return package


def _built(site: Path, *, digest: str | None = None) -> None:
    """The file a wheel's build writes beside `expcore`: the commit, and the digest of the source it was built from."""
    data = {
        "format": core_identity.BUILD_FORMAT,
        "commit": COMMIT,
        "tree": TREE,
        "dirty": False,
        "python_sha256": digest or core_identity.installed_digest(site),
    }
    (site / "expcore" / core_identity.BUILD_FILE).write_bytes(core_identity.build_bytes(data))


def make_cores(base: Path) -> dict[str, SimpleNamespace]:
    """Each core as `python` (the interpreter that imports it), `core_src` (what it is imported from), and `package`."""
    cores = {}
    python, site = _environment(base / "archive")  # a `git archive`: the files, no .git, no build identity
    cores["archive"] = SimpleNamespace(python=python, core_src=site, package=_identity_module(site))
    python, site = _environment(base / "wheel")
    _identity_module(site)
    _built(site)
    cores["wheel"] = SimpleNamespace(python=python, core_src=site, package=site / "expcore")
    python, site = _environment(base / "stale")  # a wheel whose files were edited after its build
    _identity_module(site)
    _built(site, digest="0" * 64)
    cores["stale"] = SimpleNamespace(python=python, core_src=site, package=site / "expcore")
    repo = base / "core"  # a checkout of the core's project, committed
    _identity_module(repo / "src")
    (repo / "pyproject.toml").write_text('[project]\nname = "papers-experiments-core"\nversion = "0.0.0"\n')
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, env={**os.environ, **GIT_ENV})
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "the core")
    python, site = _environment(base / "checkout")
    (site / "core.pth").write_text(f"{repo / 'src'}\n", encoding="utf-8")  # as an editable install is
    cores["checkout"] = SimpleNamespace(python=python, core_src=repo / "src", package=repo / "src" / "expcore")
    return cores


@pytest.fixture(scope="module")
def cores(tmp_path_factory):
    return make_cores(tmp_path_factory.mktemp("cores"))


def step_path() -> str:
    """The step's PATH, holding the directory of the `git` this suite runs."""
    git = shutil.which("git")
    assert git, "the suite needs git"
    return f"{Path(git).parent}:/usr/bin:/bin"


def write_manifest(tmp_path: Path, python: Path | str, core_src: Path) -> Path:
    """The manifest of two shards on two federations that are ready, as `plan` reads it. `plan` and `lines` also take
    each shard's campaign fingerprint (O63), which these cores cannot (each holds the identity module alone): the
    manifest's `python` is the fingerprint stand-in, which answers the core probe as `python`, the real interpreter of
    the core, answers it."""
    (tmp_path / "study").mkdir(exist_ok=True)
    python = standin_python(tmp_path / "standin", core_python=python)
    roots = [tmp_path / "fed" / name for name in ("a", "b")]
    for n, root in enumerate(roots, 1):
        root.mkdir(parents=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
    lines = [
        f'study = "{tmp_path / "study"}"',
        f'python = "{python}"',
        f'core_src = "{core_src}"',
        f'results = "{tmp_path / "results"}"',
        f'tmpfs = "{tmp_path / "tmpfs"}"',
        f'step_path = "{step_path()}"',
        "cpus = 6",
        'memory = "18g"',
    ]
    for label, root in zip(("fed-a", "fed-b"), roots, strict=True):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{root}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


# ------------------------------------------------------------------------------------------------ plan and lines
REFUSED = [("archive", "unknown"), ("stale", "stale")]


@pytest.mark.parametrize("command", ["plan", "lines"])
@pytest.mark.parametrize(("kind", "said"), REFUSED, ids=["a git archive", "an install edited since its build"])
def test_c06_plan_and_lines_refuse_a_core_without_an_identity_before_printing_a_step(
    shards, cores, tmp_path, capsys, command, kind, said
):
    """C06: the manifest's interpreter imports a core that is unknown (no `.git`, no build identity) or stale: refused
    before any step line is printed, naming what to use instead."""
    core = cores[kind]
    assert shards.main([command, "--manifest", str(write_manifest(tmp_path, core.python, core.core_src))]) == 2
    seen = capsys.readouterr()
    assert seen.out == ""
    assert said in seen.err
    assert REMEDY in seen.err


@pytest.mark.parametrize("command", ["plan", "lines"])
@pytest.mark.parametrize("kind", ["checkout", "wheel"])
def test_c06_plan_and_lines_take_a_checkout_and_a_wheel_with_its_build_identity(
    shards, cores, tmp_path, capsys, command, kind
):
    """C06: a git checkout of the core (an editable install) and a wheel's files with their build identity are cores
    the run will start on: one line per shard, as before."""
    core = cores[kind]
    assert shards.main([command, "--manifest", str(write_manifest(tmp_path, core.python, core.core_src))]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_c06_plan_refuses_an_interpreter_that_cannot_report_the_core(shards, tmp_path, capsys):
    """C06: an interpreter that does not run is not a core that is fine: the refusal says the identity could not be
    read, and nothing is printed."""
    manifest = write_manifest(tmp_path, tmp_path / "no-such-venv" / "bin" / "python", tmp_path)
    assert shards.main(["plan", "--manifest", str(manifest)]) == 2
    seen = capsys.readouterr()
    assert seen.out == ""
    assert "could not report the core's identity" in seen.err


# ------------------------------------------------------------------------------------------------ step
def _step(tmp_path: Path) -> argparse.Namespace:
    study = tmp_path / "study"
    study.mkdir(exist_ok=True)
    (study / "study.toml").write_text("[study]\n")
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False)  # fmt: skip


def _dispatcher():
    seen = []

    def run(argv, env, cwd):
        seen.append(list(argv))
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        return 0

    return run, seen


@pytest.mark.parametrize(("kind", "said"), REFUSED, ids=["a git archive", "an install edited since its build"])
def test_c06_step_refuses_a_core_without_an_identity_before_anything_is_made(
    shards, cores, tmp_path, monkeypatch, kind, said
):
    """C06: the step reads the identity of the core its own interpreter imports, which the dispatcher will run: unknown
    or stale is refused before the step's directory, the work root or the dispatcher exist."""
    monkeypatch.setattr(core_identity, "current", lambda: core_identity.identity_of(cores[kind].package))
    run, seen = _dispatcher()
    with pytest.raises(shards.ShardsError, match=said) as refused:
        shards.step(_step(tmp_path), runner=run, core_found=lambda path: True)
    assert REMEDY in str(refused.value)
    assert not seen
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


@pytest.mark.parametrize("kind", ["checkout", "wheel"])
def test_c06_step_runs_on_a_checkout_and_on_a_wheel_with_its_build_identity(shards, cores, tmp_path, monkeypatch, kind):
    """C06: the cores the run starts on are the cores the step starts on."""
    monkeypatch.setattr(core_identity, "current", lambda: core_identity.identity_of(cores[kind].package))
    run, seen = _dispatcher()
    assert shards.step(_step(tmp_path), runner=run, core_found=lambda path: True) == 0
    assert len(seen) == 1
    assert (tmp_path / "results" / "fed-a.out.tar").is_file()


def test_c06_a_step_command_on_a_git_archived_core_stops_at_its_start(shards, cores, tmp_path):
    """C06: the live case, as a process: `<python> -I shards.py step` on an interpreter whose core is a bare copy exits
    2 with the remedy, and has made nothing on the node's tmpfs or in the results."""
    args = _step(tmp_path)
    command = [
        str(cores["archive"].python), "-I", str(SCRIPT), "step", "--study", str(args.study), "--index", "1",
        "--count", "2", "--label", "fed-a", "--tmpfs", str(args.tmpfs), "--results", str(args.results),
        "--core-src", str(cores["archive"].core_src),
    ]  # fmt: skip
    env = {"PATH": step_path(), "LC_ALL": "C", **shards.home_env(shards.NO_HOME)}
    done = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert done.returncode == 2, done.stderr
    assert REMEDY in done.stderr
    assert not list(args.tmpfs.iterdir())
    assert not list(args.results.iterdir())
