"""The fresh-repository check (scripts/fresh_repo.py): a new study repository depends on the core by commit and runs
the example study without copying a file from a checkout.

The full check needs a commit the repository host can serve, so it runs in the fresh-repo workflow after a push to
main, and here only as a maintainer test (EXPCORE_FRESH_REPO_SHA). The rest of the script's logic is tested with a
local path, and its study steps run for real against the interpreter that runs the tests.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from study_support import clean_environment

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fresh_repo.py"
SHA = "0123456789abcdef0123456789abcdef01234567"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


def load_script():
    spec = importlib.util.spec_from_file_location("fresh_repo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fresh = load_script()


def test_the_dependency_is_the_cores_repository_at_a_commit_or_a_tag():
    assert (
        fresh.dependency(SHA)
        == f"papers-experiments-core @ git+https://github.com/groundnuty/papers-experiments-core@{SHA}"
    )
    assert fresh.dependency("v0.1.0").endswith("papers-experiments-core@v0.1.0")
    for bad in ("main", SHA[:12], SHA.upper(), "v0.1"):
        with pytest.raises(ValueError, match="not a full commit id"):
            fresh.dependency(bad)
    with pytest.raises(ValueError, match="a commit is needed"):
        fresh.dependency(None)


def test_a_local_path_or_file_url_stands_in_for_the_repository_in_tests(tmp_path):
    assert fresh.dependency(None, str(tmp_path)) == f"papers-experiments-core @ {tmp_path.resolve().as_uri()}"
    assert fresh.dependency(SHA, tmp_path.as_uri()) == f"papers-experiments-core @ git+{tmp_path.as_uri()}@{SHA}"


def test_the_plan_adds_the_core_by_commit_writes_the_example_from_it_and_copies_nothing(tmp_path, capsys):
    assert fresh.main(["--sha", SHA, "--dry-run", "--workdir", str(tmp_path / "fresh")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "git init --quiet",
        "uv init --bare --no-workspace --vcs none --name fresh-study --python 3.12",
        f"uv add --python 3.12 'papers-experiments-core @ git+https://github.com/groundnuty/papers-experiments-core@{SHA}'",
    ]
    assert "uv run python -m expcore example study" in lines
    assert "git add -- study pyproject.toml uv.lock" in lines
    for command in ("run study", "check study/out/runs", "analyze study", "report study"):
        assert f"uv run python -m expcore {command}" in lines
    assert lines[-1].startswith("make -C unpacked/tiny-study-package check PYTHON=")
    assert not any(str(ROOT) in line for line in lines)  # nothing names the checkout these tests run from
    assert not any(line.split()[0] in ("cp", "rsync", "install") for line in lines)
    assert not (tmp_path / "fresh").exists()  # a dry run makes nothing


def test_the_script_refuses_a_bad_commit_a_workdir_that_holds_anything_and_a_skip_without_a_runner(tmp_path, capsys):
    assert fresh.main(["--sha", "main", "--dry-run"]) == 2
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x", encoding="utf-8")
    assert fresh.main(["--sha", SHA, "--workdir", str(tmp_path / "busy")]) == 2
    assert fresh.main(["--skip-install"]) == 2
    assert "not empty" in capsys.readouterr().err


def test_the_commands_never_inherit_another_repository_or_environment():
    env = {"GIT_DIR": "/elsewhere/.git", "VIRTUAL_ENV": "/elsewhere", "UV_PROJECT_ENVIRONMENT": "/v", "PATH": "/bin"}
    assert fresh.clean_environment(env) == {"PATH": "/bin"}


def test_the_study_steps_run_the_example_end_to_end_in_a_new_repository_and_its_package_checks(
    tmp_path, clean_environment
):
    """Everything but the install, against this interpreter: the example comes from the core through `example`, the
    commit makes the code identity clean, and the package passes its own make check."""
    workdir = tmp_path / "fresh"
    workdir.mkdir()
    args = fresh.build_parser().parse_args(["--skip-install", "--runner", sys.executable, "--workdir", str(workdir)])
    assert fresh.run(fresh.plan(args, workdir), workdir, fresh.clean_environment(clean_environment)) == 0
    log = subprocess.run(["git", "log", "--format=%s"], cwd=workdir, capture_output=True, text=True, check=True)
    assert log.stdout.splitlines() == ["the example study"]
    assert (workdir / "study" / "out" / "report.md").is_file()
    assert (workdir / "unpacked" / "tiny-study-package" / "SHA256SUMS").is_file()


@pytest.mark.skipif(
    not os.environ.get("EXPCORE_FRESH_REPO_SHA"),
    reason="maintainer check: set EXPCORE_FRESH_REPO_SHA to a pushed commit of the core (needs the network)",
)
def test_a_fresh_repository_depends_on_a_pushed_commit_and_runs_the_example(tmp_path, clean_environment):
    """The whole check: uv adds the core by commit from its repository, and nothing is copied from this checkout."""
    sha = os.environ["EXPCORE_FRESH_REPO_SHA"]
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--sha", sha, "--workdir", str(tmp_path / "fresh")],
        capture_output=True,
        text=True,
        env=fresh.clean_environment(clean_environment),
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
