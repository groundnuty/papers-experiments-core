"""The core's own identity (C06, P01): its commit, git tree, dirt and Python digest, read from a checkout of its own or
from the identity its build wrote beside it, and the gate a study command holds it to.

Every repository here is a throwaway one (`make_repo`), shaped like the core's project: what is read is git's answer,
never the checkout the suite runs from. The real installs, one commit five ways, are in `test_core_installs.py`.
"""

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from expcore.provenance import core_identity
from expcore.provenance.core_check import CoreVersionUnknown, recorded_core_problem, require_core
from expcore.provenance.core_identity import (
    BUILD_FILE,
    SHIPPED,
    build_bytes,
    build_identity,
    from_build,
    from_checkout,
    identity_of,
    installed_digest,
    package_files,
)

ROOT = Path(__file__).resolve().parents[2]
PROJECT = '[project]\nname = "papers-experiments-core"\nversion = "0.1.0"\n'


def core_project(repo) -> Path:
    """A committed checkout shaped like the core's: its project file, both packages and a cache git ignores."""
    repo.write("pyproject.toml", PROJECT)
    repo.write(".gitignore", "__pycache__/\nbuild/\n")
    repo.write("src/expcore/__init__.py", "")
    repo.write("src/expcore/provenance/__init__.py", "")
    repo.write("src/expcore/cli.py", "WIRE = 'blue'\n")
    repo.write("src/expcore_onedata/__init__.py", "")
    repo.write("docs/notes.md", "notes\n")
    repo.commit("the core")
    return repo.path


def installed(site: Path, checkout: Path) -> Path:
    """The packages of a checkout installed below `site` as a wheel lays them out, with the build identity beside."""
    for package in ("expcore", "expcore_onedata"):
        shutil.copytree(checkout / "src" / package, site / package, ignore=shutil.ignore_patterns("__pycache__"))
    (site / "expcore" / BUILD_FILE).write_bytes(build_bytes(build_identity(checkout)))
    return site / "expcore"


def test_c06_a_clean_checkout_is_named_by_its_commit_and_tree(repo):
    """C06: read from git, the identity is HEAD and HEAD's tree, clean, found in the checkout."""
    root = core_project(repo)
    found = from_checkout(root)
    assert (found.commit, found.tree) == (repo.git("rev-parse", "HEAD"), repo.git("rev-parse", "HEAD^{tree}"))
    assert (found.dirty, found.source, found.why) == (False, "checkout", None)
    assert identity_of(root / "src" / "expcore") == found


