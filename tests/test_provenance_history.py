"""P07: imported modules reproduce from the source blob their header names (maintainer check plus its own tests)."""

import os
from pathlib import Path

import pytest

from expcore.provenance import history as hist
from expcore.provenance import sanitise as sn
from expcore.provenance.gitobjects import blob_id
from expcore.provenance.headers import format_import_header, insert_header
from expcore.provenance.sanitise_map import load_map

ROOT = Path(__file__).resolve().parents[1]
MAP_TEXT = """\
version = 1

[[entry]]
token = "secret-space"
replacement = "space-a"
category = "space"

[[entry]]
token = "secret_space"
replacement = "space_a"
category = "space"
"""
SOURCE = 'ZONE = "secret-space"\n\n\ndef secret_space():\n    return ZONE\n'
SOURCE_PATH = "pkg/tools.py"
MODULE = "src/expcore/tools.py"


@pytest.fixture
def map_file(tmp_path: Path) -> Path:
    path = tmp_path / "sanitise_map.toml"
    path.write_text(MAP_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def world(make_repo, map_file, tmp_path):
    """A source repository, and a core repository whose module was imported from it in one commit."""
    origin = make_repo("origin")
    origin.write(SOURCE_PATH, SOURCE)
    source_commit = origin.commit("source")
    core = make_repo("core")
    core.write("src/expcore/__init__.py", "# Provenance: new\n")
    core.commit("skeleton")
    staged = tmp_path / "staged.py"
    staged.write_text(SOURCE, encoding="utf-8")
    sn.import_module(map_file, "origin-repo", source_commit[:12], SOURCE_PATH, staged, core.path / MODULE)
    import_commit = core.commit("import")
    return core, origin, source_commit, import_commit, load_map(map_file)


def verify(world) -> list[str]:
    core, origin, _, _, token_map = world
    return hist.verify_history(core.path, {"origin-repo": origin.path}, token_map)


# --- parsing the source-repository setting -------------------------------------------------------------------------


def test_parse_source_repos():
    assert hist.parse_source_repos("a=/x/y; b = /z ;") == {"a": Path("/x/y"), "b": Path("/z")}
    assert hist.parse_source_repos("") == {}


@pytest.mark.parametrize(
    ("spec", "position"),
    [("orchardrepo", 1), ("orchardrepo=", 1), ("=/x", 1), ("a=/x;orchardrepo", 2)],
)
def test_parse_source_repos_rejects_malformed_pairs_naming_only_their_position(spec, position):
    """P07: a source repository's name is the earlier study's, private: a malformed pair is named by its position."""
    with pytest.raises(ValueError, match=f"source repository {position} .*NAME=PATH") as refused:
        hist.parse_source_repos(spec)
    assert "orchardrepo" not in str(refused.value)


# --- the check ------------------------------------------------------------------------------------------------------


def test_a_fresh_import_reproduces(world):
    """P07: applying the map to the named source blob reproduces the import commit's file, minus the header."""
    assert verify(world) == []


def test_later_commits_do_not_matter_only_the_import_commit_does(world):
    core, *_ = world
    core.write(MODULE, (core.path / MODULE).read_text(encoding="utf-8") + "\n\nEXTRA = 1\n")
    core.commit("change after import")
    assert verify(world) == []


def test_a_renamed_module_is_still_traced_to_its_import_commit(world):
    core, *_ = world
    core.git("mv", MODULE, "src/expcore/renamed_tools.py")
    core.commit("rename")
    assert verify(world) == []


def test_a_hand_edited_near_copy_of_an_imported_module_is_reported(world, map_file, tmp_path):
    """P07: a second import of the same source under another name, edited by hand, is checked at its own adding commit.

    Git's copy detection would trace the new file to the clean module it resembles, which is no evidence for it.
    """
    core, _, source_commit, _, _ = world
    staged = tmp_path / "again.py"
    staged.write_text(SOURCE, encoding="utf-8")
    copy = core.path / "src/expcore/tools_copy.py"
    sn.import_module(map_file, "origin-repo", source_commit[:12], SOURCE_PATH, staged, copy)
    copy.write_text(copy.read_text(encoding="utf-8").replace('"space-a"', '"space-b"'), encoding="utf-8")
    core.commit("import again, with one default changed by hand")
    problems = verify(world)
    assert len(problems) == 1
    assert problems[0].startswith("src/expcore/tools_copy.py: import commit ")
    assert "differs from the sanitised source blob" in problems[0]


def test_an_exact_second_import_under_another_name_reproduces(world, map_file, tmp_path):
    core, _, source_commit, _, _ = world
    staged = tmp_path / "again.py"
    staged.write_text(SOURCE, encoding="utf-8")
    sn.import_module(map_file, "origin-repo", source_commit[:12], SOURCE_PATH, staged, core.path / "src/expcore/b.py")
    core.commit("import again, unchanged")
    assert verify(world) == []


def test_a_module_deleted_and_added_again_by_hand_is_checked_at_its_new_adding_commit(world):
    """P07: once a module is deleted, the commit that adds it again starts its history; the old import says nothing."""
    core, *_ = world
    before = (core.path / MODULE).read_text(encoding="utf-8")
    core.git("rm", "--quiet", MODULE)
    core.commit("drop the module")
    core.write(MODULE, before.replace('"space-a"', '"space-b"'))
    core.commit("bring it back, edited")
    problems = verify(world)
    assert len(problems) == 1
    assert "differs from the sanitised source blob" in problems[0]


def test_a_rename_with_an_edit_in_the_same_commit_is_traced_to_the_import(world):
    """P07: a rename deletes the earlier path in the same commit, so the history follows it to the import; the edit is
    a later change, which the history shows."""
    core, *_ = world
    before = (core.path / MODULE).read_text(encoding="utf-8")
    core.git("rm", "--quiet", MODULE)
    core.write("src/expcore/moved.py", before + "\n\nEXTRA = 1\n")
    core.commit("move and extend")
    assert verify(world) == []


def test_new_modules_are_not_checked_against_a_source(world):
    core, *_ = world
    core.write("src/expcore/fresh.py", "# Provenance: new\nx = 1\n")
    core.commit("new module")
    assert verify(world) == []


def test_a_module_without_a_header_is_a_problem(world):
    core, *_ = world
    core.write("src/expcore/bare.py", "x = 1\n")
    core.commit("bare")
    assert verify(world) == ["src/expcore/bare.py: has no provenance header"]


def test_an_import_commit_that_differs_from_the_sanitised_blob_is_reported(make_repo, map_file, tmp_path):
    origin = make_repo("origin")
    origin.write(SOURCE_PATH, SOURCE)
    source_commit = origin.commit("source")
    core = make_repo("core")
    (core.path / "src").mkdir()
    staged = tmp_path / "staged.py"
    staged.write_text(SOURCE, encoding="utf-8")
    sn.import_module(map_file, "origin-repo", source_commit, SOURCE_PATH, staged, core.path / "src" / "tools.py")
    tampered = (core.path / "src" / "tools.py").read_text(encoding="utf-8").replace("space-a", "space-b")
    (core.path / "src" / "tools.py").write_text(tampered, encoding="utf-8")
    core.commit("import, hand edited")
    problems = hist.verify_history(core.path, {"origin-repo": origin.path}, load_map(map_file))
    assert len(problems) == 1
    assert "differs from the sanitised source blob" in problems[0]


def test_a_header_blob_that_is_not_the_named_commits_blob_is_reported(make_repo, map_file):
    origin = make_repo("origin")
    origin.write(SOURCE_PATH, SOURCE)
    source_commit = origin.commit("source")
    core = make_repo("core")
    wrong = format_import_header("origin-repo", source_commit, SOURCE_PATH, blob_id(b"another file"), "0" * 64)
    core.write("src/tools.py", insert_header("x = 1\n", wrong))
    core.commit("import")
    problems = hist.verify_history(core.path, {"origin-repo": origin.path}, load_map(map_file))
    assert problems == ["src/tools.py: header blob is not the blob of the named source commit and path"]


def test_an_unknown_source_repository_is_reported_by_the_module_never_by_its_name(world):
    """P07: the source repository a header names is private; the problem names the module, which is public."""
    core, _, _, _, token_map = world
    problems = hist.verify_history(core.path, {}, token_map)
    assert problems == [f"{MODULE}: the source repository its header names is not among the ones provided"]


def test_a_source_blob_git_cannot_read_is_reported_without_the_source_repositorys_path(world):
    """P07: git's own message names the source repository's path; the problem names the module instead."""
    core, origin, _, _, token_map = world
    blob = origin.git("rev-parse", f"HEAD:{SOURCE_PATH}")
    (origin.path / ".git" / "objects" / blob[:2] / blob[2:]).unlink()  # the tree still names it; the blob is gone
    problems = hist.verify_history(core.path, {"origin-repo": origin.path}, token_map)
    assert problems == [f"{MODULE}: the source blob cannot be read from the source repository its header names"]


def test_an_import_made_with_another_map_is_reported(world, tmp_path):
    core, origin, *_ = world
    other = tmp_path / "other_map.toml"
    other.write_text(MAP_TEXT + '\n[[entry]]\ntoken = "unrelated-name"\nreplacement = "other-name"\ncategory = "x"\n')
    problems = hist.verify_history(core.path, {"origin-repo": origin.path}, load_map(other))
    assert len(problems) == 1
    assert "different sanitising map" in problems[0]


def test_a_header_edited_after_the_import_to_name_another_blob_is_reported(world):
    core, origin, *_ = world
    origin.write(SOURCE_PATH, SOURCE + "\nOTHER = 1\n")
    later = origin.commit("changed source")
    new_blob = origin.git("rev-parse", f"{later}:{SOURCE_PATH}")
    old = (core.path / MODULE).read_text(encoding="utf-8")
    header = format_import_header("origin-repo", later, SOURCE_PATH, new_blob, load_map_sha(world))
    core.write(MODULE, insert_header(old.split("\n", 3)[3], header))
    core.commit("header now names the newer blob")
    problems = verify(world)
    assert len(problems) == 1
    assert "does not carry the source header that is current now" in problems[0]


def load_map_sha(world) -> str:
    return world[4].sha256


# --- the command line -----------------------------------------------------------------------------------------------


def test_the_command_line_checks_every_module_and_exits_0_when_all_hold(world, map_file, capsys, monkeypatch):
    """P07: `python -m expcore.provenance.history` runs the check on a repository and says it holds."""
    core, origin, *_ = world
    monkeypatch.delenv("EXPCORE_SOURCE_REPOS", raising=False)
    argv = ["--map", str(map_file), "--source-repos", f"origin-repo={origin.path}", "--repo", str(core.path)]
    assert hist.main(argv) == 0
    assert capsys.readouterr().out == "ok: every module under src holds (2 modules, 1 imported)\n"


def test_the_command_line_lists_each_problem_and_exits_1(world, map_file, capsys, monkeypatch):
    """P07: the command prints every problem and exits 1; the source repositories may come from the environment."""
    core, origin, *_ = world
    core.write("src/expcore/bare.py", "x = 1\n")
    core.commit("bare")
    monkeypatch.setenv("EXPCORE_SOURCE_REPOS", f"origin-repo={origin.path}")
    monkeypatch.chdir(core.path)
    assert hist.main(["--map", str(map_file)]) == 1
    assert capsys.readouterr().out == "FAIL src/expcore/bare.py: has no provenance header\n"


@pytest.mark.parametrize("repos", [None, "origin-repo"])
def test_the_command_line_refuses_missing_or_malformed_source_repositories(world, map_file, capsys, monkeypatch, repos):
    core, *_ = world
    monkeypatch.delenv("EXPCORE_SOURCE_REPOS", raising=False)
    argv = ["--map", str(map_file), "--repo", str(core.path)] + ([] if repos is None else ["--source-repos", repos])
    assert hist.main(argv) == 2
    assert "error:" in capsys.readouterr().err


def test_the_command_line_refuses_a_map_that_does_not_load(world, tmp_path, capsys):
    core, origin, *_ = world
    bad = tmp_path / "bad.toml"
    bad.write_text("version = 2\n", encoding="utf-8")
    argv = ["--map", str(bad), "--source-repos", f"origin-repo={origin.path}", "--repo", str(core.path)]
    assert hist.main(argv) == 2
    assert "expected 'version = 1'" in capsys.readouterr().err


def test_the_command_line_exits_nonzero_when_there_is_no_module_to_check(world, map_file, capsys):
    """P07: a run that checked no module proves nothing, so it is an error, not a pass."""
    core, origin, *_ = world
    argv = ["--map", str(map_file), "--source-repos", f"origin-repo={origin.path}", "--repo", str(core.path)]
    assert hist.main([*argv, "--root", "lib"]) == 2
    assert "no Python module under lib" in capsys.readouterr().err


def test_the_command_line_never_prints_a_name_the_map_hides(world, map_file, capsys):
    """P07: every line of the command goes through the redactor of the map's tokens: a module whose provenance header
    is unreadable is reported without the header's text, which holds a mapped name."""
    core, origin, *_ = world
    core.write("src/expcore/odd.py", "# Provenance: secret-space\nx = 1\n")
    core.commit("odd header")
    argv = ["--map", str(map_file), "--source-repos", f"origin-repo={origin.path}", "--repo", str(core.path)]
    assert hist.main(argv) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL src/expcore/odd.py: unreadable provenance header")
    assert "secret-space" not in out


# --- merges -----------------------------------------------------------------------------------------------------------


def merge_with(core, change) -> None:
    """Merge a side branch into the current branch, letting `change` alter the tree before the merge commit."""
    core.git("checkout", "-q", "-b", "side")
    core.write("side.txt", "side\n")
    core.commit("side")
    core.git("checkout", "-q", "-")
    core.write("main.txt", "main\n")
    core.commit("main")
    core.git("merge", "-q", "--no-ff", "--no-commit", "side")
    change()
    core.commit("merge side")


def test_an_import_first_added_in_a_merge_commit_is_traced_to_that_merge(make_repo, map_file, tmp_path):
    """P07: a merge is compared with its first parent, so a module the merge commit itself adds has an adding commit:
    the merge, whose file is checked like any import commit's."""
    origin = make_repo("origin")
    origin.write(SOURCE_PATH, SOURCE)
    source_commit = origin.commit("source")
    core = make_repo("core")
    core.write("src/expcore/__init__.py", "# Provenance: new\n")
    core.commit("skeleton")
    staged = tmp_path / "staged.py"
    staged.write_text(SOURCE, encoding="utf-8")
    merge_with(
        core, lambda: sn.import_module(map_file, "origin-repo", source_commit, SOURCE_PATH, staged, core.path / MODULE)
    )
    assert hist.verify_history(core.path, {"origin-repo": origin.path}, load_map(map_file)) == []


def test_a_module_deleted_and_added_back_edited_in_a_merge_is_checked_at_the_merge(world):
    """P07: a merge that adds back, edited, a module deleted on its first parent starts the module's history there; the
    old clean import is no evidence for it."""
    core, *_ = world
    before = (core.path / MODULE).read_text(encoding="utf-8")
    core.git("rm", "--quiet", MODULE)
    core.commit("drop the module")
    merge_with(core, lambda: core.write(MODULE, before.replace('"space-a"', '"space-b"')))
    problems = verify(world)
    assert len(problems) == 1
    assert "differs from the sanitised source blob" in problems[0]


# --- the real check, where the source repositories and the private map are present ----------------------------------


def test_every_imported_module_reproduces_from_its_source():
    """P07: every imported module of this tree reproduces from the source blob its header names (maintainer check)."""
    spec = os.environ.get("EXPCORE_SOURCE_REPOS")
    tokens = os.environ.get("EXPCORE_STUDY_TOKENS")
    if not spec or not tokens:
        pytest.skip("P07 maintainer check: set EXPCORE_SOURCE_REPOS (name=path;...) and EXPCORE_STUDY_TOKENS")
    token_map = load_map(Path(tokens).parent / "sanitise_map.toml")
    problems = hist.verify_history(ROOT, hist.parse_source_repos(spec), token_map)
    assert not problems, "imported modules that do not reproduce:\n" + "\n".join(problems)
