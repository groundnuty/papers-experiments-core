"""The sanitising import: map validation, token replacement, headers, blob ids and verification."""

import hashlib
from pathlib import Path

import pytest

from expcore.provenance import sanitise as sn
from expcore.provenance import study_tokens as st
from expcore.provenance.headers import read_header
from expcore.provenance.sanitise_map import MapError, parse_map

MAP_TEXT = """\
version = 1

[[entry]]
token = "fake-zone.example.net"
replacement = "host-1.example.test"
category = "host"

[[entry]]
token = "secret-space-extra"
replacement = "space-b"
category = "space"

[[entry]]
token = "secret-space"
replacement = "space-a"
category = "space"

[[entry]]
token = "secret_space"
replacement = "space_a"
category = "space"

[[entry]]
token = "zq"
replacement = "xx"
category = "option"

[[entry]]
token = "10.99.0.0/16"
replacement = "192.0.2.0/24"
category = "network"
"""

SOURCE = '''#!/usr/bin/env python3
"""Fetch from https://fake-zone.example.net:8443/api.

The second line names secret-space too.
"""
import os

ZONE_URL = "https://fake-zone.example.net:8443/api"
DATA = "/data/secret-space/in"
OTHER = "/data/Secret-Space-Extra/in"
NET = "10.99.0.0/16"
NOTE = "allow 10.99.0.0/16 here"
secret_space = 1
get_secret_space_path = secret_space  # zq-duplication of secret-space
MODE = "zq"
LABEL = 'zq-dup'
EMBEDDED = "supersecret-spaces"
GREETING = "héllo secret-space"
INDEX = "index"


def index(zq, *, mode=os.sep):
    return f"{zq}-secret-space-{mode}"
'''


