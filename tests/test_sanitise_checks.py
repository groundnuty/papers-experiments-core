"""Sanitiser safeguards: escape syntax, Unicode boundaries, the post-check, argument validation and CRLF."""

import ast

import pytest

from expcore.provenance import sanitise as sn
from expcore.provenance.headers import read_header
from expcore.provenance.sanitise_map import parse_map

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

[[entry]]
token = "latin"
replacement = "greek"
category = "word"

[[entry]]
token = "zq"
replacement = "xx"
category = "option"
"""


@pytest.fixture
def entries():
    return parse_map(MAP_TEXT.encode(), "map.toml").entries


@pytest.fixture
def map_file(tmp_path):
    path = tmp_path / "map.toml"
    path.write_text(MAP_TEXT, encoding="utf-8")
    return path


def run_import(map_file, src, dest, **overrides) -> int:
    args = {"source_repo": "fake-study-repo", "commit": "0123abcd", "path": "pkg/mod.py", **overrides}
    return sn.main(
        [
            "import",
            "--map",
            str(map_file),
            "--source-repo",
            args["source_repo"],
            "--commit",
            args["commit"],
            "--path",
            args["path"],
            str(src),
            str(dest),
        ]
    )


# --- escape syntax --------------------------------------------------------------------------------------------------


def test_a_token_after_an_escape_is_replaced_and_the_escape_is_kept(entries):
    source = 'x = "line\\nsecret-space"\ny = "tab\\tsecret_space"\n'
    assert sn.sanitise_python(source, entries) == 'x = "line\\nspace-a"\ny = "tab\\tspace_a"\n'


def test_named_unicode_escapes_are_never_rewritten(entries):
    source = 'x = "\\N{LATIN SMALL LETTER S}" + "latin"\n'
    assert sn.sanitise_python(source, entries) == 'x = "\\N{LATIN SMALL LETTER S}" + "greek"\n'


def test_named_escape_inside_an_fstring_is_never_rewritten(entries):
    source = 'x = f"\\N{LATIN SMALL LETTER S}{y}latin"\n'
    assert sn.sanitise_python(source, entries) == 'x = f"\\N{LATIN SMALL LETTER S}{y}greek"\n'


def test_backslashes_are_ordinary_characters_in_raw_strings_and_comments(entries):
    source = 'x = r"C:\\secret-space"\ny = rf"C:\\secret-space {z}"\n# C:\\secret-space\n'
    expected = 'x = r"C:\\space-a"\ny = rf"C:\\space-a {z}"\n# C:\\space-a\n'
    assert sn.sanitise_python(source, entries) == expected


def test_a_token_hidden_behind_an_escape_is_refused_by_the_post_check(entries):
    for source in ('x = "\\x73ecret-space"\n', 'x = "C:\\secret-space"\n', 'x = f"\\x73ecret-space {y}"\n'):
        assert sn.sanitise_python(source, entries) == source  # the rewriting leaves escape syntax alone
        with pytest.raises(sn.SanitiseError, match=r"mod\.py: 1 study-token finding\(s\) remain after sanitising"):
            sn.sanitise_checked(source, entries, filename="mod.py")


def test_whole_fstring_literals_are_replaced_as_a_whole(entries):
    assert sn.sanitise_python('x = f"zq"\ny = f"{zq}"\n', entries) == 'x = f"xx"\ny = f"{xx}"\n'
    assert sn.sanitise_python('x = f"zq-{y}"\n', entries) == 'x = f"zq-{y}"\n'


# --- Unicode boundaries ---------------------------------------------------------------------------------------------


def test_a_token_glued_to_an_accented_letter_is_left_alone(entries):
    glued = 'a = "\u00e9secret-space"\nb = "secret-space\u00e9"\n\u00e9secret_space = 1\n'
    source = glued + 'c = "x \u00e9secret-space secret-space"\n'
    expected = glued + 'c = "x \u00e9secret-space space-a"\n'
    assert sn.sanitise_python(source, entries) == expected


def test_the_post_check_refuses_what_unicode_boundaries_left_behind(entries):
    # The checker's runs are ASCII-only, so it still sees the token next to the accented letter.
    with pytest.raises(sn.SanitiseError, match="remain after sanitising"):
        sn.sanitise_checked('x = "secret-space\u00e9"\n', entries, filename="mod.py")


def test_import_refuses_a_source_with_leftovers_and_writes_nothing(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text('x = "\\x73ecret-space"\n', encoding="utf-8")
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 2
    assert "remain after sanitising" in capsys.readouterr().err
    assert not dest.exists()


def test_the_post_check_message_names_hash_prefixes_only(entries):
    with pytest.raises(sn.SanitiseError) as caught:
        sn.sanitise_checked('x = "\\x73ecret-space"\n', entries, filename="mod.py")
    assert "secret-space" not in str(caught.value)


# --- argument validation --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("repo", ["fake-repo", "owner/fake-repo", "name.with_chars-1", "a/b"])
def test_good_source_repositories(repo):
    assert sn.validate_source(repo, "0123abc", "pkg/mod.py") == "0123abc"


@pytest.mark.parametrize(
    "repo",
    ["https://host.example/o/r", "git@host.example:o/r", "a b", "", "a/b/c", "../x", "x/..", "./x", "a@b", "x://y"],
)
def test_source_repositories_that_are_urls_or_paths_are_refused(repo):
    with pytest.raises(sn.SanitiseError, match="source repository"):
        sn.validate_source(repo, "0123abc", "pkg/mod.py")


@pytest.mark.parametrize("commit", ["abc123", "0123abcg", "HEAD", "", "a" * 41, "0123 abc"])
def test_commits_must_be_seven_to_forty_hex_characters(commit):
    with pytest.raises(sn.SanitiseError, match="7 to 40 hexadecimal"):
        sn.validate_source("fake-repo", commit, "pkg/mod.py")


def test_commit_lengths_and_case():
    assert sn.validate_source("fake-repo", "a" * 7, "m.py") == "a" * 7
    assert sn.validate_source("fake-repo", "a" * 40, "m.py") == "a" * 40
    assert sn.validate_source("fake-repo", "ABCDEF1", "m.py") == "abcdef1"


@pytest.mark.parametrize(
    "path", ["/etc/mod.py", "../mod.py", "pkg/../mod.py", "..", "", "pkg\\mod.py", "pkg/mod.py\n# x"]
)
def test_source_paths_must_be_relative_without_parent_references(path):
    with pytest.raises(sn.SanitiseError, match="source path"):
        sn.validate_source("fake-repo", "0123abc", path)


def test_the_command_line_refuses_bad_arguments_before_writing(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text("x = 1\n", encoding="utf-8")
    for overrides in ({"source_repo": "https://host.example/r"}, {"commit": "main"}, {"path": "../x.py"}):
        dest = tmp_path / "mod.py"
        assert run_import(map_file, src, dest, **overrides) == 2
        assert not dest.exists()
    assert capsys.readouterr().err.count("error:") == 3


# --- CRLF -----------------------------------------------------------------------------------------------------------


def test_a_fresh_crlf_import_verifies(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    source = (
        b'#!/usr/bin/env python3\r\n"""Doc."""\r\nURL = "https://x.example/secret-space"\r\n\r\n\r\n'
        b"def f():\r\n    return 1\r\n"
    )
    src.write_bytes(source)
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 0
    data = dest.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")  # no bare LF, header included
    assert b"space-a" in data
    assert read_header(data.decode("utf-8")) is not None
    capsys.readouterr()
    verify = sn.main(["verify", "--map", str(map_file), "--source", str(src), "--module", str(dest)])
    assert verify == 0
    assert capsys.readouterr().out.startswith("ok ")


def test_a_crlf_import_that_was_edited_still_fails_verification(map_file, tmp_path):
    src = tmp_path / "orig.py"
    src.write_bytes(b'x = "secret-space"\r\ny = 2\r\n')
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 0
    dest.write_bytes(dest.read_bytes().replace(b"y = 2", b"y = 3"))
    assert sn.main(["verify", "--map", str(map_file), "--source", str(src), "--module", str(dest)]) == 1


def test_an_lf_import_keeps_lf_headers(map_file, tmp_path):
    src = tmp_path / "orig.py"
    src.write_bytes(b'x = "secret-space"\n')
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 0
    assert b"\r" not in dest.read_bytes()


# --- merged literals the rewriting cannot see -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "x = f\"{'secret-' 'space'}\"\n",
        'x = "secret-" f"space{1}"\n',
        'x = f"{1}secret-" "space"\n',
    ],
)
def test_the_post_check_refuses_literals_the_parser_merges(source, entries, map_file, tmp_path):
    assert sn.sanitise_python(source, entries) == source  # no single token holds the whole name
    with pytest.raises(sn.SanitiseError, match=r"mod\.py: 1 study-token finding\(s\) remain after sanitising"):
        sn.sanitise_checked(source, entries, filename="mod.py")
    src = tmp_path / "orig.py"
    src.write_text(source, encoding="utf-8")
    dest = tmp_path / "out.py"
    assert run_import(map_file, src, dest) == 2
    assert not dest.exists()


# --- whole escape sequences -----------------------------------------------------------------------------------------

HEX_MAP = b"""version = 1

