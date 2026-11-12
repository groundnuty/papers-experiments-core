"""The pinned tool-server checkout: the commit it holds, and that it is clean."""

import pytest

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
