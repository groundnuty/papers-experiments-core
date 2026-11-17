"""The pinned tool-server checkout: the commit it holds, and that it is clean."""

import os
import stat
import subprocess

import pytest
from git_index import aged_copy, index_state, read_only_tree

from expcore_onedata.config import load_platform
from expcore_onedata.pin import PinError, verify_configured, verify_pin
from expcore_onedata.testing import write_platform


@pytest.fixture
def clone(make_repo):
    repo = make_repo("server")
    repo.write("server.py", "print(1)\n")
    repo.commit("pinned")
    return repo


def test_a_clean_checkout_at_the_pinned_commit_passes(clone):
    commit = clone.git("rev-parse", "HEAD")
    assert verify_pin(clone.path, commit) == commit


def test_another_commit_a_local_change_or_no_checkout_is_refused(clone, tmp_path):
    commit = clone.git("rev-parse", "HEAD")
    with pytest.raises(PinError, match="not at the pinned commit"):
        verify_pin(clone.path, "0" * 40)
    clone.write("server.py", "print(2)\n")
    with pytest.raises(PinError, match="local changes"):
        verify_pin(clone.path, commit)
    with pytest.raises(PinError, match="git rev-parse failed"):
        verify_pin(tmp_path / "missing", commit)


def test_a_configuration_without_a_pin_verifies_nothing(tmp_path, fake):
    config = load_platform(write_platform(tmp_path))
    assert config.toolserver.clone is None
    assert verify_configured(config.toolserver) is None


def test_the_configuration_names_the_checkout_and_the_commit(tmp_path, fake, clone):
    commit = clone.git("rev-parse", "HEAD")
    config = load_platform(
        write_platform(tmp_path, lambda d: d["toolserver"].update(clone=str(clone.path), commit=commit))
    )
    assert verify_configured(config.toolserver) == commit


def test_o81_the_launch_check_does_not_write_the_checkout_it_checks(clone, tmp_path):
    """O81: a tracked file's mtime changes (a fresh copy, a restore); the check still leaves `.git/index` as it was."""
    commit = clone.git("rev-parse", "HEAD")
    copy = aged_copy(clone.path, tmp_path / "copy")
    before = index_state(copy)
    assert verify_pin(copy, commit) == commit
    assert index_state(copy) == before


def test_o81_a_changed_tracked_file_is_still_refused_and_the_refusal_writes_nothing(clone, tmp_path):
    """O81: the answer does not change with the flag: dirty stays dirty, and the check still does not write."""
    commit = clone.git("rev-parse", "HEAD")
    copy = aged_copy(clone.path, tmp_path / "copy")
    (copy / "server.py").write_text("print(2)\n", encoding="utf-8")
    before = index_state(copy)
    with pytest.raises(PinError, match="local changes"):
        verify_pin(copy, commit)
    assert index_state(copy) == before
    with pytest.raises(PinError, match="not at the pinned commit"):
        verify_pin(copy, "0" * 40)
    assert index_state(copy) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only directory")
def test_o81_a_checkout_with_a_read_only_git_directory_verifies_and_refuses_as_before(clone, tmp_path):
    """O81: a read-only `.git` (a copy shipped read-only) answers as a writable one does. This also passes without the
    flag, since git's refresh is opportunistic; it guards the read-only copy a study ships."""
    commit = clone.git("rev-parse", "HEAD")
    copy = aged_copy(clone.path, tmp_path / "copy")
    with read_only_tree(copy / ".git"):
        assert not os.access(copy / ".git" / "index", os.W_OK)
        assert verify_pin(copy, commit) == commit
        (copy / "server.py").write_text("print(2)\n", encoding="utf-8")
        with pytest.raises(PinError, match="local changes"):
            verify_pin(copy, commit)
    assert stat.S_IMODE((copy / ".git" / "index").stat().st_mode) & stat.S_IWUSR  # restored for the cleanup


def test_o81_the_users_global_git_configuration_is_not_read_by_the_launch_check(clone, tmp_path, monkeypatch):
    """O81: a global `core.fsmonitor` hook runs on a plain `git status` and must not run on the check."""
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nprintf '\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    config = tmp_path / "global.gitconfig"
    config.write_text(f"[core]\n\tfsmonitor = {hook}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    commit = clone.git("rev-parse", "HEAD")
    probe, copy = aged_copy(clone.path, tmp_path / "probe"), aged_copy(clone.path, tmp_path / "copy")
    marker.unlink(missing_ok=True)  # making the copies is not what is measured
    subprocess.run(["git", "-C", str(probe), "status", "--porcelain", "--untracked-files=no"], capture_output=True)
    if not marker.exists():
        pytest.skip("this host runs no git fsmonitor hook, so the test cannot tell a read configuration from none")
    marker.unlink()
    assert verify_pin(copy, commit) == commit
    assert not marker.exists()
