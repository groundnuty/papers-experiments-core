"""The code stamp: P01 (unknown is dirty), P02 (data files are part of the identity), P03 (no mtimes, no caches)."""

import json
import os

import pytest

from expcore.provenance.codestamp import (
    STAMP,
    UNKNOWN,
    CodeScope,
    CodeScopeError,
    CodeVersionUnknown,
    hashed_root_of,
    read_stamp,
    require_known,
    tree_hash,
    verify_identity,
    version,
    write_stamp,
)

SCOPE = CodeScope.of(["pkg", "data", "config.toml"], not_hashed=["data/AMENDMENTS.md"])
SHA = "a" * 40


def populate(root):
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "run.py").write_text("print('run')\n", encoding="utf-8")
    (root / "data" / "table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "data" / "AMENDMENTS.md").write_text("one row\n", encoding="utf-8")
    (root / "config.toml").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A plain directory of code and data that git cannot find a repository for."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return populate(tmp_path / "tree")


@pytest.fixture
def checkout(repo):
    """A git checkout of the same files with one commit."""
    populate(repo.path)
    repo.commit("code")
    return repo


def test_p03_touching_a_file_leaves_the_hash_unchanged(tree):
    """P03: modification times play no part."""
    before = tree_hash(tree, SCOPE)
    for name in ("pkg/run.py", "data/table.csv"):
        os.utime(tree / name, (1, 1))
    assert tree_hash(tree, SCOPE) == before


def test_p03_caches_and_bytecode_leave_the_hash_unchanged(tree):
    """P03: __pycache__, .pytest_cache, a virtual environment and .pyc files are not code."""
    before = tree_hash(tree, SCOPE)
    for name in (
        "pkg/__pycache__/run.cpython-312.pyc",
        "pkg/.pytest_cache/v/cache",
        "pkg/.venv/lib/x.py",
        "pkg/stale.pyc",
    ):
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_bytes(b"cache")
    assert tree_hash(tree, SCOPE) == before


def test_p03_the_hash_depends_on_content_path_and_set_of_files_only(tree):
    """P03: The hash depends on content path and set of files only."""
    before = tree_hash(tree, SCOPE)
    (tree / "pkg" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    added = tree_hash(tree, SCOPE)
    assert added != before
    (tree / "pkg" / "extra.py").rename(tree / "pkg" / "other.py")
    assert tree_hash(tree, SCOPE) not in (before, added)
    (tree / "pkg" / "other.py").unlink()
    assert tree_hash(tree, SCOPE) == before


def test_p02_editing_a_data_file_under_a_code_root_changes_the_identity(tree):
    """P02: the files the code reads are part of what a run is."""
    before = tree_hash(tree, SCOPE)
    (tree / "data" / "table.csv").write_text("a,b\n1,3\n", encoding="utf-8")
    assert tree_hash(tree, SCOPE) != before


def test_p02_a_single_file_root_counts_and_a_file_outside_the_roots_does_not(tree):
    """P02: A single file root counts and a file outside the roots does not."""
    before = tree_hash(tree, SCOPE)
    (tree / "notes.txt").write_text("outside\n", encoding="utf-8")
    assert tree_hash(tree, SCOPE) == before
    (tree / "config.toml").write_text("x = 2\n", encoding="utf-8")
    assert tree_hash(tree, SCOPE) != before


def test_the_exclusions_are_a_parameter_a_record_file_may_change_without_changing_the_identity(tree):
    before = tree_hash(tree, SCOPE)
    (tree / "data" / "AMENDMENTS.md").write_text("two rows\n", encoding="utf-8")
    assert tree_hash(tree, SCOPE) == before
    strict = CodeScope.of(SCOPE.roots)
    assert tree_hash(tree, strict) != tree_hash(tree, SCOPE)


def test_p02_a_scope_that_covers_no_file_is_refused_because_it_would_pass_for_any_code(tree):
    """P02: A scope that covers no file is refused because it would pass for any code."""
    with pytest.raises(CodeScopeError, match="would cover no code"):
        tree_hash(tree, CodeScope.of(["nowhere"]))


@pytest.mark.parametrize("bad", ["/etc", "../outside", "a/../../b", ""])
def test_a_root_that_is_absolute_or_climbs_out_of_the_tree_is_refused(bad):
    with pytest.raises(CodeScopeError, match="relative path inside the tree"):
        CodeScope.of([bad])


def test_a_scope_needs_a_root():
    with pytest.raises(CodeScopeError, match="at least one root"):
        CodeScope.of([])


def test_p01_a_tree_with_neither_git_nor_stamp_is_unknown_and_dirty_and_refuses_start(tree):
    """P01: unknown code is dirty code."""
    found = version(tree, SCOPE)
    assert (found["commit"], found["dirty"], found["source"]) == (UNKNOWN, True, "none")
    assert found["tree_sha256"] is None
    assert STAMP in found["why"]
    for allow in (False, True):
        with pytest.raises(CodeVersionUnknown, match="study run: refusing to start: the code version is unknown"):
            require_known("study run", tree, SCOPE, allow_dirty=allow)


def test_p01_the_refusal_is_an_ordinary_exception_not_an_exit_of_the_interpreter(tree):
    """P01: The refusal is an ordinary exception not an exit of the interpreter."""
    with pytest.raises(RuntimeError):
        require_known("study run", tree, SCOPE)
    assert not issubclass(CodeVersionUnknown, SystemExit)


def test_p01_an_unreadable_stamp_is_unknown(tree):
    """P01: An unreadable stamp is unknown."""
    (tree / STAMP).write_text("{not json", encoding="utf-8")
    assert read_stamp(tree) is None
    assert version(tree, SCOPE)["commit"] == UNKNOWN
    (tree / STAMP).write_text(json.dumps({"head": SHA}), encoding="utf-8")  # no tree hash
    assert read_stamp(tree) is None


@pytest.mark.parametrize("head", ["", "not-a-sha", "abcdef1", "A" * 40, "a" * 39, "a" * 41, 12345, None])
def test_p01_a_stamp_whose_head_is_not_a_full_sha_is_unknown_code_that_refuses_a_study_run(tree, head):
    """P01: An empty or non-sha head names no code; the stamp is not read and the version is unknown."""
    stamp = {"head": head, "tree_sha256": tree_hash(tree, SCOPE)}
    (tree / STAMP).write_text(json.dumps(stamp), encoding="utf-8")
    assert read_stamp(tree) is None
    found = version(tree, SCOPE)
    assert (found["commit"], found["dirty"], found["source"]) == (UNKNOWN, True, "none")
    for allow in (False, True):
        with pytest.raises(CodeVersionUnknown, match="the code version is unknown"):
            require_known("study run", tree, SCOPE, allow_dirty=allow)


def test_p01_a_clean_checkout_names_its_head_and_is_not_dirty(checkout):
    """P01: A clean checkout names its head and is not dirty."""
    found = version(checkout.path, SCOPE)
    assert (found["commit"], found["dirty"], found["source"], found["why"]) == (
        checkout.git("rev-parse", "HEAD"),
        False,
        "git",
        None,
    )
    assert found["tree_sha256"] == tree_hash(checkout.path, SCOPE)
    assert require_known("study run", checkout.path, SCOPE) == found


def test_p01_uncommitted_changes_are_dirty_and_do_not_start_a_study_run_unless_allowed(checkout):
    """P01: Uncommitted changes are dirty and do not start a study run unless allowed."""
    (checkout.path / "pkg" / "run.py").write_text("print('changed')\n", encoding="utf-8")
    found = version(checkout.path, SCOPE)
    assert (found["dirty"], found["why"]) == (True, "uncommitted changes")
    with pytest.raises(CodeVersionUnknown, match="dirty code"):
        require_known("study run", checkout.path, SCOPE)
    assert require_known("pilot", checkout.path, SCOPE, allow_dirty=True)["dirty"] is True


def test_p01_an_untracked_file_under_a_root_is_dirty_because_the_tree_hash_covers_it(checkout):
    """P01: `git status --untracked-files=no` is silent about a new .py, but the tree hash includes it."""
    clean = version(checkout.path, SCOPE)
    (checkout.path / "pkg" / "new.py").write_text("print('new')\n", encoding="utf-8")
    found = version(checkout.path, SCOPE)
    assert found["tree_sha256"] != clean["tree_sha256"]
    assert (found["dirty"], found["source"]) == (True, "git")
    assert "hashed files that git does not track: pkg/new.py" in found["why"]
    with pytest.raises(CodeVersionUnknown, match="dirty code"):
        require_known("study run", checkout.path, SCOPE)
    assert require_known("pilot", checkout.path, SCOPE, allow_dirty=True)["dirty"] is True
    checkout.commit("add new")
    assert version(checkout.path, SCOPE)["dirty"] is False


def test_p01_a_file_outside_the_roots_or_not_hashed_or_a_cache_is_not_dirt(checkout):
    """P01: Only what the tree hash covers can make the identity differ from the commit."""
    scope = CodeScope.of(SCOPE.roots, not_hashed=[*SCOPE.not_hashed, "data/log.md"])
    for name in ("notes.txt", "other/scratch.py", "data/log.md", "pkg/__pycache__/run.cpython-312.pyc"):
        (checkout.path / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout.path / name).write_text("x\n", encoding="utf-8")
    found = version(checkout.path, scope)
    assert (found["dirty"], found["why"]) == (False, None)
    assert found["tree_sha256"] == tree_hash(checkout.path, scope)


def test_p01_a_gitignored_file_under_a_root_is_dirty_because_the_tree_hash_still_covers_it(checkout):
    """P01: A committed .gitignore naming a data file does not make the file part of the commit."""
    (checkout.path / ".gitignore").write_text("data/extra.csv\n", encoding="utf-8")
    checkout.commit("ignore a data file")
    clean = version(checkout.path, SCOPE)
    assert clean["dirty"] is False
    (checkout.path / "data" / "extra.csv").write_text("a\n", encoding="utf-8")
    assert checkout.git("status", "--porcelain", "--untracked-files=all") == ""  # git itself reports nothing
    found = version(checkout.path, SCOPE)
    assert found["tree_sha256"] != clean["tree_sha256"]
    assert (found["dirty"], found["why"]) == (True, "hashed files that git does not track: data/extra.csv")
    with pytest.raises(CodeVersionUnknown, match="dirty code"):
        require_known("study run", checkout.path, SCOPE)
    assert require_known("pilot", checkout.path, SCOPE, allow_dirty=True)["dirty"] is True
    (checkout.path / "data" / "extra.csv").unlink()
    assert version(checkout.path, SCOPE)["dirty"] is False


def test_p01_an_ignored_cache_or_not_hashed_file_under_a_root_stays_out_of_both_sides(checkout):
    """P01: The caches the tree hash skips are not in the hashed set, so ignoring them does not make them dirt."""
    scope = CodeScope.of(SCOPE.roots, not_hashed=[*SCOPE.not_hashed, "data/log.md"])
    (checkout.path / ".gitignore").write_text("__pycache__/\n*.pyc\ndata/log.md\n", encoding="utf-8")
    checkout.commit("ignore caches and a record")
    (checkout.path / "pkg" / "__pycache__").mkdir()
    (checkout.path / "pkg" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"cache")
    (checkout.path / "data" / "log.md").write_text("a dated record\n", encoding="utf-8")
    assert version(checkout.path, scope)["dirty"] is False


def test_p01_many_untracked_files_are_named_briefly(checkout):
    """P01: The reason names the first few untracked files and counts the rest."""
    for number in range(5):
        (checkout.path / "pkg" / f"new{number}.py").write_text("x = 1\n", encoding="utf-8")
    why = version(checkout.path, SCOPE)["why"]
    assert why == "hashed files that git does not track: pkg/new0.py, pkg/new1.py, pkg/new2.py and 2 more"


def test_p01_a_git_status_that_fails_is_dirty_never_clean(checkout):
    """P01: reading a failed `git status` as an empty one is how a dirty tree looked clean."""
    (checkout.path / ".git" / "index").write_bytes(b"not an index")
    assert checkout.git("rev-parse", "HEAD")  # git still names the head
    found = version(checkout.path, SCOPE)
    assert found["commit"] == checkout.git("rev-parse", "HEAD")
    assert (found["dirty"], found["why"]) == (True, "git status could not be read")
    with pytest.raises(CodeVersionUnknown, match="git status could not be read"):
        require_known("study run", checkout.path, SCOPE)


def test_p02_a_data_file_edited_in_a_checkout_changes_the_recorded_tree_hash_and_fails_verification(checkout):
    """P02: A data file edited in a checkout changes the recorded tree hash and fails verification."""
    before = version(checkout.path, SCOPE)
    commit, tree = before["commit"], before["tree_sha256"]
    assert verify_identity(checkout.path, SCOPE, commit, tree) == []
    (checkout.path / "data" / "table.csv").write_text("a,b\n9,9\n", encoding="utf-8")
    problems = verify_identity(checkout.path, SCOPE, commit, tree)
    assert problems == ["the hash of the code and data files differs from the one the record names"]
    checkout.commit("edit data")
    assert any("the code is at commit" in p for p in verify_identity(checkout.path, SCOPE, commit, tree))
    assert verify_identity(checkout.path, SCOPE, commit, None)


def test_a_stamp_stands_in_for_git_in_a_copied_tree_and_names_the_stamped_head(tree):
    stamp = write_stamp(tree, SHA, SCOPE)
    assert stamp["tree_sha256"] == tree_hash(tree, SCOPE)
    assert read_stamp(tree) == json.loads((tree / STAMP).read_text(encoding="utf-8"))
    found = version(tree, SCOPE)
    assert (found["commit"], found["dirty"], found["source"], found["stale"], found["why"]) == (
        SHA,
        False,
        "stamp",
        False,
        None,
    )
    assert require_known("study run", tree, SCOPE)["commit"] == SHA


def test_p02_a_stamp_is_re_verified_against_the_files_beside_it_and_a_data_edit_makes_it_stale(tree):
    """P02: A stamp is re verified against the files beside it and a data edit makes it stale."""
    write_stamp(tree, SHA, SCOPE)
    (tree / "data" / "table.csv").write_text("a,b\n1,4\n", encoding="utf-8")
    found = version(tree, SCOPE)
    assert (found["dirty"], found["stale"]) == (True, True)
    assert "differ from the stamped tree" in found["why"]
    for allow in (False, True):
        with pytest.raises(CodeVersionUnknown, match="the stamp is stale"):
            require_known("study run", tree, SCOPE, allow_dirty=allow)


def test_a_stamp_taken_from_a_dirty_source_is_dirty_but_not_stale(tree):
    write_stamp(tree, SHA, SCOPE, dirty=True)
    found = version(tree, SCOPE)
    assert (found["dirty"], found["stale"]) == (True, False)
    assert "uncommitted changes" in found["why"]
    with pytest.raises(CodeVersionUnknown, match="dirty code"):
        require_known("study run", tree, SCOPE)
    assert require_known("pilot", tree, SCOPE, allow_dirty=True)["commit"] == SHA


def test_a_stamp_can_cover_a_tree_other_than_where_it_is_written(tmp_path):
    source = populate(tmp_path / "source")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    write_stamp(snapshot, SHA, SCOPE, tree_root=source)
    assert read_stamp(snapshot)["tree_sha256"] == tree_hash(source, SCOPE)


@pytest.mark.parametrize("head", ["abc", "abcdef1", "G" * 40, "A" * 40, "a" * 39, "a" * 41, "a" * 64, ""])
def test_a_stamp_refuses_a_head_that_is_not_a_full_git_sha(tree, head):
    with pytest.raises(ValueError, match="not a git sha"):
        write_stamp(tree, head, SCOPE)
    assert not (tree / STAMP).exists()


def test_hashed_root_of_names_the_root_a_file_sits_in(tree):
    assert hashed_root_of(tree / "data" / "table.csv", tree, SCOPE) == "data"
    assert hashed_root_of(tree / "config.toml", tree, SCOPE) == "config.toml"
    assert hashed_root_of(tree / "notes.txt", tree, SCOPE) is None
    assert hashed_root_of("/somewhere/else.txt", tree, SCOPE) is None
