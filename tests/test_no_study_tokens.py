"""The repository holds no study token, judged by the committed hash file; the check never skips for a missing file."""

import os
from pathlib import Path

import pytest

from expcore.provenance.study_tokens import (
    load_hashes,
    parse_plain_entries,
    render_hash_file,
    repository_files,
    scan_paths,
)

ROOT = Path(__file__).resolve().parents[1]
HASH_FILE = ROOT / "tests" / "data" / "study_tokens.sha256"


def test_the_hash_file_is_committed_and_holds_hashes():
    assert HASH_FILE.is_file(), "tests/data/study_tokens.sha256 is missing; generate it from the private token list"
    assert load_hashes(HASH_FILE), "tests/data/study_tokens.sha256 holds no hashes"


def test_no_tracked_or_new_file_contains_a_study_token(monkeypatch):
    monkeypatch.chdir(ROOT)
    findings = scan_paths(repository_files(Path()), load_hashes(HASH_FILE))
    assert not findings, "study-token findings (hash prefixes only):\n" + "\n".join(map(str, findings))


def test_committed_hash_file_matches_the_private_list():
    plain = os.environ.get("EXPCORE_STUDY_TOKENS")
    if not plain:
        pytest.skip("EXPCORE_STUDY_TOKENS is unset, so the private list is not available to compare with")
    entries = parse_plain_entries(Path(plain).read_text(encoding="utf-8"))
    expected = render_hash_file((entry.token, entry.scope) for entry in entries)
    assert HASH_FILE.read_text(encoding="utf-8") == expected, "tests/data/study_tokens.sha256 is stale; regenerate it"
