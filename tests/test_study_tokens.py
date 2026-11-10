"""The study-token hashing, candidate extraction, scanner and command line. Tokens here are all invented."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.provenance import hashfile as hf
from expcore.provenance import study_tokens as st

LONG_HOST = "fake-zone.example.net"
LONG_NAME = "secret-space"
LONG_IDENT = "secret_space"
SHORT = "zq"
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


# --- hash file ------------------------------------------------------------------------------------------------------


def test_hash_file_is_sorted_deduplicated_and_marks_short_tokens_whole():
    text = st.render_hash_file(["zq", "ZQ ", "secret-space", "Secret-Space", "", "abc", "abcd"])
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert lines == sorted(lines)
    assert len(lines) == len(set(lines)) == 4
    assert f"whole all {st.token_hash('zq')}" in lines
    assert f"whole all {st.token_hash('abc')}" in lines
    assert f"any all {st.token_hash('abcd')}" in lines
    assert f"any all {st.token_hash('secret-space')}" in lines


def test_hash_file_lines_carry_a_scope_and_all_wins_over_code_for_the_same_token():
    text = st.render_hash_file([("alpha-one", "code"), ("beta-two", "code"), ("beta-two", "all"), "gamma-three"])
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert f"any code {st.token_hash('alpha-one')}" in lines
    assert f"any all {st.token_hash('beta-two')}" in lines
    assert f"any all {st.token_hash('gamma-three')}" in lines
    assert len(lines) == 3
    with pytest.raises(ValueError, match="unknown scope"):
        st.render_hash_file([("alpha-one", "everywhere")])


def test_hash_file_round_trips_through_the_parser():
    loaded = st.parse_hash_file(st.render_hash_file(TOKENS), "test")
    assert set(loaded.whole) == {st.token_hash(SHORT)}
    assert st.token_hash(LONG_HOST) in loaded.anywhere
    assert loaded


def test_two_field_lines_from_the_older_format_read_as_scope_all():
    old = f"any {st.token_hash('alpha-one')}\nwhole {st.token_hash('ab')}\nany code {st.token_hash('beta-two')}\n"
    loaded = st.parse_hash_file(old, "old.sha256")
    assert loaded.anywhere == {st.token_hash("alpha-one"): "all", st.token_hash("beta-two"): "code"}
    assert loaded.whole == {st.token_hash("ab"): "all"}
    assert set(loaded.only_all().anywhere) == {st.token_hash("alpha-one")}


@pytest.mark.parametrize(
    "line", ["any nothex", "some " + "0" * 64, "any", "0" * 64, "any everywhere " + "0" * 64, "any all x " + "0" * 64]
)
def test_malformed_hash_lines_name_the_file_and_line(line):
    with pytest.raises(ValueError, match=r"hashes\.sha256:3:"):
        st.parse_hash_file(f"# comment\n\n{line}\n", "hashes.sha256")


def test_plain_list_ignores_comments_and_blank_lines():
    assert st.parse_plain_list("# c\n\n  one \ntwo\n") == ["one", "two"]


def test_plain_list_categories_set_the_scope():
    text = (
        "before-any-header\n# category: Conditions\ncond-one\n# a remark\ncond-two\n"
        "# category: hosts\nhost-one\n#category:DATASETS\nset-one\n# category: data-states\nstate-one\n"
        "# category: tasks\ntask-one\n# category: users\nuser-one\n"
    )
    scopes = {e.token: e.scope for e in hf.parse_plain_entries(text)}
    assert scopes == {
        "before-any-header": "all",
        "cond-one": "code",
        "cond-two": "code",
        "host-one": "all",
        "set-one": "code",
        "state-one": "code",
        "task-one": "code",
        "user-one": "all",
    }
    assert [e.line for e in hf.parse_plain_entries(text)][:2] == [1, 3]


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
        "# zq-duplication of rows\n",
        'x = "zq-duplicate"\n',
        'x = "idle"\n',
        "index = 1\n",
        "def zq_dup(): pass\n",
        'x = "a zq b"\n',
        'x = "secret space"\n',
        'x = "supersecret-spaces"\n',
        'x = "10.99.0.0"\n',
        'x = f"{index}-zq-duplicate"\n',
        "x = 1\n",
    ],
)
def test_python_without_a_token_has_no_findings(source, hashes):
    assert st.scan_source("mod.py", source, hashes) == []


def test_short_tokens_match_only_a_whole_identifier_or_literal(hashes):
    assert st.scan_source("m.py", 'x = "zq"\n', hashes)
    assert st.scan_source("m.py", "zq = 1\n", hashes)
    assert not st.scan_source("m.py", '# zq\nx = "zq-zq"\n', hashes)


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
    assert not st.scan_source("a.md", f"{SHORT} and zq-duplication\n", hashes)


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
    assert set(st.parse_hash_file(captured.out, "stdout").whole) == {st.token_hash(SHORT)}


def test_cli_hash_takes_the_scope_from_category_headers(tmp_path, capsys):
    plain = tmp_path / "plain.txt"
    plain.write_text("# category: conditions\nalpha-one\n# category: hosts\nbeta-two\n", encoding="utf-8")
    assert st.main(["hash", str(plain)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"any code {st.token_hash('alpha-one')}" in lines
    assert f"any all {st.token_hash('beta-two')}" in lines


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


def test_cli_check_defaults_to_the_whole_repository_and_the_default_hash_file(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.path)
    repo.write("docs/page.md", f"{LONG_NAME}\n")
    repo.write("README.md", f"{LONG_HOST}\n")
    repo.write("tests/data/study_tokens.sha256", st.render_hash_file(TOKENS))
    repo.write(".gitignore", "ignored.txt\n")
    repo.write("ignored.txt", f"{LONG_NAME}\n")
    repo.git("add", "docs", "tests", ".gitignore")
    assert st.main(["check"]) == 1
    out = capsys.readouterr().out
    assert f"docs/page.md:1 text {st.token_hash(LONG_NAME)[:12]}" in out
    assert f"README.md:1 text {st.token_hash(LONG_HOST)[:12]}" in out  # untracked but not ignored
    assert "ignored.txt" not in out
    assert "study_tokens.sha256" not in out  # the hash file itself is never scanned


def test_check_works_outside_a_git_work_tree(tmp_path, monkeypatch, hash_file, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text(f'x = "{LONG_NAME}"\n', encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(LONG_NAME, encoding="utf-8")
    assert st.main(["check", "--hashes", str(hash_file)]) == 1
    out = capsys.readouterr().out
    assert "pkg/m.py:1 string" in out
    assert ".git" not in out


def test_check_exits_2_when_the_hash_file_is_absent_or_empty_unless_allowed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert st.main(["check"]) == 2
    assert "tests/data/study_tokens.sha256 not found" in capsys.readouterr().err
    assert st.main(["check", "--allow-missing-hashes"]) == 0
    assert "skipped" in capsys.readouterr().err
    empty = tmp_path / "empty.sha256"
    empty.write_text("# nothing here\n", encoding="utf-8")
    assert st.main(["check", "--hashes", str(empty)]) == 2
    assert "holds no hashes" in capsys.readouterr().err
    assert st.main(["check", "--hashes", str(empty), "--allow-missing-hashes"]) == 0
    assert st.main(["check", "--hashes", str(tmp_path / "gone"), "--allow-missing-hashes"]) == 0


@pytest.mark.parametrize("line", ["{digest}", "{digest}  -", "{digest}  names.txt"])
def test_k01_a_file_of_plain_sha256_digests_is_refused_saying_how_to_generate_one(tmp_path, capsys, line):
    """K01: a file of plain `sha256(token)` digests (bare, or as sha256sum prints them) would check nothing, since a
    study-token hash is salted and normalised; it is refused with the command that makes a correct file."""
    plain = tmp_path / "plain.sha256"
    plain.write_text("\n".join([line.format(digest=hashlib.sha256(LONG_NAME.encode()).hexdigest())] * 2) + "\n")
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert st.main(["check", "--hashes", str(plain), str(tmp_path / "m.py")]) == 2
    err = capsys.readouterr().err
    assert "plain.sha256:1: a bare SHA-256 digest" in err
    assert "python -m expcore.provenance.study_tokens hash" in err


def test_k01_a_malformed_hash_line_names_the_generating_command():
    """K01: any line not in the hash format refuses the file, naming the line and the command that makes one."""
    with pytest.raises(ValueError, match=r"names\.sha256:2: expected '<any\|whole> <code\|all> <sha256>'.*hash PLAIN"):
        st.parse_hash_file(f"any all {'0' * 64}\nany secret-word\n", "names.sha256")


def test_k01_the_check_says_how_many_hashes_it_loaded_and_which_list_never_its_path(tmp_path, hash_file, capsys):
    """K01: the check prints how many hashes it read, so a file that loads but holds the wrong ones is noticed, and
    whether it is the default list or one given in its place, never the path, which is the author's."""
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert st.main(["check", "--hashes", str(hash_file), str(tmp_path / "m.py")]) == 0
    count = len(st.load_hashes(hash_file).anywhere) + len(st.load_hashes(hash_file).whole)
    assert count > 1
    err = capsys.readouterr().err
    assert f"study-token check: {count} hashes, from the list given with --hashes\n" in err
    assert str(hash_file) not in err


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


