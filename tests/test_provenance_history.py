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


@pytest.mark.parametrize("spec", ["a", "a=", "=/x", "a=/x;b"])
def test_parse_source_repos_rejects_malformed_pairs(spec):
    with pytest.raises(ValueError, match="name=path"):
        hist.parse_source_repos(spec)


# --- the check ------------------------------------------------------------------------------------------------------


def test_a_fresh_import_reproduces(world):
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


def test_an_unknown_source_repository_is_reported(world):
    core, _, _, _, token_map = world
    problems = hist.verify_history(core.path, {}, token_map)
    assert len(problems) == 1
    assert "'origin-repo' is not among the repositories provided" in problems[0]


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


# --- the real check, where the source repositories and the private map are present ----------------------------------


def test_every_imported_module_reproduces_from_its_source():
    spec = os.environ.get("EXPCORE_SOURCE_REPOS")
    tokens = os.environ.get("EXPCORE_STUDY_TOKENS")
    if not spec or not tokens:
        pytest.skip("P07 maintainer check: set EXPCORE_SOURCE_REPOS (name=path;...) and EXPCORE_STUDY_TOKENS")
    token_map = load_map(Path(tokens).parent / "sanitise_map.toml")
    problems = hist.verify_history(ROOT, hist.parse_source_repos(spec), token_map)
    assert not problems, "imported modules that do not reproduce:\n" + "\n".join(problems)
