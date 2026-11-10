"""A package says which of its files no commit holds, and an anonymised one refuses them unless told otherwise (#61)."""

import json
import shutil
import tarfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.cli import main
from expcore.package import build as package_build
from expcore.package.build import PACKAGE_FILE, build_package, verify_package
from expcore.package.spec import PackageError
from expcore.package.tracking import tracking
from expcore.provenance.hashfile import render_hash_file
from expcore.provenance.redact import hidden_name

NAME = "tiny-study-package"

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file(["zarnothwick"]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    campaign = Campaign(make_copy(tmp_path_factory.mktemp("campaign"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    return campaign


@pytest.fixture
def anonymisation(tmp_path) -> Path:
    path = tmp_path / "anon.toml"
    path.write_text('files = ["README.md"]\n\n[terms]\n"Quillon Varek" = "Author A"\n', encoding="utf-8")
    return path


@pytest.fixture
def committed(campaign, repo, tmp_path, monkeypatch) -> Path:
    """The study, finished, committed to a repository of its own; out/ is ignored there, as in the example."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    root = copy_campaign(campaign, repo.path, "study").root
    assert (root / ".gitignore").read_text(encoding="utf-8").startswith("out/")
    repo.commit("the study")
    return root


def include(root: Path, *paths: str) -> None:
    spec = root / "package.toml"
    added = "".join(f'    "{path}",\n' for path in paths)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace('    "README.md",\n', f'    "README.md",\n{added}'), "utf-8"
    )


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def package_json(archive: Path, where: Path) -> dict:
    return json.loads((unpack(archive, where) / PACKAGE_FILE).read_text(encoding="utf-8"))


def test_k12_a_study_committed_in_full_has_nothing_to_list_and_nothing_to_warn_about(committed, hashes, tmp_path):
    """K12: out/ is ignored and generated, and the code has its identity: neither is reported as uncommitted."""
    warned: list[str] = []
    archive = build_package(committed, tmp_path / "dist", hashes=hashes, warn=warned.append)
    data = package_json(archive, tmp_path / "unpacked")
    assert (data["vcs"], data["uncommitted"]) == ("git", [])
    assert warned == []


def test_k12_an_uncommitted_and_an_untracked_file_are_warned_about_and_listed(committed, hashes, tmp_path):
    """K12: a tracked file changed since its commit (modified) and a file git does not track (untracked), both
    included, are named in the warning and in PACKAGE.json; the build goes on."""
    with (committed / "README.md").open("a", encoding="utf-8") as readme:
        readme.write("\nA note added after the commit.\n")
    (committed / "notes.txt").write_text("not in the repository\n", encoding="utf-8")
    include(committed, "notes.txt")
    warned: list[str] = []
    archive = build_package(committed, tmp_path / "dist", hashes=hashes, warn=warned.append)
    data = package_json(archive, tmp_path / "unpacked")
    assert data["uncommitted"] == [
        {"path": "README.md", "state": "modified"},
        {"path": "notes.txt", "state": "untracked"},
        {"path": "package.toml", "state": "modified"},
    ]
    assert len(warned) == 1
    assert "README.md (modified)" in warned[0]
    assert "notes.txt (untracked)" in warned[0]
    assert PACKAGE_FILE in warned[0]


def test_k12_an_anonymised_build_refuses_uncommitted_files_unless_they_are_allowed(
    committed, hashes, anonymisation, tmp_path
):
    """K12: an anonymised package names no commit, so a file no commit holds has nothing behind it: refused, naming
    it, unless the author passes the option, and then it is listed like any other."""
    (committed / "notes.txt").write_text("not in the repository\n", encoding="utf-8")
    include(committed, "notes.txt")
    with pytest.raises(PackageError, match=r"notes\.txt \(untracked\).*--allow-uncommitted"):
        build_package(committed, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert not list((tmp_path / "dist").glob("*.tar.gz"))
    warned: list[str] = []
    archive = build_package(
        committed, tmp_path / "dist", hashes=hashes, anonymise=anonymisation, allow_uncommitted=True, warn=warned.append
    )
    root = unpack(archive, tmp_path / "unpacked")
    listed = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))["uncommitted"]
    assert {"path": "notes.txt", "state": "untracked"} in listed
    assert warned
    assert verify_package(root) == []


def test_k12_a_path_that_holds_a_listed_term_is_shown_only_by_its_place(committed, hashes, anonymisation, tmp_path):
    """K12: the refusal names no term of the anonymisation list, even in a file name."""
    name = "quillon varek notes.txt"
    (committed / name).write_text("a note\n", encoding="utf-8")
    include(committed, name)
    with pytest.raises(PackageError, match=r"\(untracked\)") as refused:
        build_package(committed, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert "quillon" not in str(refused.value).lower()


def test_k05_the_refusal_carries_each_hidden_path_for_the_author_to_ask_for(committed, hashes, anonymisation, tmp_path):
    """K05: a file no commit holds whose path holds a term is named by its hash in the refusal, which carries the path
    itself only for the command line to show when the author asks."""
    name = "quillon varek notes.txt"
    (committed / name).write_text("a note\n", encoding="utf-8")
    include(committed, name)
    with pytest.raises(PackageError) as refused:
        build_package(committed, tmp_path / "dist", hashes=hashes, anonymise=anonymisation)
    assert refused.value.private == {hidden_name(name): name}
    assert hidden_name(name) in str(refused.value)


def test_k12_the_warning_about_an_uncommitted_file_hides_a_path_that_holds_a_private_name(committed, hashes, tmp_path):
    """K12: the hashes load before the tracking check, so its warning names a path that holds a private name by its
    hash, like every later message, and never shows the name."""
    name = "zarnothwick-notes.txt"  # holds the invented private name the hash file lists
    (committed / name).write_text("a note\n", encoding="utf-8")
    include(committed, name)
    warned: list[str] = []
    with pytest.raises(PackageError) as refused:  # the path's own finding refuses the build afterwards
        build_package(committed, tmp_path / "dist", hashes=hashes, warn=warned.append)
    assert [line for line in warned if "no commit holds" in line] == [
        "included but no commit holds them, listed in PACKAGE.json: "
        f"package.toml (modified), {hidden_name(name)} (untracked)"
    ]
    assert "zarnothwick" not in "".join(warned) + str(refused.value)


def test_k12_a_study_outside_any_repository_says_it_cannot_tell_and_is_not_refused(
    campaign, hashes, anonymisation, tmp_path, monkeypatch
):
    """K12: a stamped snapshot has no commit to compare with: the build says so, records that, and refuses nothing."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    root = copy_campaign(campaign, tmp_path, "copy").root
    warned: list[str] = []
    archive = build_package(root, tmp_path / "dist", hashes=hashes, anonymise=anonymisation, warn=warned.append)
    data = package_json(archive, tmp_path / "unpacked")
    assert (data["vcs"], data["uncommitted"]) == ("none", [])
    assert "cannot be told" in warned[0]


def test_k12_the_command_line_warns_on_standard_error_and_takes_the_option(
    committed, hashes, anonymisation, tmp_path, capsys
):
    """K12: the warning reaches the author; --allow-uncommitted reaches the build."""
    (committed / "notes.txt").write_text("not in the repository\n", encoding="utf-8")
    include(committed, "notes.txt")
    argv = ["package", str(committed), "--out", str(tmp_path / "dist"), "--hashes", str(hashes)]
    anonymised = [*argv, "--anonymise", str(anonymisation)]
    assert main(anonymised) == 1
    assert "--allow-uncommitted" in capsys.readouterr().err
    assert main([*anonymised, "--allow-uncommitted"]) == 0
    assert "notes.txt (untracked)" in capsys.readouterr().err
    shutil.rmtree(tmp_path / "dist")


def test_k12_tracking_reads_paths_relative_to_a_study_in_a_subdirectory(repo):
    """K12: a study is often a directory of its repository, and paths are relative to the study."""
    repo.write("sub/a.txt", "a\n")
    repo.write("sub/b.txt", "b\n")
    repo.commit("two files")
    (repo.path / "sub" / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo.path / "sub" / "c.txt").write_text("new\n", encoding="utf-8")
    state = tracking(repo.path / "sub", ["a.txt", "b.txt", "c.txt"])
    assert state.vcs == "git"
    assert state.uncommitted == (("a.txt", "modified"), ("c.txt", "untracked"))
