"""Anonymisation lists: whole words only, only in the prose and metadata named, and never a term in a finding."""

import json

import pytest

from expcore.package.anonymise import REDACTED_PATH, Anonymisation, find_terms, load_anonymisation, rewrite
from expcore.package.spec import PackageError

TERMS = {"Quillon Varek": "Author A", "Varek": "Author A", "Brindlemoor Institute": "Institution A", "orvo": "Site A"}
FOUND = "a term of the anonymisation list"


def anonymisation_file(where, files=("README.md",), terms=None):
    lines = [f"files = {json.dumps(list(files))}", "", "[terms]"]
    lines += [f"{json.dumps(term)} = {json.dumps(label)}" for term, label in (terms or TERMS).items()]
    where.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return where


@pytest.fixture
def listed(tmp_path):
    (tmp_path / "study").mkdir()
    (tmp_path / "private").mkdir()
    path = anonymisation_file(tmp_path / "private" / "anon.toml")
    return load_anonymisation(path, keep_out_of=[tmp_path / "study"], model_names={"alpha-stub"})


def test_a_term_is_replaced_as_a_whole_word_in_the_named_prose_and_nowhere_inside_another_word(listed):
    readme = (
        "By Quillon  Varek (Brindlemoor Institute).\n"
        "VAREK wrote the orvo-adjacent step; orvo is a site.\n"
        "Varekson and quillon_varek and varek.example stay.\n"
    )
    files = rewrite({"README.md": readme.encode()}, listed, code=set())
    assert files["README.md"].decode().splitlines() == [
        "By Author A (Institution A).",
        "Author A wrote the orvo-adjacent step; Site A is a site.",
        "Varekson and quillon_varek and varek.example stay.",
    ]


def test_k05_a_term_in_a_code_file_or_a_record_fails_naming_the_file_and_line_never_the_term(listed):
    """K05: findings never carry the matched value; a code file is reported, never rewritten."""
    files = {
        "README.md": b"Author A wrote this.\n",
        "plugin.py": b"import os\n# written by varek\n",
        "out/runs/x.json": b'{\n "author": "Quillon Varek"\n}',
    }
    assert rewrite(files, listed, code={"plugin.py"})["plugin.py"] == files["plugin.py"]
    findings = find_terms(files, listed)
    assert findings == [f"out/runs/x.json:2: {FOUND}", f"plugin.py:2: {FOUND}"]
    assert not any(term.casefold() in "".join(findings).casefold() for term in TERMS)


def test_k05_a_term_the_whole_word_rule_left_inside_a_hyphenated_name_is_found_and_a_path_is_redacted(listed):
    """K05: nothing slips past the whole-word rule, and a path that holds a term is never shown."""
    files = rewrite({"README.md": b"See the Varek-lab notes.\n"}, listed, code=set())
    files["notes/varek/data.csv"] = b"1,2\n"
    assert find_terms(files, listed) == [f"README.md:1: {FOUND}", f"{REDACTED_PATH}: the path holds {FOUND}"]


@pytest.mark.parametrize(
    ("name", "code"),
    [("plugin.py", set()), ("study.toml", set()), ("prompt.txt", {"prompt.txt"}), ("out/report.md", set())],
)
def test_code_configuration_records_and_generated_tables_are_never_rewritten(listed, name, code):
    """A named file that is code, configuration, in the code identity or under out/ is refused, never relabelled."""
    named = Anonymisation(listed.source, (name,), listed.terms)
    with pytest.raises(PackageError, match="never rewritten"):
        rewrite({name: b"Varek\n"}, named, code=code)


def test_a_named_file_the_package_does_not_include_is_refused(listed):
    with pytest.raises(PackageError, match="does not include"):
        rewrite({"other.md": b""}, listed, code=set())


def test_k03_a_list_inside_the_study_or_the_output_is_refused(tmp_path):
    """K03: the private list never enters the repository or the output."""
    study, out = tmp_path / "study", tmp_path / "dist"
    (out / "nested").mkdir(parents=True)
    study.mkdir()
    for inside in (study / "anon.toml", out / "nested" / "anon.toml"):
        with pytest.raises(PackageError, match="K03"):
            load_anonymisation(anonymisation_file(inside), keep_out_of=[study, out], model_names=set())


@pytest.mark.parametrize(("term", "why"), [("2026", "no letter"), ("0.42", "no letter"), ("Alpha-Stub", "model")])
def test_k04_a_term_that_would_change_a_number_or_a_model_name_is_refused(tmp_path, term, why):
    """K04: anonymisation must not change numbers or model names; the refusal names the term's place, not the term."""
    path = anonymisation_file(tmp_path / "anon.toml", terms={"Varek": "Author A", term: "X"})
    with pytest.raises(PackageError, match=why) as refused:
        load_anonymisation(path, keep_out_of=[], model_names={"alpha-stub"})
    assert "term 2 of [terms]" in str(refused.value)
    assert term not in str(refused.value)


