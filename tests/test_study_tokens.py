"""The study-token hashing, candidate extraction, scanner and command line. Tokens here are all invented."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.provenance import study_tokens as st

LONG_HOST = "fake-zone.example.net"
LONG_NAME = "secret-space"
LONG_IDENT = "secret_space"
SHORT = "de"
CIDR = "10.99.0.0/16"
TOKENS = [LONG_HOST, LONG_NAME, LONG_IDENT, SHORT, CIDR]
PROVENANCE = (
    "# Provenance:\n"
    f"#   source: {LONG_NAME} @ 0123abcd, pkg/mod.py, blob {'0' * 40}\n"
    f"#   import: sanitised with study-token map {'0' * 64}\n"
)


@pytest.fixture
def hashes() -> st.Hashes:
    return st.parse_hash_file(st.render_hash_file(TOKENS), "test")


@pytest.fixture
def hash_file(tmp_path: Path) -> Path:
    path = tmp_path / "hashes.sha256"
    path.write_text(st.render_hash_file(TOKENS), encoding="utf-8")
    return path


def kinds(findings: list[st.Finding]) -> set[tuple[int, str]]:
    return {(f.line, f.kind) for f in findings}


# --- normalising and hashing ---------------------------------------------------------------------------------------


def test_normalise_applies_nfkc_casefold_and_strip():
    assert st.normalise("  \uff33ecret-SPACE\n") == "secret-space"


def test_token_hash_is_sha256_of_the_versioned_prefix_and_the_normalised_token():
    expected = hashlib.sha256(b"expcore-study-token-v1:secret-space").hexdigest()
    assert st.token_hash("secret-space") == expected
    assert st.token_hash("  Secret-SPACE ") == expected


# --- candidates -----------------------------------------------------------------------------------------------------


def test_candidates_of_a_url_with_host_port_and_path():
    found = set(st.candidates("https://a.b.example.org:8443/x/study-space/in"))
    assert {"a.b.example.org", "b.example.org", "example.org", "a.b", "example"} <= found
    assert {"study-space", "study", "space"} <= found
    assert {"https", "8443", "x", "in"} <= found
    assert "example.org:8443" not in found
    assert "study-space/in" not in found


def test_candidates_of_a_composed_identifier():
    found = set(st.candidates("api_host_name"))
    assert {"api_host_name", "api_host", "host_name", "api", "host", "name"} == found


def test_candidates_keep_the_separators_they_had():
    found = set(st.candidates("a-b_c.d"))
    assert {"a-b_c.d", "a-b_c", "b_c.d", "a-b", "b_c", "c.d"} <= found
    assert "a_b" not in found


def test_candidates_of_a_path_assignment_and_query():
    found = set(st.candidates("/data/secret-space/in?token=fake-zone.example.net&x=1"))
    assert {"secret-space", "fake-zone.example.net", "token", "data"} <= found


def test_candidates_include_the_raw_run_when_it_starts_or_ends_with_a_separator():
    found = set(st.candidates("__init__ and _private_ ."))
    assert {"init", "__init__", "private", "_private_"} <= found


def test_candidates_use_nfkc_so_fullwidth_letters_do_not_hide_a_token():
    assert "secret-space" in set(st.candidates("\uff53ecret\uff0dspace"))


def test_candidate_windows_are_bounded():
    run = ".".join(["w"] * 40)
    lengths = {c.count(".") + 1 for c in st.candidates(run)}
    assert max(lengths) == st.MAX_COMPONENTS
    assert not any(len(c) > st.MAX_CANDIDATE_CHARS for c in st.candidates("a." * 400))


def test_a_token_after_a_very_long_run_is_still_found():
    assert LONG_NAME in set(st.candidates("x." * 300 + LONG_NAME))


# --- hash file ------------------------------------------------------------------------------------------------------


def test_hash_file_is_sorted_deduplicated_and_marks_short_tokens_whole():
    text = st.render_hash_file(["de", "DE ", "secret-space", "Secret-Space", "", "abc", "abcd"])
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines)) == 4
    assert f"whole {st.token_hash('de')}" in lines
    assert f"whole {st.token_hash('abc')}" in lines
    assert f"any {st.token_hash('abcd')}" in lines
    assert f"any {st.token_hash('secret-space')}" in lines


def test_hash_file_round_trips_through_the_parser():
    loaded = st.parse_hash_file(st.render_hash_file(TOKENS), "test")
    assert st.token_hash(SHORT) in loaded.whole
    assert st.token_hash(LONG_HOST) in loaded.anywhere
    assert loaded


@pytest.mark.parametrize("line", ["any nothex", "some " + "0" * 64, "any", "0" * 64])
def test_malformed_hash_lines_name_the_file_and_line(line):
    with pytest.raises(ValueError, match=r"hashes\.sha256:3:"):
        st.parse_hash_file(f"# comment\n\n{line}\n", "hashes.sha256")


def test_plain_list_ignores_comments_and_blank_lines():
    assert st.parse_plain_list("# c\n\n  one \ntwo\n") == ["one", "two"]


# --- scanning Python ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (f'url = "https://{LONG_HOST}:8443/api"\n', "string"),
        (f'path = "/data/{LONG_NAME}/in"\n', "string"),
        (f'name = "x-{LONG_NAME}-y"\n', "string"),
        (f"def get_{LONG_IDENT}_path():\n    pass\n", "name"),
        (f"{LONG_IDENT} = 1\n", "name"),
        (f"# see {LONG_NAME} for details\n", "comment"),
        (f'url = f"https://{{host}}.{LONG_HOST}/"\n', "string"),
        ('x = "\\x73ecret-space"\n', "string"),
        (f"x = 1  # {LONG_HOST}.\n", "comment"),
        (f'doc = """first\nsecond {LONG_NAME}\n"""\n', "string"),
        (f"{SHORT} = 1\n", "name"),
        (f'x = "{SHORT}"\n', "string"),
        (f"x = '  {SHORT} '\n", "string"),
        (f'net = "{CIDR}"\n', "string"),
        (f"b = b'{LONG_NAME}'\n", "string"),
    ],
)
def test_python_leaks_are_found(source, kind, hashes):
    findings = st.scan_source("mod.py", source, hashes)
    assert findings, source
    assert {f.kind for f in findings} == {kind}


@pytest.mark.parametrize(
    "source",
    [
        "# de-duplication of rows\n",
        'x = "de-duplicate"\n',
        'x = "idle"\n',
        "index = 1\n",
        "def de_dup(): pass\n",
        'x = "a de b"\n',
        'x = "secret space"\n',
        'x = "supersecret-spaces"\n',
        'x = "10.99.0.0"\n',
        'x = f"{index}-de-duplicate"\n',
        "x = 1\n",
    ],
)
def test_python_without_a_token_has_no_findings(source, hashes):
    assert st.scan_source("mod.py", source, hashes) == []


def test_short_tokens_match_only_a_whole_identifier_or_literal(hashes):
    assert st.scan_source("m.py", 'x = "de"\n', hashes)
    assert st.scan_source("m.py", "de = 1\n", hashes)
    assert not st.scan_source("m.py", '# de\nx = "de-de"\n', hashes)


def test_findings_carry_a_twelve_character_hash_prefix_and_never_the_text(hashes):
    source = f'url = "https://{LONG_HOST}/"\n# {LONG_NAME} {LONG_IDENT} {CIDR}\n'
    findings = st.scan_source("mod.py", source, hashes)
    assert findings
    for finding in findings:
        rendered = f"{finding} {finding!r}"
        assert len(finding.hash_prefix) == 12
        for token in TOKENS:
            assert token not in rendered
            assert token.replace("-", "_") not in rendered
    assert st.token_hash(LONG_HOST).startswith(findings[0].hash_prefix)


def test_finding_lines_point_inside_multi_line_literals(hashes):
    source = f'x = 1\ndoc = """first\nsecond\nthird {LONG_NAME}\n"""\n'
    assert kinds(st.scan_source("m.py", source, hashes)) == {(4, "string")}


def test_unterminated_python_is_still_scanned_as_text(hashes):
    source = f'x = ("unterminated\n# {LONG_NAME}\n'
    findings = st.scan_source("broken.py", source, hashes)
    assert any(f.kind == "text" for f in findings)


# --- the provenance header ------------------------------------------------------------------------------------------


def test_a_header_that_names_a_token_passes_but_the_same_token_in_the_body_fails(hashes):
    assert st.scan_source("m.py", PROVENANCE + "x = 1\n", hashes) == []
    findings = st.scan_source("m.py", PROVENANCE + f"\n# {LONG_NAME}\n", hashes)
    assert kinds(findings) == {(5, "comment")}


def test_the_header_is_skipped_after_a_shebang_and_encoding_line(hashes):
    source = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + PROVENANCE + "x = 1\n"
    assert st.scan_source("m.py", source, hashes) == []


def test_a_comment_right_after_the_header_is_still_scanned(hashes):
    findings = st.scan_source("m.py", PROVENANCE + f"# {LONG_NAME}\n", hashes)
    assert kinds(findings) == {(4, "comment")}


def test_a_header_lookalike_below_the_top_is_scanned(hashes):
    source = "import os\n" + PROVENANCE
    assert st.scan_source("m.py", source, hashes)


def test_header_skipping_is_only_for_python_files(hashes):
    assert st.scan_source("notes.md", PROVENANCE, hashes)


# --- scanning other files -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a.toml", "a.md", "a.json", "a.yml", "a.txt", "a.cfg"])
def test_text_files_are_scanned_by_line(name, hashes):
    text = f"line one\nsee https://{LONG_HOST}:8443/x\n"
    assert kinds(st.scan_source(name, text, hashes)) == {(2, "text")}


def test_text_files_match_short_tokens_only_as_a_whole_line(hashes):
    assert st.scan_source("a.md", f"one\n  {SHORT}  \n", hashes)
    assert not st.scan_source("a.md", f"{SHORT} and de-duplication\n", hashes)


def test_scan_paths_walks_directories_and_skips_binary_files(tmp_path, hashes):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(f'x = "{LONG_NAME}"\n', encoding="utf-8")
    (tmp_path / "pkg" / "b.bin").write_bytes(b"\0" + LONG_NAME.encode())
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "c.py").write_text(f'x = "{LONG_NAME}"\n', encoding="utf-8")
    findings = st.scan_paths([tmp_path], hashes)
    assert [Path(f.path).name for f in findings] == ["a.py"]


def test_scan_paths_rejects_a_missing_path(tmp_path, hashes):
    with pytest.raises(FileNotFoundError):
        st.scan_paths([tmp_path / "nope"], hashes)


# --- command line ---------------------------------------------------------------------------------------------------


def test_cli_hash_prints_the_hash_file_and_it_round_trips(tmp_path, capsys):
    plain = tmp_path / "plain.txt"
    plain.write_text("# tokens\n" + "\n".join(TOKENS) + "\n\n", encoding="utf-8")
    assert st.main(["hash", str(plain)]) == 0
    captured = capsys.readouterr()
    assert captured.out == st.render_hash_file(TOKENS)
    # Hex digests can contain a short token by chance, so only the long ones are compared.
    assert not any(token in captured.out for token in TOKENS if len(token) > st.WHOLE_ONLY_BELOW)
    assert st.parse_hash_file(captured.out, "stdout").whole == frozenset({st.token_hash(SHORT)})


def test_cli_hash_warns_by_line_number_about_tokens_that_can_only_match_a_whole_literal(tmp_path, capsys):
    plain = tmp_path / "plain.txt"
    plain.write_text(f"{LONG_NAME}\n{CIDR}\n", encoding="utf-8")
    assert st.main(["hash", str(plain)]) == 0
    err = capsys.readouterr().err
    assert "plain.txt:2" in err
    assert "plain.txt:1" not in err
    assert CIDR not in err


def test_cli_check_exit_codes(tmp_path, hash_file, capsys):
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")
    dirty = tmp_path / "dirty.py"
    dirty.write_text(f'x = "{LONG_NAME}"\n', encoding="utf-8")
    assert st.main(["check", "--hashes", str(hash_file), str(clean)]) == 0
    assert st.main(["check", "--hashes", str(hash_file), str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"{dirty}:1 string {st.token_hash(LONG_NAME)[:12]}" in captured.out
    assert LONG_NAME not in captured.out + captured.err


def test_cli_check_defaults_to_src_and_the_default_hash_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(f'x = "{LONG_NAME}"\n', encoding="utf-8")
    assert st.main(["check"]) == 0
    assert "skipped" in capsys.readouterr().err
    (tmp_path / "tests" / "data").mkdir(parents=True)
    (tmp_path / "tests" / "data" / "study_tokens.sha256").write_text(st.render_hash_file(TOKENS), encoding="utf-8")
    assert st.main(["check"]) == 1


def test_cli_check_errors_on_missing_explicit_hash_file_or_path(tmp_path, hash_file, capsys):
    assert st.main(["check", "--hashes", str(tmp_path / "missing"), str(tmp_path)]) == 2
    assert st.main(["check", "--hashes", str(hash_file), str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err.count("error:") == 2


def test_module_entry_point_runs_as_a_script(tmp_path, hash_file):
    (tmp_path / "m.py").write_text(f"# {LONG_NAME}\n", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "expcore.provenance.study_tokens", "check", "--hashes", str(hash_file), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert "m.py:1 comment" in done.stdout
