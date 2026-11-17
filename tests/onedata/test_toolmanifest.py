"""O84: the manifest command line verifies the pinned checkout before it starts the server, as the launch does.

A manifest frozen from a clone at another commit, or with a local change, would list tools no run's server has, so
`freeze` and `check` refuse such a clone, naming the difference, before anything is started or written.
"""

import pytest

from expcore_onedata import toolmanifest
from expcore_onedata.testing import write_platform

RECORD = "FAKE_MCP_RECORD"


def pinned(tmp_path, make_repo):
    """A clone with one commit, and a platform file that pins it; return both."""
    clone = make_repo("server")
    clone.write("server.py", "print(1)\n")
    commit = clone.commit("pinned")

    def pin(data):
        data["toolserver"].update(clone=str(clone.path), commit=commit)

    return clone, write_platform(tmp_path, pin)


def run(action, platform):
    """The command line on the platform's `read` condition, passing the stand-in server its record file's name."""
    return toolmanifest.main([action, str(platform), "read", "--pass-env", RECORD])


def test_o84_freeze_and_check_start_the_server_from_a_clean_clone_at_the_pinned_commit(
    tmp_path, make_repo, monkeypatch
):
    """O84: a clone at the pinned commit with no local change is no obstacle: the server starts for both actions."""
    _, platform = pinned(tmp_path, make_repo)
    seen = tmp_path / "seen.json"
    monkeypatch.setenv(RECORD, str(seen))
    for action in ("freeze", "check"):
        assert run(action, platform) == 0
        assert seen.exists(), action
        seen.unlink()


@pytest.mark.parametrize("action", ["freeze", "check"])
def test_o84_a_clone_at_another_commit_than_the_pin_is_refused_before_the_server_starts(
    tmp_path, make_repo, monkeypatch, capsys, action
):
    """O84: the clone has moved on from the pinned commit, and `freeze` and `check` refuse it with the launch's own
    message, start nothing and write nothing: the manifest the earlier freeze made is as it was."""
    clone, platform = pinned(tmp_path, make_repo)
    seen, declared = tmp_path / "seen.json", tmp_path / "read.manifest.json"
    monkeypatch.setenv(RECORD, str(seen))
    assert run("freeze", platform) == 0  # a manifest to check against, made while the clone was at the pin
    seen.unlink()
    before = declared.read_bytes()
    clone.write("other.py", "x = 1\n")
    clone.commit("moved on")
    capsys.readouterr()
    assert run(action, platform) == 2
    assert "not at the pinned commit" in capsys.readouterr().err
    assert not seen.exists()
    assert declared.read_bytes() == before


@pytest.mark.parametrize("action", ["freeze", "check"])
def test_o84_a_clone_with_a_local_change_is_refused_before_the_server_starts(
    tmp_path, make_repo, monkeypatch, capsys, action
):
    """O84: a tracked file of the clone differs from its commit, and `freeze` and `check` refuse it, naming the local
    changes, start nothing and write nothing."""
    clone, platform = pinned(tmp_path, make_repo)
    seen, declared = tmp_path / "seen.json", tmp_path / "read.manifest.json"
    monkeypatch.setenv(RECORD, str(seen))
    assert run("freeze", platform) == 0
    seen.unlink()
    before = declared.read_bytes()
    clone.write("server.py", "print(2)\n")
    capsys.readouterr()
    assert run(action, platform) == 2
    assert "local changes" in capsys.readouterr().err
    assert not seen.exists()
    assert declared.read_bytes() == before