def test_k04_a_rewritten_json_or_toml_file_must_still_parse(tmp_path):
    """K04: anonymisation must not break a file's parse. JSON is rewritten in its parsed form, so a label with quotes
    stays one string; a TOML file that the plain-text rewrite would break is refused, as is JSON that does not parse."""
    path = anonymisation_file(tmp_path / "anon.toml", terms={"Varek": 'A "quoted" one'})
    quoted = load_anonymisation(path, keep_out_of=[], model_names=set())
    rewritten = rewrite({"codemeta.json": b'{"x": "Varek"}'}, named(quoted, "codemeta.json"), code=set())
    assert json.loads(rewritten["codemeta.json"]) == {"x": 'A "quoted" one'}
    with pytest.raises(PackageError, match="no longer parses as TOML"):
        rewrite({"meta.toml": b'x = "Varek"\n'}, named(quoted, "meta.toml"), code=set())
    with pytest.raises(PackageError, match="does not parse as JSON"):
        rewrite({"codemeta.json": b'{"x": Varek}'}, named(quoted, "codemeta.json"), code=set())


def test_a_label_that_holds_a_term_is_refused(tmp_path):
    path = anonymisation_file(tmp_path / "anon.toml", terms={"Varek": "Varek's group"})
    with pytest.raises(PackageError, match="label contains a term"):
        load_anonymisation(path, keep_out_of=[], model_names=set())


def named(listed: Anonymisation, *files: str) -> Anonymisation:
    return Anonymisation(listed.source, files, listed.terms)


ESCAPED_JSON = b'{"author": "\\u0056arek", "Varek": {"place": "Brindlemoor Institute"}, "names": ["Quillon Varek", 3]}'


def test_k03_a_named_json_file_is_anonymised_in_its_parsed_keys_and_strings_escapes_included(listed):
    """K03: the private names never enter the output: a JSON escape such as \\u0056 no longer hides a term from the
    rewrite, because JSON metadata is parsed, rewritten in its decoded keys and strings, and written out again."""
    files = rewrite({"codemeta.json": ESCAPED_JSON}, named(listed, "codemeta.json"), code=set())
    assert json.loads(files["codemeta.json"]) == {
        "author": "Author A",
        "Author A": {"place": "Institution A"},
        "names": ["Author A", 3],
    }
    assert find_terms(files, listed) == []


def test_k04_a_rewritten_json_file_is_written_out_the_same_way_every_time(listed):
    """K04: the rewrite keeps the file parseable and its numbers as they were, and its serialisation is fixed."""
    first = rewrite({"codemeta.json": ESCAPED_JSON}, named(listed, "codemeta.json"), code=set())["codemeta.json"]
    again = rewrite({"codemeta.json": first}, named(listed, "codemeta.json"), code=set())["codemeta.json"]
    assert first == again
    assert first == (json.dumps(json.loads(first), ensure_ascii=False, indent=2) + "\n").encode()


def test_k03_an_escaped_term_in_a_file_that_is_not_rewritten_is_found(listed):
    """K03: a record or any other file that the list does not name is searched in its decoded strings too."""
    findings = find_terms({"out/runs/x.json": b'{\n "author": "\\u0056arek",\n "n": 1\n}\n'}, listed)
    assert findings
    assert not any("varek" in finding.casefold() for finding in findings)


@pytest.mark.parametrize("hidden", ["&#86;arek", "&#x56;arek", "%56arek", "\\u0056arek", "&amp;#86;arek"])
def test_k03_an_escaped_term_left_in_named_markdown_fails_the_build_naming_the_file(listed, hidden):
    """K03: the rewrite cannot see a term written as an HTML entity, a percent-escape or a backslash escape, so the
    residual check reads the file decoded, finds it, and the build refuses, naming the file and the line."""
    files = rewrite({"README.md": f"# Notes\n\nBy {hidden}.\n".encode()}, named(listed, "README.md"), code=set())
    assert find_terms(files, listed) == [f"README.md:3: {FOUND}"]


def test_k03_toml_metadata_may_be_named_and_is_rewritten_as_text_then_checked(listed):
    """K03: TOML is rewritten as plain text and refused, naming the file, if its decoded view still holds a term; a
    rewritten TOML file must still parse (K04)."""
    plain = b'[project]\nauthors = [{name = "Quillon Varek"}]\n'
    files = rewrite({"pyproject.toml": plain}, named(listed, "pyproject.toml"), code=set())
    assert b"Author A" in files["pyproject.toml"]
    assert find_terms(files, listed) == []
    escaped = b'[project]\nauthors = [{name = "Quillon \\u0056arek"}]\n'
    files = rewrite({"pyproject.toml": escaped}, named(listed, "pyproject.toml"), code=set())
    assert any(finding.startswith("pyproject.toml") for finding in find_terms(files, listed))


@pytest.mark.parametrize("name", ["CITATION.cff", "meta.yaml", "meta.yml"])
def test_k04_yaml_cannot_be_named_because_its_rewrite_cannot_be_checked(listed, name):
    """K04: without a YAML parser the rewrite of a YAML file can be neither checked for its parse nor read decoded,
    so naming one is refused, saying how to ship it instead."""
    with pytest.raises(PackageError, match="YAML") as refusal:
        rewrite({name: b"authors:\n  - name: Quillon Varek\n"}, named(listed, name), code=set())
    assert "anonymised copy" in str(refusal.value)


def test_k03_a_term_split_by_a_continuation_or_escaped_in_code_is_found(listed):
    """K03: the term check reads every text file normalised as well: continuations joined, escapes decoded."""
    files = {"run.sh": b'echo "Quillon Var\\\nek"\n', "tool.py": b'AUTHOR = "\\x56arek"\n'}
    assert find_terms(files, listed) == [f"run.sh:1: {FOUND}", f"tool.py:1: {FOUND}"]