@pytest.fixture
def map_file(tmp_path: Path) -> Path:
    path = tmp_path / "map.toml"
    path.write_text(MAP_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def token_map(map_file: Path):
    return parse_map(map_file.read_bytes(), str(map_file))


def hashes_of(*tokens: str) -> st.Hashes:
    return st.parse_hash_file(st.render_hash_file(tokens), "test")


# --- replacing tokens -----------------------------------------------------------------------------------------------


def test_hostname_inside_a_url_is_replaced(token_map):
    result = sn.sanitise_python('url = "https://fake-zone.example.net:8443/api"\n', token_map.entries)
    assert result == 'url = "https://host-1.example.test:8443/api"\n'


def test_space_name_inside_a_path_is_replaced(token_map):
    assert sn.sanitise_python('p = "/data/secret-space/in"\n', token_map.entries) == 'p = "/data/space-a/in"\n'


def test_identifiers_are_replaced_whole_and_as_components(token_map):
    source = "secret_space = 1\nget_secret_space_path = secret_space\n"
    assert sn.sanitise_python(source, token_map.entries) == "space_a = 1\nget_space_a_path = space_a\n"


def test_comments_and_docstrings_are_sanitised(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert "# zq-duplication of space-a" in result
    assert "The second line names space-a too." in result
    assert "https://host-1.example.test:8443/api." in result


def test_short_token_leaves_longer_words_alone_but_replaces_the_whole_literal(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert 'MODE = "xx"' in result
    assert "# zq-duplication" in result
    assert "LABEL = 'zq-dup'" in result
    assert 'INDEX = "index"' in result
    assert "def index(xx, *, mode=os.sep):" in result


def test_cidr_literal_is_replaced_only_as_a_whole_literal(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert 'NET = "192.0.2.0/24"' in result
    assert 'NOTE = "allow 10.99.0.0/16 here"' in result


def test_longest_token_wins_and_matching_ignores_case(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert 'OTHER = "/data/space-b/in"' in result


def test_tokens_inside_a_longer_component_are_left_alone(token_map):
    assert 'EMBEDDED = "supersecret-spaces"' in sn.sanitise_python(SOURCE, token_map.entries)


def test_fstring_parts_and_non_ascii_literals_are_sanitised_in_place(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert 'GREETING = "h\u00e9llo space-a"' in result
    assert '    return f"{xx}-space-a-{mode}"' in result


def test_only_the_replaced_characters_change(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    assert result.count("\n") == SOURCE.count("\n")
    assert result.startswith("#!/usr/bin/env python3\n")
    assert sn.sanitise_python(SOURCE, ()) == SOURCE


def test_non_ascii_text_before_a_token_does_not_shift_the_replacement(token_map):
    source = 'x = ("ééé", "secret-space")  # ü secret-space\n'
    assert sn.sanitise_python(source, token_map.entries) == ('x = ("ééé", "space-a")  # ü space-a\n')


def test_result_compiles_and_the_checker_finds_no_listed_token(token_map):
    result = sn.sanitise_python(SOURCE, token_map.entries)
    compile(result, "<result>", "exec")
    hashes = hashes_of(*(e.token for e in token_map.entries))
    assert st.scan_source("src.py", SOURCE, hashes)
    # NOTE embeds the CIDR in a longer sentence, where it can match only as an entire literal.
    assert st.scan_source("src.py", result, hashes) == []


def test_replacement_that_is_not_an_identifier_is_refused_with_file_and_line(token_map):
    with pytest.raises(sn.SanitiseError, match=r"mod\.py:2: map entry #1 .*must itself be an identifier"):
        sn.sanitise_python("x = 1\nsecret_space = 1\n", _entries("secret_space", "space-a"), filename="mod.py")


def test_an_identifier_error_names_the_entry_by_number_and_replacement_never_the_token():
    """P07: the map is private, so a refusal names the entry by its number and its replacement, never its token."""
    entries = parse_map(_map_with(("brindle-gate", "gate-a"), ("brindle_gate", "gate-b"), ("other_gate", "gate-c")))
    with pytest.raises(sn.SanitiseError) as refused:
        sn.sanitise_python("x = 1\nother_gate = 1\n", entries.entries, filename="mod.py")
    message = str(refused.value)
    assert message.startswith("mod.py:2: map entry #3 (category 'c'): its replacement 'gate-c' is used inside")
    assert "must itself be an identifier" in message
    assert "other_gate" not in message


@pytest.mark.parametrize(
    ("entries", "shown"),
    [
        ([("brindle-gate", "gate-a"), ("BRINDLE-GATE", "gate-b")], "entry #2 (category 'c', replacement 'gate-b')"),
        ([("brindle-gate", 'gate"a')], "entry #1 (category 'c', replacement 'gate\"a')"),
        ([("brindle-gate", "gate-a"), ("gate-b", "x-brindle-gate")], "entry #2 (category 'c')"),
        ([("brindle-gate", "BRINDLE-GATE")], "entry #1 (category 'c')"),
    ],
)
def test_a_map_error_names_the_entry_by_number_and_replacement_never_the_token(entries, shown):
    """P07: a map refusal names the entry by number and its replacement, which is public, and leaves out a replacement
    that holds a mapped token; the token never appears."""
    with pytest.raises(MapError) as refused:
        parse_map(_map_with(*entries), "map.toml")
    message = str(refused.value)
    assert f"map.toml: {shown}: " in message
    assert "brindle" not in message.casefold()


def test_a_replacement_that_breaks_the_syntax_is_reported_with_the_file_name():
    with pytest.raises(sn.SanitiseError, match=r"mod\.py: the sanitised source does not compile"):
        sn.sanitise_python("fakename = 1\n", _entries("fakename", "class"), filename="mod.py")


def test_source_that_does_not_compile_is_reported_with_the_file_name(token_map):
    with pytest.raises(sn.SanitiseError, match=r"broken\.py: the source does not compile"):
        sn.sanitise_python("def (:\n", token_map.entries, filename="broken.py")


def _entries(token: str, replacement: str):
    return parse_map(
        f'version = 1\n[[entry]]\ntoken = "{token}"\nreplacement = "{replacement}"\ncategory = "c"\n'.encode()
    ).entries


# --- the map --------------------------------------------------------------------------------------------------------


def _map_with(*entries: tuple[str, str]) -> bytes:
    body = "".join(f"[[entry]]\ntoken = '{t}'\nreplacement = '{r}'\ncategory = 'c'\n\n" for t, r in entries)
    return ("version = 1\n\n" + body).encode()


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("alpha-one", "beta-one"), ("ALPHA-ONE", "beta-two")], r"entry #2 .*token repeats entry #1"),
        ([("alpha-one", "beta-one"), ("alpha-two", "Beta-One")], r"entry #2 .*replacement repeats entry #1"),
        ([("alpha-one", "alpha-one")], r"entry #1 .*replacement equals a mapped token"),
        ([("alpha-one", "ALPHA-ONE")], r"entry #1 .*replacement equals a mapped token"),
        ([("alpha-one", "beta-one"), ("beta-one", "gamma-one")], r"entry #1 .*replacement equals a mapped token"),
        ([("alpha", "x-alpha-y")], r"entry #1 .*contains a mapped token on a component boundary"),
        ([("alpha-one", 'bad"quote')], r"entry #1 .*quotes, backslashes"),
    ],
)
def test_invalid_maps_name_the_entry(entries, message):
    with pytest.raises(MapError, match=message):
        parse_map(_map_with(*entries), "map.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[entry]]\ntoken='a'\n", r"version = 1"),
        ("version = 2\n", r"version = 1"),
        (
            "version = 1\n[[entry]]\ntoken = 'alpha-one'\nreplacement = 'beta-one'\n",
            r"entry #1: missing keys \['category'\]",
        ),
        ("version = 1\n[[entry]]\ntoken='a'\nreplacement='b'\ncategory='c'\nextra=1\n", r"entry #1: unknown keys"),
        ("version = 1\n[[entry]]\ntoken=''\nreplacement='b'\ncategory='c'\n", r"entry #1: 'token' must be"),
        ("version = [", r"not valid TOML"),
    ],
)
def test_malformed_maps_are_refused(text, message):
    with pytest.raises(MapError, match=message):
        parse_map(text.encode(), "map.toml")


def test_the_map_hash_is_the_sha256_of_the_file(map_file, token_map):
    assert token_map.sha256 == hashlib.sha256(map_file.read_bytes()).hexdigest()


# --- import and verify ----------------------------------------------------------------------------------------------


def run_import(map_file: Path, src: Path, dest: Path, source_repo: str = "fake-study-repo") -> int:
    return sn.main(
        [
            "import",
            "--map",
            str(map_file),
            "--source-repo",
            source_repo,
            "--commit",
            "0123abcd",
            "--path",
            "pkg/capping.py",
            str(src),
            str(dest),
        ]
    )


def test_import_writes_the_header_after_the_shebang_and_prints_the_map_hash(repo, map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_bytes(SOURCE.encode("utf-8"))
    dest = tmp_path / "out" / "mod.py"
    dest.parent.mkdir()
    assert run_import(map_file, src, dest) == 0
    map_sha = hashlib.sha256(map_file.read_bytes()).hexdigest()
    assert capsys.readouterr().out.strip() == map_sha

    text = dest.read_text(encoding="utf-8")
    start = "#!/usr/bin/env python3\n# Provenance:\n#   source: fake-study-repo @ 0123abcd, pkg/capping.py, blob "
    assert text.startswith(start)
    header = read_header(text)
    assert header is not None
    assert header.map_sha256 == map_sha
    assert header.blob == repo.git("hash-object", str(src))
    compile(text, str(dest), "exec")


def test_blob_id_matches_git_hash_object_for_awkward_content(repo, tmp_path):
    for name, data in {"empty": b"", "binary": b"\x00\xff\r\n\x01", "text": "héllo\n".encode()}.items():
        file = tmp_path / name
        file.write_bytes(data)
        assert sn.blob_id(data) == repo.git("hash-object", str(file))


def test_import_refuses_to_overwrite_an_existing_file(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text(SOURCE, encoding="utf-8")
    dest = tmp_path / "mod.py"
    dest.write_text("keep me\n", encoding="utf-8")
    assert run_import(map_file, src, dest) == 2
    assert "already exists" in capsys.readouterr().err
    assert dest.read_text(encoding="utf-8") == "keep me\n"


def test_import_rejects_a_bad_commit_and_a_bad_map(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text("x = 1\n", encoding="utf-8")
    bad_commit = ["import", "--map", str(map_file), "--source-repo", "r", "--commit", "HEAD", "--path", "p.py"]
    assert sn.main([*bad_commit, str(src), str(tmp_path / "a.py")]) == 2
    broken = tmp_path / "broken.toml"
    broken.write_text("version = 3\n", encoding="utf-8")
    assert run_import(broken, src, tmp_path / "b.py") == 2
    assert capsys.readouterr().err.count("error:") == 2
    assert not (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()


def verify(map_file: Path, src: Path, dest: Path) -> int:
    return sn.main(["verify", "--map", str(map_file), "--source", str(src), "--module", str(dest)])


def test_verify_passes_on_a_fresh_import_and_fails_after_a_manual_edit(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text(SOURCE, encoding="utf-8")
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 0
    assert verify(map_file, src, dest) == 0

    dest.write_text(dest.read_text(encoding="utf-8").replace("space-a", "space-c", 1), encoding="utf-8")
    capsys.readouterr()
    assert verify(map_file, src, dest) == 1
    assert "body differs from the sanitised source, first at line" in capsys.readouterr().out


def test_verify_fails_when_the_map_or_the_source_changed_since_the_import(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text(SOURCE, encoding="utf-8")
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest) == 0
    other_map = tmp_path / "other.toml"
    extra = '\n[[entry]]\ntoken = "unrelated-name"\nreplacement = "other-name"\ncategory = "x"\n'
    other_map.write_text(MAP_TEXT + extra)
    assert verify(other_map, src, dest) == 1
    assert "map hash does not match" in capsys.readouterr().out
    src.write_text(SOURCE + "# more\n", encoding="utf-8")
    assert verify(map_file, src, dest) == 1
    assert "blob id does not match" in capsys.readouterr().out


def test_verify_refuses_a_module_without_a_source_header(map_file, tmp_path, capsys):
    src = tmp_path / "orig.py"
    src.write_text("x = 1\n", encoding="utf-8")
    dest = tmp_path / "mod.py"
    dest.write_text("# Provenance: new\nx = 1\n", encoding="utf-8")
    assert verify(map_file, src, dest) == 1
    assert "not a sanitised import" in capsys.readouterr().out


def test_an_import_passes_the_study_token_check_when_its_header_names_a_token(map_file, tmp_path):
    src = tmp_path / "orig.py"
    src.write_text(SOURCE, encoding="utf-8")
    dest = tmp_path / "mod.py"
    assert run_import(map_file, src, dest, source_repo="secret-space") == 0
    hashes = hashes_of("secret-space", "fake-zone.example.net", "secret_space")
    assert st.scan_source(dest, dest.read_text(encoding="utf-8"), hashes) == []
