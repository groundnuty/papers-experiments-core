"""Shared fixtures: a throwaway git repository that ignores the developer's own git configuration."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.provenance.study_tokens import render_hash_file


class Repo:
    """A temporary git repository with helpers to write files and commit them."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        """Run git inside the repository and return its stdout, stripped."""
        done = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def write(self, relative: str, text: str) -> Path:
        """Write a text file below the repository root, creating directories."""
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def commit(self, message: str) -> str:
        """Stage everything, commit without signing, and return the new commit's sha."""
        self.git("add", "--all")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """Create an empty repository whose git calls ignore global and system configuration."""
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return Repo(root)


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
    """A repository with a clean commit, a leaking one and a fixing one, plus the hash file the scripts read."""
    repo.write("tests/data/study_tokens.sha256", render_hash_file(["secret-space"]))
    repo.write("src/ok.py", "x = 1\n")
    first = repo.commit("clean")
    repo.write("src/leak.py", 'name = "secret-space"\n')
    second = repo.commit("leak")
    (repo.path / "src" / "leak.py").unlink()
    third = repo.commit("fix")
    return repo, first, second, third