[[entry]]
token = "abcd"
replacement = "ef12"
category = "word"
"""


@pytest.mark.parametrize(
    "source",
    [
        'x = "\\uabcd"\n',
        'x = "\\U0000abcd"\n',
        'x = "\\xabcd"\n',
        'x = "\\N{LATIN SMALL LETTER A}bcd"\n',
        'x = f"\\uabcd{y}"\n',
        'x = f"{y}\\xabcd"\n',
        "x = '\\uabcd' + '\\uABCD'\n",
    ],
)
def test_escape_sequences_are_masked_whole_and_their_digits_never_rewritten(source):
    entries = parse_map(HEX_MAP, "hex.toml").entries
    assert sn.sanitise_python(source, entries) == source


def test_text_after_a_whole_escape_is_still_rewritten_and_the_value_stays_valid():
    entries = parse_map(HEX_MAP, "hex.toml").entries
    cases = {
        'x = "\\uabcd abcd"\n': 'x = "\\uabcd ef12"\n',
        'x = "\\U0000abcd abcd"\n': 'x = "\\U0000abcd ef12"\n',
        'x = "\\101abcd"\n': 'x = "\\101ef12"\n',
        'x = "\\x41 abcd"\n': 'x = "\\x41 ef12"\n',
        'x = "abcd"\n': 'x = "ef12"\n',
    }
    for source, expected in cases.items():
        assert sn.sanitise_python(source, entries) == expected
    result = sn.sanitise_python('x = "\\uabcd abcd"\n', entries)
    assert ast.literal_eval(result.removeprefix("x = ").strip()) == "\uabcd ef12"  # the escape keeps its meaning
