"""The pre-push hook checks every commit being pushed, driven here with git's stdin protocol and a stub uv."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.provenance.study_tokens import render_hash_file

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "pre-push"
ZERO = "0" * 40
TOKEN = "secret-space"


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """A directory holding a stand-in for `uv run --frozen python ...` that runs the current interpreter."""
    directory = tmp_path / "bin"
    directory.mkdir()
    stub = directory / "uv"
    stub.write_text(f'#!/bin/sh\nshift 3\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return directory


@pytest.fixture
def pushed(repo):
    """A repository with a clean commit, a leaking one and a fixing one, plus the hash file the hook reads."""
    repo.write("tests/data/study_tokens.sha256", render_hash_file([TOKEN]))
    repo.write("src/ok.py", "x = 1\n")
    first = repo.commit("clean")
    repo.write("src/leak.py", f'name = "{TOKEN}"\n')
    second = repo.commit("leak")
    (repo.path / "src" / "leak.py").unlink()
    third = repo.commit("fix")
    return repo, first, second, third


def run_hook(repo, bin_dir: Path, lines: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [str(HOOK), "origin"], cwd=repo.path, input=lines, capture_output=True, text=True, env=env, check=False
    )


def test_hook_is_executable():
    assert os.access(HOOK, os.X_OK)


def test_an_update_whose_range_contains_a_leak_is_refused_even_if_the_tip_is_clean(pushed, bin_dir):
    repo, first, _, third = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/main {third} refs/heads/main {first}\n")
    assert done.returncode == 1
    assert "src/leak.py:1 string" in done.stdout
    assert TOKEN not in done.stdout + done.stderr


def test_an_update_of_clean_commits_is_allowed(pushed, bin_dir):
    repo, _, second, third = pushed
    done = run_hook(repo, bin_dir, f"refs/heads/main {third} refs/heads/main {second}\n")
    assert done.returncode == 0, done.stderr


def test_a_new_branch_checks_only_what_the_remote_does_not_have(pushed, bin_dir):
    repo, _, second, third = pushed
    everything = run_hook(repo, bin_dir, f"refs/heads/topic {third} refs/heads/topic {ZERO}\n")
    assert everything.returncode == 1
    repo.git("update-ref", "refs/remotes/origin/main", second)
    only_new = run_hook(repo, bin_dir, f"refs/heads/topic {third} refs/heads/topic {ZERO}\n")
    assert only_new.returncode == 0, only_new.stderr


def test_deleting_a_remote_ref_pushes_no_commits(pushed, bin_dir):
    repo, _, second, _ = pushed
    assert run_hook(repo, bin_dir, f"(delete) {ZERO} refs/heads/gone {second}\n").returncode == 0


def test_one_bad_ref_among_several_fails_the_push(pushed, bin_dir):
    repo, first, second, third = pushed
    lines = f"refs/heads/a {third} refs/heads/a {second}\nrefs/heads/b {third} refs/heads/b {first}\n"
    assert run_hook(repo, bin_dir, lines).returncode == 1
