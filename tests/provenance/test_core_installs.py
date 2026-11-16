"""One commit of the core installed five real ways records one entry, and another commit at the same version another
(C06): editable, from a git URL, from a directory, from a wheel, and from the wheel built out of the sdist.

The core's project (its sources, the example, the token hashes and the build hook) is copied into a throwaway
repository and committed, so nothing depends on whether the checkout the suite runs from is committed. Each install
goes into a fresh environment with uv, offline and without dependencies: the identity module imports nothing beyond
the standard library, and hatchling, the build backend, is in uv's cache once the project is synced. Nothing the
suite pins (`core_pin`) reaches these processes: each reads its own core.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from expcore.provenance.core_identity import BUILD_FILE
from expcore.provenance.environment import CORE_DISTRIBUTION, differences

ROOT = Path(__file__).resolve().parents[2]
UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(UV is None, reason="the install tests run uv, which installs the project")
PROJECT_FILES = ("pyproject.toml", "README.md", "hatch_build.py", ".gitignore", "tests/data/study_tokens.sha256")
PROJECT_TREES = ("src", "examples/tiny_study")
READ = (
    "import json; from expcore.provenance.core_identity import current; found = current(); "
    "print(json.dumps({'entry': found.entry(), 'source': found.source, 'why': found.why}))"
)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
}


def run(*argv: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV, "UV_OFFLINE": "1"}, check=False
    )
    assert done.returncode == 0, f"{' '.join(argv)}: exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    return done.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return run("git", "-C", str(repo), *args)


def listed(path: str) -> list[str]:
    """The files under `path` that a commit of the checkout would hold: tracked, or untracked and not ignored."""
    return [line for line in git(ROOT, "ls-files", "-co", "--exclude-standard", "--", path).splitlines() if line]


def core_repository(where: Path) -> Path:
    """A repository holding the core's project as the checkout holds it, committed once."""
    where.mkdir(parents=True)
    for relative in [*PROJECT_FILES, *(file for tree in PROJECT_TREES for file in listed(tree))]:
        source, target = ROOT / relative, where / relative
        if source.is_file():  # a tracked file deleted in the checkout is left out, as a commit of it would leave it
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    run("git", "init", "--quiet", str(where))
    git(where, "add", "--all")
    git(where, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "the core")
    return where


def venv(where: Path) -> Path:
    run(str(UV), "venv", "--quiet", "--python", sys.executable, str(where))
    return where / "bin" / "python"


def install(python: Path, *what: str) -> None:
    run(str(UV), "pip", "install", "--quiet", "--no-deps", "--python", str(python), *what)


def wheel_of(repo: Path, out: Path, *, through_sdist: bool = False) -> Path:
    run(str(UV), "build", "--quiet", *(() if through_sdist else ("--wheel",)), "--out-dir", str(out), str(repo))
    [wheel] = out.glob("*.whl")
    return wheel


def identity_in(python: Path) -> dict:
    return json.loads(run(str(python), "-I", "-c", READ))


@pytest.fixture(scope="module")
def core_repo(tmp_path_factory) -> Path:
    return core_repository(tmp_path_factory.mktemp("core") / "repo")


def test_c06_one_commit_installed_five_ways_records_one_entry_naming_it(core_repo, tmp_path):
    """C06: editable, a git URL, a directory, a wheel and the wheel built from the sdist give the core one entry: the
    repository's HEAD and its tree, clean, with one digest of the source; the earlier form (by direct URL) gave five."""
    head, tree = git(core_repo, "rev-parse", "HEAD"), git(core_repo, "rev-parse", "HEAD^{tree}")
    ways = {
        "editable": ("-e", str(core_repo)),
        "git URL": (f"{CORE_DISTRIBUTION} @ git+file://{core_repo}@{head}",),
        "directory": (str(core_repo),),
        "wheel": (str(wheel_of(core_repo, tmp_path / "dist-wheel")),),
        "sdist": (str(wheel_of(core_repo, tmp_path / "dist-sdist", through_sdist=True)),),
    }
    found = {}
    for name, what in ways.items():
        python = venv(tmp_path / "venvs" / name.replace(" ", "-"))
        install(python, *what)
        found[name] = identity_in(python)
    entries = {name: json.dumps(read["entry"], sort_keys=True) for name, read in found.items()}
    assert len(set(entries.values())) == 1, entries
    entry = found["wheel"]["entry"]
    assert entry["commit"] == head
    assert entry["tree"] == tree
    assert entry["dirty"] is False
    assert {name: read["source"] for name, read in found.items()} == {
        "editable": "checkout",
        "git URL": "build",
        "directory": "build",
        "wheel": "build",
        "sdist": "build",
    }
    assert git(core_repo, "status", "--porcelain") == ""  # no install wrote into the checkout


def test_c06_two_commits_at_one_version_record_two_entries_and_a_dirty_build_says_so(core_repo, tmp_path):
    """C06: a second commit at 0.1.0 installs as other code (its commit, tree and digest differ), so its campaign is
    another; a wheel built from a checkout with an uncommitted change records it dirty; the wheel carries its identity
    at expcore/_build_identity.json."""
    second = Path(shutil.copytree(core_repo, tmp_path / "second"))
    module = second / "src" / "expcore" / "provenance" / "core_identity.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# a second commit at the same version\n", "utf-8")
    git(second, "-c", "commit.gpgsign=false", "commit", "--quiet", "--all", "-m", "a second commit")
    first_wheel, second_wheel = wheel_of(core_repo, tmp_path / "one"), wheel_of(second, tmp_path / "two")
    assert first_wheel.name == second_wheel.name  # one version, one file name
    with zipfile.ZipFile(second_wheel) as archive:
        carried = json.loads(archive.read(f"expcore/{BUILD_FILE}"))
    assert carried["commit"] == git(second, "rev-parse", "HEAD")
    entries = []
    for name, wheel in (("one", first_wheel), ("two", second_wheel)):
        python = venv(tmp_path / f"venv-{name}")
        install(python, str(wheel))
        entries.append(identity_in(python)["entry"])
    one, two = entries
    assert {key for key in one if one[key] != two[key]} == {"commit", "tree", "python_sha256"}
    env = {"python": "3.12", "distributions": {CORE_DISTRIBUTION: "0.1.0"}}
    lines = differences({**env, "sources": {CORE_DISTRIBUTION: one}}, {**env, "sources": {CORE_DISTRIBUTION: two}})
    assert lines == [f"{CORE_DISTRIBUTION} is other code than the one recorded: its commit, tree, python_sha256 differ"]
    assert one["commit"][:12] not in lines[0]
    module.write_text(module.read_text(encoding="utf-8") + "# uncommitted\n", "utf-8")
    python = venv(tmp_path / "venv-dirty")
    install(python, str(wheel_of(second, tmp_path / "dirty")))
    dirty = identity_in(python)
    assert dirty["entry"]["dirty"] is True
    assert dirty["entry"]["commit"] == two["commit"]
    assert "uncommitted changes" in dirty["why"]
