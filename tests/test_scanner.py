"""Scanner details: scope, path disclosure, f-strings, adjacent literals and edge separators. Names are invented."""

from pathlib import Path

import pytest

from expcore.provenance import study_tokens as st
from expcore.provenance.hashfile import scope_for_category
from expcore.provenance.scanner import REDACTED_PATH, Finding, decode_escapes

HOST = "fake-zone.example.net"
NAME = "secret-space"
PRIVATE = "_private_"
SHORT = "zq"
TOKENS = [(HOST, "code"), (NAME, "all"), (PRIVATE, "all"), (SHORT, "all")]


@pytest.fixture
def hashes() -> st.Hashes:
    return st.parse_hash_file(st.render_hash_file(TOKENS), "test")


def lines_and_kinds(findings: list[Finding]) -> list[tuple[int, str]]:
    return [(f.line, f.kind) for f in findings]


# --- scope ----------------------------------------------------------------------------------------------------------


def test_code_scope_tokens_are_found_in_code_but_not_in_prose(hashes):
    text = f"{HOST} and {NAME}\n"
    assert len(st.scan_source("src/a.py", f"# {text}", hashes)) == 2
    assert len(st.scan_source("config/settings.toml", text, hashes)) == 2
    all_only = [st.token_hash(NAME)[:12]]
    assert [f.hash_prefix for f in st.scan_source("docs/guide.md", text, hashes)] == all_only
    assert [f.hash_prefix for f in st.scan_source("README.md", text, hashes)] == all_only
    assert len(st.scan_source("docs/sub/dir/page.py", f"# {text}", hashes)) == 1


def test_prose_is_any_markdown_file_or_anything_under_a_leading_docs_directory(hashes):
    assert st.scan_source("src/docs/guide.md", f"{HOST}\n", hashes) == []
    assert st.scan_source(".claude/commands/x.md", f"{HOST}\n", hashes) == []
    assert st.scan_source("./docs/guide.txt", f"{HOST}\n", hashes) == []
    assert len(st.scan_source("mydocs/guide.txt", f"{HOST}\n", hashes)) == 1


@pytest.mark.parametrize("category", ["models", "repositories", "conditions", "datasets"])
def test_design_and_public_name_categories_are_code_scope(category):
    assert scope_for_category(category) == "code"


@pytest.mark.parametrize("category", ["hosts", "addresses", "spaces", "providers", "users", "env-vars", None])
def test_private_name_categories_are_checked_everywhere(category):
    assert scope_for_category(category) == "all"


# --- path disclosure ------------------------------------------------------------------------------------------------


def test_a_matching_path_is_a_path_finding_and_hides_the_path_in_every_finding_of_the_file(hashes):
    findings = st.scan_source(f"src/{NAME}.py", f'x = "{NAME}"\n', hashes)
    assert {f.kind for f in findings} == {"path", "string"}
    assert {f.path for f in findings} == {REDACTED_PATH}
    rendered = " ".join(f"{f} {f!r}" for f in findings)
    assert NAME not in rendered
    assert str(findings[0]).startswith(f"{REDACTED_PATH}:")
    assert {f.hash_prefix for f in findings} == {st.token_hash(NAME)[:12]}


def test_path_matching_works_for_directories_and_composed_names_and_short_names(hashes):
    for path in (f"{NAME}/mod.py", f"pkg/x-{NAME}-y.py", f"pkg/{HOST}.txt", f"pkg/{SHORT}/mod.py", f"pkg/{SHORT}.py"):
        found = st.scan_source(path, "x = 1\n", hashes)
        assert [f.kind for f in found] == ["path"], path
        assert found[0].path == REDACTED_PATH


def test_a_path_that_only_resembles_a_token_is_shown(hashes):
    for path in ("pkg/secret_space.py", "pkg/secretspace.py", "pkg/index.py", "pkg/zq-dup.py"):
        assert st.scan_source(path, "x = 1\n", hashes) == [], path


def test_a_clean_path_is_shown(hashes):
    findings = st.scan_source("src/pkg/mod.py", f'x = "{NAME}"\n', hashes)
    assert [f.path for f in findings] == ["src/pkg/mod.py"]


