"""The core's identity across a study's commands (C06, C29, P01): a dirty core is refused for a scored or merged result
unless dirt is allowed, two commits of one version are two cores that never merge, and a store an earlier core recorded
keeps resuming, checking and analysing in its own form, while a merge never mixes the two forms.

The core's identity is replaced, for one command at a time, by one read from a throwaway repository (`make_repo`) or by
one at an invented commit: what is under test is what the commands do with it. The example study runs for real.
"""

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from shard_support import make_study, run_shard
from study_support import Campaign, clean_environment, cli, finish

from expcore.package.build import build_package
from expcore.package.spec import PackageError
from expcore.provenance import core_identity
from expcore.provenance import environment as environment_module
from expcore.provenance.core_identity import from_checkout
from expcore.provenance.environment import CORE_DISTRIBUTION, environment
from expcore.study import pipeline

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

OTHER_COMMIT = "5eed" * 10  # invented: a second commit at the core's one version
OTHER_TREE = "7b" * 20  # invented


def store_environment(root: Path) -> dict:
    connection = sqlite3.connect(root / "out" / "state.sqlite")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    finally:
        connection.close()
    return json.loads(row[0])["environment"]


@pytest.fixture
def dirty_core(make_repo) -> core_identity.CoreIdentity:
    """The identity of a core checkout with an uncommitted change, read from git."""
    repo = make_repo("core")
    repo.write("pyproject.toml", '[project]\nname = "papers-experiments-core"\nversion = "0.1.0"\n')
    repo.write("src/expcore/__init__.py", "")
    repo.write("src/expcore_onedata/__init__.py", "")
    repo.commit("the core")
    repo.write("src/expcore/__init__.py", "EDITED = True\n")
    found = from_checkout(repo.path)
    assert found.dirty
    return found


def at_another_commit(pinned: core_identity.CoreIdentity) -> core_identity.CoreIdentity:
    return dataclasses.replace(pinned, commit=OTHER_COMMIT, tree=OTHER_TREE)


def earlier_form(patch: pytest.MonkeyPatch) -> None:
    """Make the pipeline record the core as a core before its identity did: by how it was installed."""
    patch.setattr(pipeline, "study_environment", lambda study: environment(study.requires, earlier_core=True))


def test_p01_a_dirty_core_is_refused_for_a_scored_run_unless_dirt_is_allowed_and_then_recorded_dirty(
    tmp_path, clean_environment, dirty_core, monkeypatch
):
    """P01: with the core dirty, run, check and analyze are refused, each naming the dirt, as dirty study code is; with
    --allow-dirty the pilot runs to its report, its environment says the core was dirty, and a package refuses it."""
    campaign = Campaign(make_study(tmp_path, "study"))
    monkeypatch.setattr(core_identity, "current", lambda: dirty_core)
    code, errors = cli(clean_environment, "run", str(campaign.root))
    assert code == 1
    assert "refusing to start on a dirty core (uncommitted changes" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()
    code, errors = cli(clean_environment, "run", str(campaign.root), "--allow-dirty")
    assert code == 0, errors
    assert store_environment(campaign.root)["sources"][CORE_DISTRIBUTION] == dirty_core.entry()
    for command, target in (("check", campaign.runs), ("analyze", campaign.root)):
        code, errors = cli(clean_environment, command, str(target))
        assert code == 1
        assert "refusing to start on a dirty core" in errors
    for command, target in (("check", campaign.runs), ("analyze", campaign.root), ("report", campaign.root)):
        code, errors = cli(clean_environment, command, str(target), "--allow-dirty")
        assert code == 0, errors
    with pytest.raises(PackageError, match=r"a core that is not a clean commit \(the core \(.{12}\) had uncommitted"):
        build_package(campaign.root, tmp_path / "dist")


def test_c06_a_run_at_another_commit_of_one_version_does_not_resume_the_campaign(
    tmp_path, clean_environment, committed_core, monkeypatch
):
    """C06: one version, another commit: the resume is refused, naming the core's commit as it was and as it is."""
    campaign = Campaign(make_study(tmp_path, "study"))
    assert cli(clean_environment, "run", str(campaign.root))[0] == 0
    monkeypatch.setattr(core_identity, "current", lambda: at_another_commit(committed_core))
    code, errors = cli(clean_environment, "run", str(campaign.root))
    assert code == 1
    assert f"sources.{CORE_DISTRIBUTION}.commit: recorded {committed_core.commit!r}, now {OTHER_COMMIT!r}" in errors


def test_c29_shards_run_on_two_commits_of_one_version_are_refused_at_merge_naming_both(
    tmp_path, clean_environment, committed_core, monkeypatch
):
    """C29: two shards whose cores share 0.1.0 and differ in commit are two cores: the merge refuses them, naming both
    commits, and writes nothing."""
    one = run_shard(tmp_path, "shard-one", clean_environment, 1)
    with monkeypatch.context() as patch:
        patch.setattr(core_identity, "current", lambda: at_another_commit(committed_core))
        two = run_shard(tmp_path, "shard-two", clean_environment, 2)
    target = Campaign(make_study(tmp_path, "merged"))
    code, errors = cli(
        clean_environment, "merge", str(target.root / "out"), str(one.root / "out"), str(two.root / "out")
    )
    assert code == 1
    assert f"ran the core at commit {OTHER_COMMIT}" in errors
    assert f"at commit {committed_core.commit}: shards merge only when they ran one core" in errors
    assert not (target.root / "out" / "runs").exists()


