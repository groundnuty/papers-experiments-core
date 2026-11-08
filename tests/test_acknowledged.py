"""Acknowledged findings: accepted one by one in pushed history, never in the working tree. Tokens are invented."""

import pytest

from expcore.provenance import acknowledged as ack
from expcore.provenance import study_tokens as st
from expcore.provenance.scanner import REDACTED_PATH, Finding

NAME = "secret-space"
PREFIX = st.token_hash(NAME)[:12]


@pytest.fixture
def hash_file(tmp_path):
    path = tmp_path / "hashes.sha256"
    path.write_text(st.render_hash_file([NAME]), encoding="utf-8")
    return path


@pytest.fixture
def history(repo):
    """A leaking commit under a clean one, so the leak is historical: returns (repo, leaking sha, tip sha)."""
    repo.write("src/ok.py", "x = 1\n")
    repo.commit("clean root")
    repo.write("src/leak.py", f'name = "{NAME}"\n')
    leak = repo.commit("leak")
    (repo.path / "src" / "leak.py").unlink()
    tip = repo.commit("fix")
    return repo, leak, tip


def write_ack(repo, *lines: str):
    path = repo.path / "tests" / "data" / "study_tokens.acknowledged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(history, hash_file, *extra: str) -> int:
    repo, leak, tip = history
    base = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path)]
    return st.main([*base, *extra, f"{leak}~1..{tip}"])


def test_an_exact_entry_suppresses_the_finding_and_is_counted(history, hash_file, capsys):
    repo, leak, _ = history
    write_ack(repo, "# an early fixture", f"{leak} src/leak.py:1 {PREFIX}", "")
    assert run(history, hash_file, "--acknowledged", str(repo.path / "tests/data/study_tokens.acknowledged")) == 0
    captured = capsys.readouterr()
    assert "1 finding(s) acknowledged" in captured.err
    assert captured.out == ""


def test_the_default_file_is_read_from_the_working_directory(history, hash_file, monkeypatch, capsys):
    repo, leak, _ = history
    write_ack(repo, f"{leak} src/leak.py:1 {PREFIX}")
    monkeypatch.chdir(repo.path)
    assert run(history, hash_file) == 0
    assert "acknowledged" in capsys.readouterr().err


def test_without_a_file_nothing_is_acknowledged(history, hash_file, monkeypatch):
    monkeypatch.chdir(history[0].path)
    assert run(history, hash_file) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sha: f"{'0' * 40} src/leak.py:1 {PREFIX}",  # another commit
        lambda sha: f"{sha} src/other.py:1 {PREFIX}",  # another path
        lambda sha: f"{sha} src/leak.py:2 {PREFIX}",  # another line
        lambda sha: f"{sha} src/leak.py:1 {'0' * 12}",  # another hash
        lambda sha: f"{sha[:39]}{'0' if sha[39] != '0' else '1'} src/leak.py:1 {PREFIX}",  # one character off
    ],
)
def test_an_entry_that_does_not_match_exactly_suppresses_nothing(history, hash_file, mutate, capsys):
    repo, leak, _ = history
    path = write_ack(repo, mutate(leak))
    assert run(history, hash_file, "--acknowledged", str(path)) == 1
    captured = capsys.readouterr()
    assert f"{leak[:12]} src/leak.py:1 string {PREFIX}" in captured.out
    assert "acknowledged" not in captured.err.replace("acknowledged:", "")


def test_only_the_acknowledged_finding_is_suppressed(repo, hash_file, capsys):
    repo.write("src/a.py", f'a = "{NAME}"\n')
    repo.write("src/b.py", f'b = "{NAME}"\n')
    leak = repo.commit("two leaks")
    repo.write("src/a.py", f'a = "{NAME}"\nz = 2\n')  # the same finding again, in a later commit
    later = repo.commit("later commit touching a.py")
    repo.write("src/ok.py", "x = 1\n")
    repo.commit("tip")
    path = write_ack(repo, f"{leak} src/a.py:1 {PREFIX}")
    args = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path), "--acknowledged", str(path)]
    assert st.main([*args, leak, later]) == 1
    out = capsys.readouterr().out
    assert "src/b.py:1" in out
    assert f"{later[:12]} src/a.py:1" in out  # the entry names one commit, not every commit with that finding
    assert f"{leak[:12]} src/a.py" not in out