def test_path_under_docs_is_matched_against_all_scope_only(hashes):
    assert st.scan_source(f"docs/{HOST}.md", "text\n", hashes) == []
    assert [f.kind for f in st.scan_source(f"docs/{NAME}.md", "text\n", hashes)] == ["path"]


def test_scan_paths_checks_the_path_of_a_binary_file_but_not_its_content(tmp_path, hashes, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / f"{NAME}.bin").write_bytes(b"\0" + NAME.encode())
    (tmp_path / "clean.bin").write_bytes(b"\0" + NAME.encode())
    findings = st.scan_paths([Path(".")], hashes)
    assert [(f.path, f.kind) for f in findings] == [(REDACTED_PATH, "path")]


def test_scan_paths_hides_a_matching_path(tmp_path, hashes, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / NAME).mkdir()
    (tmp_path / NAME / "m.py").write_text("x = 1\n", encoding="utf-8")
    findings = st.scan_paths([tmp_path / NAME], hashes)
    assert [(f.path, f.kind) for f in findings] == [(REDACTED_PATH, "path")]


# --- f-strings ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'x = f"\\x73ecret-space {y}"\n',
        'x = f"{y} \\x73ecret-space"\n',
        'x = f"\\N{LATIN SMALL LETTER S}ecret-space {y}"\n',
        'x = f"\\u0073ecret-space {y}"\n',
        'x = f"\\163ecret-space {y}"\n',
    ],
)
def test_escapes_in_fstring_parts_are_decoded(source, hashes):
    found = st.scan_source("m.py", source, hashes)
    assert found, source
    assert {f.kind for f in found} == {"string"}


def test_decode_escapes_helper():
    assert decode_escapes("\\x61b") == "ab"
    assert decode_escapes("plain") is None
    assert decode_escapes("\\xZZ") is None
    assert decode_escapes("caf\u00e9 \\x61") == "caf\u00e9 a"


def test_an_fstring_without_replacement_fields_is_one_whole_literal(hashes):
    assert lines_and_kinds(st.scan_source("m.py", f'x = f"{SHORT}"\n', hashes)) == [(1, "string")]
    assert lines_and_kinds(st.scan_source("m.py", f"x = f'  {SHORT}  '\n", hashes)) == [(1, "string")]
    assert lines_and_kinds(st.scan_source("m.py", 'x = f"\\x7aq"\n', hashes)) == [(1, "string")]
    assert lines_and_kinds(st.scan_source("m.py", 'x = rf"{{zq}}"\n', hashes)) == []


def test_the_literal_parts_of_an_fstring_with_fields_are_checked_as_whole_values(hashes):
    for source in (f'x = f"{SHORT}{{y}}"\n', f'x = f"{{y}}{SHORT}"\n', f'x = f"{SHORT}{{f"{{y}}"}}"\n'):
        assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(1, "string")], source
    assert st.scan_source("m.py", f'x = f"{SHORT}-{{y}}"\n', hashes) == []  # the part is "zq-", not the token


def test_fstring_parts_still_match_embedded_tokens(hashes):
    assert lines_and_kinds(st.scan_source("m.py", f'x = f"https://{{host}}.{HOST}/"\n', hashes)) == [(1, "string")]


def test_nested_and_multiline_fstrings(hashes):
    source = f'x = (\n    f"first {{a}} "\n    f"{{b!r}} {NAME}"\n)\n'
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(3, "string")]


# --- adjacent literals ----------------------------------------------------------------------------------------------


def test_adjacent_literals_are_checked_as_one_value(hashes):
    source = 'x = ("secret-"\n     "space")\ny = ("fake-zone." "example.net")\n'
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(1, "string"), (3, "string")]


def test_adjacent_bytes_literals_are_checked_as_one_value(hashes):
    assert lines_and_kinds(st.scan_source("m.py", "x = (b'secret-' b'space')\n", hashes)) == [(1, "string")]


def test_a_short_token_split_over_adjacent_literals_is_a_whole_value(hashes):
    assert lines_and_kinds(st.scan_source("m.py", 'x = ("z" "q")\n', hashes)) == [(1, "string")]
    assert lines_and_kinds(st.scan_source("m.py", 'x = ("z" f"q")\n', hashes)) == [(1, "string")]


def test_a_comment_between_adjacent_literals_does_not_split_them(hashes):
    source = 'x = ("secret-"  # note\n     "space")\n'
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(1, "string")]