def test_p01_a_dirty_core_merges_a_pilots_shards_only_when_dirt_is_allowed(
    tmp_path, clean_environment, dirty_core, monkeypatch
):
    """P01: shards a dirty core ran, under --allow-dirty, merge under that core only with --allow-dirty too."""
    monkeypatch.setattr(core_identity, "current", lambda: dirty_core)
    shards = []
    for index, name in ((1, "shard-one"), (2, "shard-two")):
        root = make_study(tmp_path, name)
        argv = ("run", str(root), "--shard", f"{index}/2", "--shard-label", f"shard-{index}", "--allow-dirty")
        code, errors = cli(clean_environment, *argv)
        assert code == 0, errors
        shards.append(str(root / "out"))
    target = make_study(tmp_path, "merged")
    code, errors = cli(clean_environment, "merge", str(target / "out"), *shards)
    assert code == 1
    assert "refusing to start on a dirty core" in errors
    code, errors = cli(clean_environment, "merge", str(target / "out"), *shards, "--allow-dirty")
    assert code == 0, errors


def test_c06_a_store_an_earlier_core_recorded_resumes_is_checked_and_analysed_in_its_form(
    tmp_path, clean_environment, monkeypatch, capsys
):
    """C06: a store whose environment records the core by how it was installed, as a core before its identity did,
    resumes under that form with its fingerprint unchanged, saying so, and is checked and analysed: each compares it
    with the core in use read in the same form."""
    campaign = Campaign(make_study(tmp_path, "study"))
    with monkeypatch.context() as patch:
        earlier_form(patch)
        assert cli(clean_environment, "run", str(campaign.root))[0] == 0
    before = store_environment(campaign.root)
    assert environment_module.is_earlier_form(before)
    code, errors = cli(clean_environment, "run", str(campaign.root))
    assert code == 0, errors
    assert "the store recorded the core in its earlier form, by how it was installed; it resumes" in errors
    assert store_environment(campaign.root) == before
    finish(clean_environment, campaign)
    assert campaign.codes == {"check": 0, "analyze": 0, "report": 0}


def test_c06_an_earlier_store_whose_install_differs_from_the_cores_now_is_refused_as_before(
    tmp_path, clean_environment, monkeypatch
):
    """C06: the earlier form is compared as the earlier core compared it, and nothing weaker: a store recorded with the
    core installed from a git URL at one commit refuses a core that the same form reads otherwise now."""
    campaign = Campaign(make_study(tmp_path, "study"))
    real = environment_module.source_of
    at = {"kind": "vcs", "vcs": "git", "url": "https://code.example.org/lab/core", "commit": OTHER_COMMIT}
    with monkeypatch.context() as patch:
        earlier_form(patch)
        patch.setattr(
            environment_module, "source_of", lambda d: at if d.metadata["Name"] == CORE_DISTRIBUTION else real(d)
        )
        assert cli(clean_environment, "run", str(campaign.root))[0] == 0
    code, errors = cli(clean_environment, "run", str(campaign.root))
    assert code == 1
    assert f"sources.{CORE_DISTRIBUTION}" in errors


def test_c29_a_merge_never_mixes_the_two_forms_and_merges_shards_that_all_hold_the_earlier_one(
    tmp_path, clean_environment, monkeypatch
):
    """C29: a shard an earlier core recorded beside one recorded by the core's identity is refused, naming both; shards
    that all hold the earlier form merge under it, said on stderr, and the merged batch is checked in it."""
    with monkeypatch.context() as patch:
        earlier_form(patch)
        old_one = run_shard(tmp_path, "old-one", clean_environment, 1)
        old_two = run_shard(tmp_path, "old-two", clean_environment, 2)
    new_two = run_shard(tmp_path, "new-two", clean_environment, 2)
    mixed = Campaign(make_study(tmp_path, "mixed"))
    outs = (str(old_one.root / "out"), str(new_two.root / "out"))
    code, errors = cli(clean_environment, "merge", str(mixed.root / "out"), *outs)
    assert code == 1
    assert "records the core by how it was installed, as an earlier core recorded it" in errors
    assert "shards of the two forms are not one campaign" in errors
    merged = Campaign(make_study(tmp_path, "merged"))
    outs = (str(old_one.root / "out"), str(old_two.root / "out"))
    code, errors = cli(clean_environment, "merge", str(merged.root / "out"), *outs)
    assert code == 0, errors
    assert "every shard recorded the core in its earlier form" in errors
    finish(clean_environment, merged)
    assert merged.codes == {"check": 0, "analyze": 0, "report": 0}