def test_the_working_tree_check_ignores_the_file(repo, hash_file, monkeypatch, capsys):
    repo.write("src/leak.py", f'name = "{NAME}"\n')
    sha = repo.commit("leak")
    write_ack(repo, f"{sha} src/leak.py:1 {PREFIX}")
    monkeypatch.chdir(repo.path)
    assert st.main(["check", "--hashes", str(hash_file), "src"]) == 1
    assert "src/leak.py:1 string" in capsys.readouterr().out
    assert st.main(["check", "--hashes", str(hash_file)]) == 1


def test_an_entry_naming_the_current_commit_is_refused(repo, hash_file, capsys):
    repo.write("src/leak.py", f'name = "{NAME}"\n')
    head = repo.commit("leak at the tip")
    path = write_ack(repo, "# header", f"{head} src/leak.py:1 {PREFIX}")
    args = ["check-commits", "--hashes", str(hash_file), "--repo", str(repo.path), "--acknowledged", str(path), head]
    code = st.main(args)
    assert code == 2
    assert "study_tokens.acknowledged:2: names the current commit" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    [
        "abc123 src/leak.py:1 " + "0" * 12,  # a short commit prefix
        "0" * 12 + " src/leak.py:1 " + "0" * 12,  # a 12-character prefix is not a full commit id
        "0" * 40 + " src/leak.py " + "0" * 12,  # no line number
        "0" * 40 + " src/leak.py:x " + "0" * 12,
        "0" * 40 + " src/leak.py:1 " + "0" * 11,  # hash prefix shorter than 12
        "0" * 40 + " src/leak.py:1",  # no hash prefix
        "0" * 40 + " src/leak.py:1 " + "0" * 12 + " extra",
        "G" * 40 + " src/leak.py:1 " + "0" * 12,
    ],
)
def test_a_malformed_line_fails_naming_its_line_number(history, hash_file, line, capsys):
    repo, *_ = history
    path = write_ack(repo, "# comment", "", line)
    assert run(history, hash_file, "--acknowledged", str(path)) == 2
    assert "study_tokens.acknowledged:3: expected" in capsys.readouterr().err


def test_a_named_file_that_is_missing_is_an_error(history, hash_file, tmp_path, capsys):
    assert run(history, hash_file, "--acknowledged", str(tmp_path / "gone")) == 2
    assert "error:" in capsys.readouterr().err


def test_parse_and_split_helpers():
    entries = ack.parse_acknowledgements(f"# c\n\n{'a' * 40} p/q.py:7 {'b' * 12}\n", "f")
    assert [(e.commit, e.path, e.line, e.hash_prefix, e.source_line) for e in entries] == [
        ("a" * 40, "p/q.py", 7, "b" * 12, 3)
    ]
    finding = st.Finding("p/q.py", 7, "string", "b" * 12)
    other = st.Finding("p/q.py", 8, "string", "b" * 12)
    results = [("a" * 40, finding), ("a" * 40, other)]
    assert ack.split_acknowledged(results, entries) == ([("a" * 40, other)], [("a" * 40, finding)])
    assert ack.split_acknowledged(results, []) == (results, [])


def test_the_committed_file_lists_the_five_historical_findings():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tests" / "data" / "study_tokens.acknowledged"
    entries = ack.parse_acknowledgements(path.read_text(encoding="utf-8"), str(path))
    assert [(e.commit, e.path, e.line) for e in entries] == [
        ("cce4dec3b00d666f375eab59a5d8686ec378566e", "tests/test_check_commits.py", 11),
        ("cce4dec3b00d666f375eab59a5d8686ec378566e", "tests/test_study_tokens.py", 15),
        ("cce4dec3b00d666f375eab59a5d8686ec378566e", "tests/test_study_tokens.py", 106),
        ("cce4dec3b00d666f375eab59a5d8686ec378566e", "tests/test_study_tokens.py", 110),
        ("76390b346c47f9e435701467a9c3abe398b007d4", "tests/test_check_commits.py", 11),
    ]
    assert {e.hash_prefix for e in entries} == {"01d3ff1052a5"}


def test_a_redacted_path_cannot_be_acknowledged():
    with pytest.raises(ValueError, match="redacted path cannot be acknowledged"):
        ack.parse_acknowledgements(f"{'a' * 40} {REDACTED_PATH}:0 {'b' * 12}\n", "f")
    redacted = Finding(REDACTED_PATH, 0, "path", "b" * 12)
    entry = ack.Acknowledgement("a" * 40, REDACTED_PATH, 0, "b" * 12, 1)  # even if built directly
    assert not entry.matches("a" * 40, redacted)