ORDINARY = "lantern"  # an invented ordinary word, long enough to be matched anywhere unless marked whole


def test_a_whole_line_in_the_plain_list_marks_its_token_and_keeps_its_category():
    """A `whole:` line names one token that matches only as an entire value, whatever its length."""
    text = f"# category: providers\nwhole: {ORDINARY}\nopen-name\n"
    entries = hf.parse_plain_entries(text)
    assert [(e.token, e.scope, e.whole) for e in entries] == [(ORDINARY, "all", True), ("open-name", "all", False)]


def test_a_whole_line_naming_no_token_is_refused_by_line_number():
    with pytest.raises(ValueError, match=r"^line 2: 'whole:' names no token$"):
        hf.parse_plain_entries("first-name\nwhole:   \n")


def test_a_token_marked_whole_is_rendered_whole_with_its_scope_and_the_mark_wins_when_listed_both_ways():
    marked = st.render_hash_file([(ORDINARY, "code", True)])
    assert [line.split()[:2] for line in marked.splitlines() if not line.startswith("#")] == [["whole", "code"]]
    both = st.render_hash_file([(ORDINARY, "all"), (ORDINARY, "all", True)])
    assert [line.split()[0] for line in both.splitlines() if not line.startswith("#")] == ["whole"]
    plain = st.render_hash_file([(ORDINARY, "all")])
    assert [line.split()[0] for line in plain.splitlines() if not line.startswith("#")] == ["any"]


def test_a_token_marked_whole_is_found_as_an_entire_name_but_not_inside_a_longer_one():
    """The point of the mark: an ordinary word on the list stops matching inside unrelated identifiers."""
    marked = st.parse_hash_file(st.render_hash_file([(ORDINARY, "all", True)]), "test")
    assert st.scan_source("m.py", f"def light_{ORDINARY}_count():\n    return 1\n", marked) == []
    assert st.scan_source("m.py", f'site = "{ORDINARY}"\n', marked)
    unmarked = st.parse_hash_file(st.render_hash_file([ORDINARY]), "test")
    assert st.scan_source("m.py", f"def light_{ORDINARY}_count():\n    return 1\n", unmarked)


def test_the_hash_command_renders_a_marked_token_whole(tmp_path, capsys):
    plain = tmp_path / "plain.txt"
    plain.write_text(f"whole: {ORDINARY}\n", encoding="utf-8")
    assert st.main(["hash", str(plain)]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if not line.startswith("#")]
    assert [line.split()[:2] for line in lines] == [["whole", "all"]]