def test_p01_a_tracked_change_anywhere_or_an_untracked_file_a_wheel_ships_is_dirt_and_a_marker_at_the_top_is_not(repo):
    """P01: a tracked file edited anywhere (a note) is dirt, and so is a file git does not track under what a wheel
    ships, and a Python file git ignores in the packages; an untracked file elsewhere, like the `.ok` uv leaves at the
    top of its git checkouts, ships nothing and is not, and neither is an ignored cache."""
    root = core_project(repo)
    (root / ".ok").write_text("", encoding="utf-8")
    (root / "src" / "expcore" / "__pycache__").mkdir()
    (root / "src" / "expcore" / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"compiled")
    assert from_checkout(root).dirty is False
    (root / "docs" / "notes.md").write_text("edited\n", encoding="utf-8")
    edited = from_checkout(root)
    assert edited.dirty
    assert "uncommitted changes to 1 tracked file" in str(edited.why)
    repo.git("checkout", "--", "docs/notes.md")
    (root / "src" / "expcore" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    untracked = from_checkout(root)
    assert untracked.dirty
    assert "src/expcore/new.py" in str(untracked.why)
    (root / "src" / "expcore" / "new.py").unlink()
    (root / "src" / "expcore" / "build").mkdir()
    (root / "src" / "expcore" / "build" / "hidden.py").write_text("HIDDEN = 1\n", encoding="utf-8")
    ignored = from_checkout(root)  # imported, and in no commit or wheel: git ignores build/ here
    assert ignored.dirty
    assert "Python files in the packages that git ignores: src/expcore/build/hidden.py" in str(ignored.why)


def test_c06_a_linked_subpackage_is_walked_and_its_link_is_dirt(repo, tmp_path):
    """C06: a subpackage that is a link to a directory outside the tree is imported like any other, so its code is in
    the digest and a change to it changes the digest; in a checkout the link is dirt (git holds where it points, not
    what it leads to), and an installed core whose linked code changed since its build is stale."""
    root = core_project(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_text("RATE = 1\n", encoding="utf-8")
    (root / "src" / "expcore" / "stats").symlink_to(outside, target_is_directory=True)
    (root / "src" / "expcore" / "loop").symlink_to(root / "src" / "expcore", target_is_directory=True)  # a cycle
    repo.commit("two subpackages by link")  # git holds each link's target path; the status is clean
    linked = from_checkout(root)
    assert linked.dirty
    assert "whose targets no commit holds: src/expcore/loop, src/expcore/stats" in str(linked.why)
    (outside / "__init__.py").write_text("RATE = 2\n", encoding="utf-8")
    assert from_checkout(root).python_sha256 != linked.python_sha256
    (root / "src" / "expcore" / "loop").unlink()
    site = tmp_path / "site"
    package = installed(site, root)  # the copy follows the link: its files, as a wheel ships them
    shutil.rmtree(site / "expcore" / "stats")
    (site / "expcore" / "stats").symlink_to(outside, target_is_directory=True)
    assert not identity_of(package).stale  # the link leads to the code the build hashed
    (outside / "__init__.py").write_text("RATE = 3\n", encoding="utf-8")
    assert identity_of(package).stale


def test_c06_a_link_to_a_package_directory_never_takes_it_out_of_the_digest(repo, tmp_path):
    """C06: a link under the example's place (which the digest leaves out) or a second name for a subpackage leaves
    the subpackage hashed under its own name, so a change to its code changes the digest; in a checkout, a link git
    holds is dirt even under the example's place."""
    site = tmp_path / "site"
    (site / "expcore" / "stats").mkdir(parents=True)
    (site / "expcore" / "examples").mkdir()
    (site / "expcore" / "__init__.py").write_text("", encoding="utf-8")
    (site / "expcore" / "stats" / "__init__.py").write_text("RATE = 1\n", encoding="utf-8")
    (site / "expcore" / "examples" / "alias").symlink_to(site / "expcore" / "stats", target_is_directory=True)
    (site / "expcore" / "second").symlink_to(site / "expcore" / "stats", target_is_directory=True)
    names = [name for name, _ in package_files(site)[0]]
    assert names == ["expcore/__init__.py", "expcore/second/__init__.py", "expcore/stats/__init__.py"]
    before = installed_digest(site)
    (site / "expcore" / "stats" / "__init__.py").write_text("RATE = 2\n", encoding="utf-8")
    assert installed_digest(site) != before
    root = core_project(repo)
    (root / "src" / "expcore" / "examples").mkdir()
    (root / "src" / "expcore" / "examples" / "alias").symlink_to(tmp_path / "anywhere", target_is_directory=True)
    repo.commit("a link under the example's place")
    assert "links in the packages" in str(from_checkout(root).why)


def test_c06_two_dirty_trees_at_one_commit_are_told_apart_by_their_python_digest(repo):
    """C06: dirt does not make two codes one: each dirty tree's digest is its own source's, and the clean tree's
    digest comes back when the edit is undone."""
    root = core_project(repo)
    clean = from_checkout(root).python_sha256
    (root / "src" / "expcore" / "cli.py").write_text("WIRE = 'red'\n", encoding="utf-8")
    red = from_checkout(root)
    (root / "src" / "expcore" / "cli.py").write_text("WIRE = 'green'\n", encoding="utf-8")
    green = from_checkout(root)
    assert red.dirty
    assert green.dirty
    assert red.commit == green.commit
    assert len({clean, red.python_sha256, green.python_sha256}) == 3
    assert red.entry() != green.entry()
    repo.git("checkout", "--", "src/expcore/cli.py")
    assert from_checkout(root).python_sha256 == clean


def test_c06_an_installed_core_reads_its_build_identity_and_records_the_checkouts_entry(repo, tmp_path):
    """C06: the identity a build wrote, read beside an installed core whose source is the build's, gives the entry the
    checkout gives; the example study a wheel carries under expcore/examples is not part of the digest."""
    root = core_project(repo)
    package = installed(tmp_path / "site", root)
    (package / "examples" / "tiny_study").mkdir(parents=True)
    (package / "examples" / "tiny_study" / "plugin.py").write_text("def build(context): ...\n", encoding="utf-8")
    found = from_build(package)
    assert found is not None
    assert found.source == "build"
    assert not found.dirty
    assert found.entry() == from_checkout(root).entry()
    assert identity_of(package) == found
    assert installed_digest(tmp_path / "site") == found.python_sha256


def test_p01_an_installed_core_edited_since_its_build_is_stale_and_without_a_build_identity_it_is_unknown(
    repo, tmp_path
):
    """P01: an installed core whose source differs from the digest its build recorded is stale, and stale is dirty;
    one with no build identity (or a withheld one, as an anonymised package ships) has an unknown commit, dirty."""
    root = core_project(repo)
    package = installed(tmp_path / "site", root)
    (package / "cli.py").write_text("WIRE = 'edited in place'\n", encoding="utf-8")
    stale = identity_of(package)
    assert stale.stale
    assert stale.dirty
    assert stale.commit == repo.git("rev-parse", "HEAD")
    (package / BUILD_FILE).write_text('{"withheld": "for an anonymous review"}\n', encoding="utf-8")
    withheld = identity_of(package)
    assert (withheld.commit, withheld.dirty, withheld.known) == ("unknown", True, False)
    (package / BUILD_FILE).unlink()
    assert identity_of(package).commit == "unknown"


def test_c06_a_build_from_a_dirty_checkout_says_dirty_and_a_build_without_git_takes_the_sdists_identity(repo, tmp_path):
    """C06: the build identity of a dirty checkout says dirty; a tree without git (an unpacked sdist) gives the
    identity the sdist carries, as it is; with neither, the commit is unknown."""
    root = core_project(repo)
    clean = build_identity(root)
    (root / "src" / "expcore" / "cli.py").write_text("WIRE = 'red'\n", encoding="utf-8")
    assert build_identity(root)["dirty"] is True
    repo.git("checkout", "--", "src/expcore/cli.py")
    unpacked = tmp_path / "unpacked"
    shutil.copytree(root, unpacked, ignore=shutil.ignore_patterns(".git"))
    assert build_identity(unpacked)["commit"] == "unknown"
    (unpacked / "src" / "expcore" / BUILD_FILE).write_bytes(build_bytes(clean))
    assert build_identity(unpacked) == clean
    assert json.loads(build_bytes(clean)) == clean
    assert build_bytes(clean) == build_bytes(dict(clean))


def test_c06_a_repository_around_the_core_is_never_taken_for_the_cores_own(make_repo, tmp_path):
    """C06: a core unpacked inside another project's repository (a vendored sdist) is not named by that repository's
    commit: only a checkout whose top is the core's own project is read from git."""
    outer = make_repo("study")
    outer.write("README.md", "a study\n")
    inner = outer.path / "vendor" / "core"
    (inner / "src" / "expcore").mkdir(parents=True)
    (inner / "pyproject.toml").write_text(PROJECT, encoding="utf-8")
    (inner / "src" / "expcore" / "__init__.py").write_text("", encoding="utf-8")
    outer.commit("a study with the core inside")
    found = identity_of(inner / "src" / "expcore")
    assert found.commit == "unknown"
    assert found.source == "none"


def test_c06_what_counts_as_shipped_covers_every_path_the_wheel_includes():
    """C06: an untracked file is dirt where a wheel would ship it: under every path `only-include` names."""
    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]
    assert all(f"{path}/".startswith(SHIPPED) for path in wheel["wheel"]["only-include"])


def test_p01_the_gate_refuses_an_unknown_or_stale_core_always_and_a_dirty_one_unless_allowed(repo, monkeypatch):
    """P01: the study's rule, for the core: unknown and stale are refused even when dirt is allowed, dirty is refused
    unless it is, and a clean core passes."""
    root = core_project(repo)
    clean = from_checkout(root)
    (root / "src" / "expcore" / "cli.py").write_text("WIRE = 'red'\n", encoding="utf-8")
    dirty = from_checkout(root)
    unknown = core_identity.CoreIdentity("unknown", None, True, clean.python_sha256, "none", "no git")
    stale = core_identity.CoreIdentity(clean.commit, clean.tree, True, "0" * 64, "build", "edited", stale=True)
    for found, allowed, refusal in [
        (unknown, True, "the core's commit is unknown"),
        (stale, True, "the installed core is stale"),
        (dirty, False, "refusing to start on a dirty core"),
    ]:
        monkeypatch.setattr(core_identity, "current", lambda found=found: found)
        with pytest.raises(CoreVersionUnknown, match=refusal):
            require_core("run", allow_dirty=allowed)
    monkeypatch.setattr(core_identity, "current", lambda: dirty)
    assert require_core("run", allow_dirty=True) == dirty
    monkeypatch.setattr(core_identity, "current", lambda: clean)
    assert require_core("run") == clean


def test_p01_a_recorded_environment_names_its_dirty_or_unknown_core_and_the_earlier_form_names_nothing(repo):
    """P01: what a package build reads of a check's environment: a dirty core or an unknown commit is a problem; the
    earlier form, and an environment without sources (an anonymised one), record no dirt to read."""
    root = core_project(repo)
    entry = from_checkout(root).entry()
    env = {"python": "3.12", "distributions": {}, "sources": {"papers-experiments-core": entry}}
    assert recorded_core_problem(env) is None
    dirty = {**env, "sources": {"papers-experiments-core": {**entry, "dirty": True}}}
    assert "uncommitted changes" in str(recorded_core_problem(dirty))
    unknown = {**env, "sources": {"papers-experiments-core": {**entry, "commit": "unknown"}}}
    assert "unknown" in str(recorded_core_problem(unknown))
    earlier = {**env, "sources": {"papers-experiments-core": {"kind": "directory", "editable": False}}}
    assert recorded_core_problem(earlier) is None
    assert recorded_core_problem({"python": "3.12", "distributions": {}}) is None
