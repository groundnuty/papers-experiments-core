"""check-commits: every commit of a range is scanned at its own content. Tokens here are all invented."""

from pathlib import Path

import pytest

from expcore.provenance import study_tokens as st

LONG_HOST = "fake-zone.example.net"
LONG_NAME = "secret-space"
# The host is an invented name for code (not checked in docs/); the space name is checked everywhere.
TOKENS = [(LONG_HOST, "code"), (LONG_NAME, "all"), ("zq", "all")]


@pytest.fixture
def hash_file(tmp_path: Path) -> Path:
    path = tmp_path / "hashes.sha256"
    path.write_text(st.render_hash_file(TOKENS), encoding="utf-8")
    return path


@pytest.fixture
def history(repo):
    """Three commits: a clean root, one that leaks (in src/ and docs/), one that removes the leak from src/."""
    repo.write("src/ok.py", "x = 1\n")
    first = repo.commit("clean root")
    repo.write("src/leak.py", f'URL = "https://{LONG_HOST}/"\n')
    repo.write("docs/note.md", f"{LONG_NAME} runs on {LONG_HOST}\n")
    repo.write("examples/demo.py", "y = 2\n")
    second = repo.commit("leak")
    (repo.path / "src" / "leak.py").unlink()
    third = repo.commit("tip is clean again")
    return repo, first, second, third


def test_check_commits_finds_a_leak_that_a_later_commit_removed(history, hash_file, capsys):
    repo, first, second, third = history
    base = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path)]
    assert st.main([*base, f"{first}..{third}"]) == 1
    out = capsys.readouterr()
    assert f"{second[:12]} src/leak.py:1 string {st.token_hash(LONG_HOST)[:12]}" in out.out
    assert LONG_HOST not in out.out + out.err
    assert st.main([*base, third]) == 0
    assert st.main([*base, first]) == 0
    assert st.main([*base, second]) == 1


def test_docs_are_checked_against_all_scope_tokens_only(history, hash_file, capsys):
    repo, first, _, third = history
    base = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path)]
    assert st.main([*base, f"{first}..{third}"]) == 1
    lines = capsys.readouterr().out.splitlines()
    docs = [line for line in lines if " docs/note.md:" in line]
    assert docs == [f"{history[2][:12]} docs/note.md:1 text {st.token_hash(LONG_NAME)[:12]}"]
    assert not any(st.token_hash(LONG_HOST)[:12] in line for line in docs)


def test_every_text_file_is_checked_not_only_src_and_examples(repo, hash_file):
    for name in ("README.md", "tests/test_x.py", "pyproject.toml", ".github/workflows/ci.yml", "scripts/run"):
        repo.write(name, f"# {LONG_NAME}\n")
    sha = repo.commit("leaks everywhere")
    found = {f.path for _, f in st.check_commits(repo.path, [sha], st.load_hashes(hash_file))}
    assert found == {"README.md", "tests/test_x.py", "pyproject.toml", ".github/workflows/ci.yml", "scripts/run"}


def test_the_hash_file_itself_is_never_scanned(repo, hash_file):
    repo.write("tests/data/study_tokens.sha256", f"# {LONG_NAME}\n")
    sha = repo.commit("hash file")
    assert st.check_commits(repo.path, [sha], st.load_hashes(hash_file)) == []


def test_a_binary_file_is_skipped(repo, hash_file):
    (repo.path / "blob.bin").write_bytes(b"\0" + LONG_NAME.encode())
    sha = repo.commit("binary")
    assert st.check_commits(repo.path, [sha], st.load_hashes(hash_file)) == []


def test_check_commits_reads_each_commit_at_its_own_content(repo, hash_file):
    repo.write("src/m.py", f'x = "{LONG_NAME}"\n')
    leaking = repo.commit("leak")
    repo.write("src/m.py", "x = 1\n")
    fixed = repo.commit("fix")
    results = st.check_commits(repo.path, [fixed], st.load_hashes(hash_file))
    assert results == []
    results = st.check_commits(repo.path, [f"{leaking}~0..{fixed}", leaking], st.load_hashes(hash_file))
    assert {sha for sha, _ in results} == {leaking}


def test_check_commits_covers_a_root_commit_and_examples(repo, hash_file):
    repo.write("examples/demo.py", f"# {LONG_NAME}\n")
    root = repo.commit("root with a leak")
    findings = st.check_commits(repo.path, [root], st.load_hashes(hash_file))
    assert [(sha, f.path) for sha, f in findings] == [(root, "examples/demo.py")]


def test_check_commits_accepts_several_revisions_and_ignores_duplicates(history, hash_file):
    repo, first, second, third = history
    hashes = st.load_hashes(hash_file)
    results = st.check_commits(repo.path, [second, f"{first}..{third}", second], hashes)
    assert {(sha, f.path) for sha, f in results} == {(second, "src/leak.py"), (second, "docs/note.md")}
    assert len(results) == 2


def test_check_commits_checks_a_merge_commit_resolution(repo, hash_file):
    repo.write("src/m.py", "x = 1\n")
    base = repo.commit("base")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("src/side.py", "z = 3\n")
    repo.commit("side")
    repo.git("checkout", "--quiet", base)
    repo.write("src/main.py", "w = 4\n")
    repo.commit("main")
    repo.git("-c", "commit.gpgsign=false", "merge", "--quiet", "--no-ff", "--no-commit", "side")
    repo.write("src/m.py", f'x = "{LONG_NAME}"\n')
    merge = repo.commit("merge with a leak in the resolution")
    results = st.check_commits(repo.path, [merge], st.load_hashes(hash_file))
    assert [f.path for _, f in results] == ["src/m.py"]


def test_check_commits_without_a_hash_file_exits_2_unless_allowed(history, monkeypatch, capsys):
    repo, first, _, third = history
    monkeypatch.chdir(repo.path)
    assert st.main(["check-commits", f"{first}..{third}"]) == 2
    assert "not found" in capsys.readouterr().err
    assert st.main(["check-commits", "--allow-missing-hashes", f"{first}..{third}"]) == 0
    assert "skipped" in capsys.readouterr().err


def test_check_commits_rejects_option_like_and_unknown_revisions(history, hash_file, capsys):
    repo, *_ = history
    base = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path)]
    assert st.main([*base, "nonexistent-rev"]) == 2
    assert st.main([*base, "--", "--all"]) == 2
    assert capsys.readouterr().err.count("error:") == 2


def test_history_flag_makes_a_bare_revision_stand_for_its_whole_ancestry(history, hash_file):
    repo, first, _, third = history
    base = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path)]
    assert st.main([*base, third]) == 0
    assert st.main([*base, "--history", third]) == 1
    assert st.main([*base, "--history", first]) == 0