def test_literals_separated_by_anything_else_are_not_joined(hashes):
    assert st.scan_source("m.py", 'x = ["secret-", "space"]\n', hashes) == []
    assert st.scan_source("m.py", 'x = "secret-"\ny = "space"\n', hashes) == []
    assert st.scan_source("m.py", 'x = ("secret-" + "space")\n', hashes) == []
    assert st.scan_source("m.py", 'x = ("secret-" f"{y}" "space")\n', hashes) == []


def test_a_token_inside_one_piece_is_reported_once(hashes):
    source = f'x = ("{NAME}"\n     "other")\n'
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(1, "string")]


# --- edge separators ------------------------------------------------------------------------------------------------


def test_a_token_with_edge_separators_matches_inside_a_longer_run(hashes):
    assert lines_and_kinds(st.scan_source("m.py", 'x = "prefix._private_.suffix"\n', hashes)) == [(1, "string")]
    # Attribute access makes three separate identifiers; the middle one equals the token as a whole.
    assert lines_and_kinds(st.scan_source("m.py", "prefix._private_.suffix = 1\n", hashes)) == [(1, "name")]
    assert lines_and_kinds(st.scan_source("m.py", "a__private__b = 1\n", hashes)) == [(1, "name")]
    assert st.scan_source("m.py", "x = privateer\n", hashes) == []


def test_twenty_four_component_tokens_are_found(hashes):
    token = ".".join(f"w{i}" for i in range(24))
    long_hashes = st.parse_hash_file(st.render_hash_file([token]), "test")
    assert st.scan_source("a.txt", f"see {token}\n", long_hashes)
    assert st.scan_source("a.txt", f"see x.{token}.y\n", long_hashes)


# --- constants the parser has merged --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "x = f\"{'secret-' 'space'}\"\n",
        'x = "secret-" f"space{1}"\n',
        'x = f"{1}secret-" "space"\n',
        'x = f"{1}" "secret-" f"space{1}"\n',
        "x = f\"{y:{'secret-' 'space'}}\"\n",
        "x = (\n    'secret-'\n    f'space{1}'\n)\n",
        "x = b'secret-' b'space'\n",
        "x = b'secret-\\xffspace'\n",
    ],
)
def test_literals_the_parser_merges_are_checked_as_one_value(source, hashes):
    found = st.scan_source("m.py", source, hashes)
    assert found, source
    assert {f.kind for f in found} == {"string"}
    assert {f.hash_prefix for f in found} == {st.token_hash(NAME)[:12]}


def test_ast_findings_carry_the_line_of_the_constant(hashes):
    source = "a = 1\nb = (\n    f'{a}' 'secret-'\n    'space'\n)\n"
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(3, "string")]


def test_literals_that_are_not_contiguous_after_merging_are_not_joined(hashes):
    assert st.scan_source("m.py", 'x = "secret-" f"{y}space"\n', hashes) == []
    assert st.scan_source("m.py", "x = f\"{'secret-'}{'space'}\"\n", hashes) == []


def test_a_finding_the_token_pass_made_is_not_repeated_by_the_constant_pass(hashes):
    source = f'doc = """first\nsecond\nthird {NAME}\n"""\n'
    assert lines_and_kinds(st.scan_source("m.py", source, hashes)) == [(3, "string")]


def test_source_that_does_not_parse_keeps_only_the_token_pass(hashes):
    valid = "x = f\"{'secret-' 'space'}\" + 'a'\n"
    broken = "x = f\"{'secret-' 'space'}\" +\n"
    assert st.scan_source("m.py", valid, hashes)
    assert st.scan_source("m.py", broken, hashes) == []
    assert lines_and_kinds(st.scan_source("m.py", f'x = "{NAME}" +\n', hashes)) == [(1, "string")]


def test_an_invalid_escape_in_a_literal_does_not_stop_the_scan(hashes):
    assert lines_and_kinds(st.scan_source("m.py", f'x = "\\d {NAME}"\n', hashes)) == [(1, "string")]


def test_a_lone_surrogate_in_a_literal_is_scanned_without_error(hashes):
    source = 'x = f"{1}\\ud800"\ny = "\\udfff"\n'
    assert st.scan_source("src/a.py", source, hashes) == []
