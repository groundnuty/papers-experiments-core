"""The repository's own source holds no study token, judged by the committed hash file."""

import os
from pathlib import Path

import pytest

from expcore.provenance.study_tokens import load_hashes, parse_plain_list, render_hash_file, scan_paths

ROOT = Path(__file__).resolve().parents[1]
HASH_FILE = ROOT / "tests" / "data" / "study_tokens.sha256"
ABSENT = "tests/data/study_tokens.sha256 is absent; generate it from the private token list to enable this check"


def test_src_contains_no_study_tokens(monkeypatch):
    if not HASH_FILE.is_file():
        pytest.skip(ABSENT)
    monkeypatch.chdir(ROOT)
    findings = scan_paths([Path("src")], load_hashes(HASH_FILE))
    assert not findings, "study-token findings (hash prefixes only):\n" + "\n".join(map(str, findings))


def test_committed_hash_file_matches_the_private_list():
    plain = os.environ.get("EXPCORE_STUDY_TOKENS")
    if not plain or not HASH_FILE.is_file():
        pytest.skip("EXPCORE_STUDY_TOKENS is unset or the hash file is absent")
    expected = render_hash_file(parse_plain_list(Path(plain).read_text(encoding="utf-8")))
    assert HASH_FILE.read_text(encoding="utf-8") == expected, "tests/data/study_tokens.sha256 is stale; regenerate it"
