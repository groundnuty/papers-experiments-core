"""Where a model's key lives is left out of the code identity and the campaign fingerprint, and nothing else (K13)."""

import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from expcore.config.key_locations import location_finders, withhold_key_locations
from expcore.config.loading import ConfigError
from expcore.config.models import load_models
from expcore.package.leaks import find_locations
from expcore.provenance.codestamp import CodeScope, tree_hash
from expcore.study.config import load_study, read_study_identity
from expcore.study.pipeline import fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
HOME = os.path.expanduser("~")  # a key file declared under this machine's home is also written with `~`
KEYED = """
[models.delta-remote]  # a model the plan runs, its key in a file
backend = "anthropic"
api_id = "delta-1"
base_url = "https://api.example.invalid"
key_file = "~/keys/old-folder/delta.key"
status = "draft"

[models.delta-remote.extra_body]
key_env = "sent-to-the-endpoint"  # a vendor extra the request carries, not a key location

[models.gamma-remote]
backend = "openai_compat"
api_id = "gamma-1"
base_url = "https://llm.example.invalid/v1"
key_env = "OLD_GAMMA_KEY"
status = "draft"
"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def study_dir(tmp_path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def identity(root: Path) -> tuple[str, str, dict]:
    """The tree hash, models.toml's hash in the fingerprint, and the fingerprint."""
    study = load_study(root)
    fingerprint = fingerprint_of(study, load_plugin(StudyContext(study, {"commit": "a" * 40})))
    return tree_hash(root, study.scope), study.config_sha256["models.toml"], fingerprint


def edit(root: Path, old: str, new: str) -> None:
    path = root / "models.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_k13_a_table_with_no_key_location_keeps_the_identity_and_fingerprint_it_always_had(study_dir):
    """K13: the example names no key location, so its tree hash, models.toml's hash and each model's digest are
    exactly what hashing everything as it is gives, and its campaigns, golden records and pinned outputs stand; with a
    key location, the tree hash and the file's hash are no longer the file's as it is."""
    study = load_study(study_dir)
    raw = (study_dir / "models.toml").read_bytes()
    tree, models_sha, fingerprint = identity(study_dir)
    assert tree == tree_hash(study_dir, CodeScope.of(study.scope.roots))
    assert models_sha == hashlib.sha256(raw).hexdigest()
    assert fingerprint["models"] == {
        k: digest(dataclasses.asdict(study.models.resolve(k))) for k in study.campaign.models
    }
    (study_dir / "models.toml").write_bytes(raw + KEYED.encode())
    keyed_tree, keyed_sha, _ = identity(study_dir)
    assert keyed_tree != tree_hash(study_dir, CodeScope.of(study.scope.roots))
    assert keyed_sha != hashlib.sha256((study_dir / "models.toml").read_bytes()).hexdigest()


def test_k13_only_where_a_key_lives_leaves_the_identity_and_the_fingerprint(study_dir):
    """K13: moving a key file or renaming a key variable changes neither the tree hash, nor models.toml's hash, nor the
    digest of a model the plan runs; any other byte of the file still does: an API id, a comment, and a vendor extra
    that happens to be called key_env, since the request carries it."""
    with (study_dir / "models.toml").open("a", encoding="utf-8") as table:
        table.write(KEYED)
    (study_dir / "campaign.toml").write_text(
        (study_dir / "campaign.toml")
        .read_text(encoding="utf-8")
        .replace('models = ["alpha-stub", "beta-stub"]', 'models = ["alpha-stub", "beta-stub", "delta-remote"]'),
        encoding="utf-8",
    )
    before = identity(study_dir)
    assert "delta-remote" in before[2]["models"]
    edit(study_dir, "~/keys/old-folder/delta.key", "/srv/elsewhere/delta.key")
    edit(study_dir, '"OLD_GAMMA_KEY"', '"NEW_GAMMA_KEY"')
    assert identity(study_dir) == before
    for old, new in (
        ('api_id = "delta-1"', 'api_id = "delta-2"'),
        ("# a model the plan runs", "# a model the plan still runs"),
        ('"sent-to-the-endpoint"', '"sent-elsewhere"'),
    ):
        edit(study_dir, old, new)
        changed = identity(study_dir)
        assert changed[0] != before[0], new
        assert changed[1] != before[1], new
        edit(study_dir, new, old)
    assert identity(study_dir) == before


@pytest.mark.parametrize(
    ("table", "where"),
    [
        (
            '[models]\ndelta = { backend = "anthropic", api_id = "d", base_url = "https://a.example.invalid", '
            'key_env = "K" }\n',
            "[models.delta]: key 'key_env'",
        ),
        (
            '[models]\ndelta.backend = "anthropic"\ndelta.api_id = "d"\ndelta.base_url = "https://a.example.invalid"\n'
            'delta.key_file = "/k"\n',
            "[models.delta]: key 'key_file'",
        ),
        (
            '[models.delta]\nbackend = "anthropic"\napi_id = "d"\nbase_url = "https://a.example.invalid"\n'
            'key_file = """/k"""\n',
            "[models.delta]: key 'key_file'",
        ),
    ],
    ids=["inline table", "dotted key", "multi-line string"],
)
def test_k13_a_key_location_a_package_could_not_withhold_is_refused_when_the_table_is_loaded(tmp_path, table, where):
    """K13: a key location written where the rewrite cannot reach it would reach every package, so loading the table
    refuses it, naming the file, the model and the key, never the value, and saying how to write it."""
    path = tmp_path / "models.toml"
    path.write_text(table, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_models(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: {where} cannot be withheld from a replication package")
    assert 'write it on a line of its own in the model\'s table, as key_file = "..."' in message
    assert "/k" not in message
    assert '"K"' not in message


def test_k13_the_rewrite_changes_only_the_key_values_and_is_its_own_canonical_form():
    """K13: each key value becomes its placeholder, keeping the key's quoting, the spacing, the comment and the line
    ending; a value under a model's sub-table is not a key location and stays; the result rewrites to itself, so a
    package's file hashes as the author's; a table with no key location comes back as the very same bytes."""
    table = (
        b'[models."delta.v1"]\r\nbackend = "anthropic"\r\n"key_file"  =  \'C:\\keys\\d.key\'   # where it lives\r\n'
        b'[models."delta.v1".extra_body]\r\nkey_file = "an extra"\r\n'
        b'[models.gamma]\nbackend = "openai_compat"\nkey_env = "GAMMA\\u005fKEY"\n'
    )
    withheld = withhold_key_locations(table)
    assert withheld == (
        b'[models."delta.v1"]\r\nbackend = "anthropic"\r\n"key_file"  =  "<withheld>"   # where it lives\r\n'
        b'[models."delta.v1".extra_body]\r\nkey_file = "an extra"\r\n'
        b'[models.gamma]\nbackend = "openai_compat"\nkey_env = "KEY_WITHHELD"\n'
    )
    assert withhold_key_locations(withheld) == withheld
    plain = b'[models.alpha]\nbackend = "stub"\n'
    assert withhold_key_locations(plain) is plain


FOUND = [
    ("key.txt", 'key_file = "key.txt"'),
    ("key.txt", "`key.txt`"),
    ("key.txt", "cat /srv/old/key.txt"),
    ("secrets", "'secrets'"),
    ("/srv/keys/a.key", "lives in /srv/keys/a.key."),
    ("/srv/keys/a.key", "`/srv/keys/a.key`"),
    ("/srv/keys/a.key", "KEY=/srv/keys/a.key\n"),
    ("/srv/keys/a.key", "    - /srv/keys/a.key:/run/key:ro"),  # followed by a colon
    ("/srv/keys/a.key", "docker run -v /srv/keys/a.key:/key image"),
    ("/srv/keys/a.key", "/srv/keys/a.key:12: the line that names it"),
    ("~/keys/a.key", "    - ~/keys/a.key:/run/key:ro"),
    ("/srv/keys/a.key", "file:///srv/keys/a.key"),  # a file URL
    ("keys/lab.key", "the key is at /srv/study/keys/lab.key"),  # a relative path inside a longer one
    ("old-study/keys/lab.key", "see /srv/old-study/keys/lab.key"),
    ("keys/lab.key", "file:///srv/study/keys/lab.key"),
    ("/srv/keys/a.key", "**/srv/keys/a.key**"),  # any character that cannot continue a path's part
    ("/srv/keys/a.key", "[/srv/keys/a.key](notes.md)"),
    ("/srv/keys/a.key", "e.g. /srv/keys/a.key..."),
    ("/srv/keys/a.key", "scp lab:/srv/keys/a.key ."),
    # other spellings of the path a declared home-relative or ./-relative key file has
    ("~/prior-study/lab.key", "    - ${HOME}/prior-study/lab.key:/run/key:ro"),
    ("~/prior-study/lab.key", "cat $HOME/prior-study/lab.key"),
    ("~/prior-study/lab.key", "error: /home/someone/prior-study/lab.key missing"),  # another host's home
    (f"{HOME}/prior-study/lab.key", "put it in ~/prior-study/lab.key"),
    (f"{HOME}/prior-study/lab.key", "- ${HOME}/prior-study/lab.key:/run/key:ro"),
    ("./keys/lab.key", "cat keys/lab.key"),
    ("./keys/lab.key", "cat ./keys/lab.key"),
    # a tail that is one bare identifier is found only behind a home directory
    ("~/key", "cat ~/key"),
    ("~/key", "cat $HOME/key"),
    ("~/key", "    - ${HOME}/key:/run/key:ro"),
    ("~/key", "error: /home/someone/key missing"),
    ("~/key", "see /Users/someone/key"),
    ("~/key", "ls /root/key"),
    ("~/key", "cat ~bob/key"),
    ("~/OPENAI_API_KEY", "export F=$HOME/OPENAI_API_KEY"),
    (f"{HOME}/key", "cat $HOME/key"),
    ("/home/someone/key", "cat ~/key"),
    # a bare file name with a full stop or a hyphen is a path of one part: found wherever no part continues it
    ("prior-study.key", "__pycache__/\nprior-study.key\n"),  # a .gitignore line
    ("prior-study.key", "[k](prior-study.key)"),
    ("prior-study.key", "KEY_FILE=prior-study.key"),
    ("lab.key", "x?f=lab.key"),
    ("prior-study.key", "Put the key in prior-study.key first."),
    ("key.txt", "copy key.txt here"),
    ("./prior-study.key", "cat prior-study.key"),
    ("~/prior-study.key", "cat prior-study.key"),
]
NOT_FOUND = [
    ("key.txt", "my-key.txt"),
    ("key.txt", "/srv/old/my-key.txt"),
    ("key.txt", "KEY.TXT"),
    ("secrets", "import secrets"),
    ("secrets", "secrets.token_hex()"),
    ("/srv/keys/a.key", "/srv/keys/a.keyring"),
    ("/srv/keys/a.key", "/srv/keys/a.key.bak"),
    ("/srv/keys/a.key", "/srv/keys/a.key/old"),
    ("/srv/keys/a.key", "/mnt/srv/keys/a.key"),
    ("/srv/keys/a.key", "/SRV/keys/a.key"),
    ("keys/lab.key", "/srv/study/mykeys/lab.key"),
    ("keys/lab.key", "keys/lab.key-old"),
    ("~/prior-study/lab.key", "/home/someone/old-prior-study/lab.key"),
    ("~/prior-study/lab.key", "$HOME/prior-study/lab.key.bak"),
    ("./keys/lab.key", "cat mykeys/lab.key"),
    ("prior-study.key", "old-prior-study.key"),
    ("prior-study.key", "prior-study.key.bak"),
    ("prior-study.key", "prior-study.keys"),
    ("prior-study.key", "PRIOR-STUDY.KEY"),
    # a tail that is one bare identifier is an ordinary word on its own: a key of a dict, a field, a variable
    ("~/key", 'd["key"]'),
    ("~/token", '{"token": t}'),
    ("~/OPENAI_API_KEY", 'os.environ["OPENAI_API_KEY"]'),
    ("/Users/someone/key", "sorted(rows, key=len)\nrow = {'key': 1}"),
    ("~/key", "/home/someone/keys"),
    ("~/key", "/home/someone/key/old"),
    ("~/key", "$HOME/mykey"),
]


def names_key_file(key_file: str, text: str) -> bool:
    finders = location_finders([SimpleNamespace(key_file=key_file, key_env=None)])
    return bool(find_locations({"notes.txt": text.encode("utf-8")}, finders))


@pytest.mark.parametrize(("key_file", "text"), FOUND, ids=[text for _, text in FOUND])
def test_k13_a_key_file_is_found_wherever_no_path_part_continues_it(key_file, text):
    """K13: a key file's path is found as written, without a leading `./`, and by its tail after the home directory
    (`~/`, or this machine's home), wherever no part of a path continues it: after the start, white space, a quote, a
    bracket, `=`, `:` or `/` (so inside a longer path, in a file URL, after `$HOME` or another host's home), and
    before the end, white space, a quote, a bracket, `:` (a mount, a line number), `,`, `;`, or a full stop that ends
    the text there. A bare file name with a full stop or a hyphen is a path of one part and found the same way,
    prose included; one that is an identifier (`secrets`) is found as a whole part of a path, or quoted."""
    assert names_key_file(key_file, text)


@pytest.mark.parametrize(("key_file", "text"), NOT_FOUND, ids=[text for _, text in NOT_FOUND])
def test_k13_a_key_file_is_not_found_inside_a_longer_name(key_file, text):
    """K13: a key file's path is found case-sensitively, never continued by a part of a longer name, and a bare
    identifier is an ordinary word unless quoted or a part of a path. So neither `my-key.txt` nor `import secrets`
    refuses a build whose key file is `key.txt` or `secrets`."""
    assert not names_key_file(key_file, text)


ANY_HOME = [
    ("/Users/someone/prior-study/lab.key", "cat ~/prior-study/lab.key"),
    ("/Users/someone/prior-study/lab.key", "cat $HOME/prior-study/lab.key"),
    ("/Users/someone/prior-study/lab.key", "error: /home/other/prior-study/lab.key missing"),
    ("/home/someone/prior-study/lab.key", "cat ~/prior-study/lab.key"),
    ("/root/prior-study/lab.key", "cat ${HOME}/prior-study/lab.key"),
    ("/Users/someone/lab.key", "cat ~/lab.key"),
    ("/Users/someone/key", "cat ~/key"),
]


@pytest.mark.parametrize(("key_file", "text"), ANY_HOME, ids=[f"{k} in {t}" for k, t in ANY_HOME])
def test_k13_a_key_file_is_found_by_its_tail_under_any_home_not_only_this_machines(
    monkeypatch, tmp_path, key_file, text
):
    """K13: the tail of a declared absolute key file after `/home/<name>/`, `/Users/<name>/` or `/root/` is searched
    whatever this machine's home is, so a study declared on one machine and built on another is still refused when a
    file writes the path from its own home, from `~`, `$HOME` or another user's."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert names_key_file(key_file, text)


def test_k13_a_key_file_declared_under_another_home_is_not_found_in_another_folder(monkeypatch, tmp_path):
    """K13: the tail is a whole relative path: a longer folder name does not hold it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not names_key_file("/Users/someone/prior-study/lab.key", "cat ~/old-prior-study/lab.key")


MODEL = '[models.a]\nbackend = "anthropic"\napi_id = "a-1"\nbase_url = "https://a.example.invalid"\n'


@pytest.mark.parametrize(
    "vendor",
    [
        "key_file = '<withheld>'",  # already the placeholder's value, in another quoting: no byte of it may change
        'key_file = "/vendor/item"',
        "key_env = 'VENDOR_ITEM'",
    ],
)
def test_k13_a_key_field_under_an_array_of_tables_is_a_vendor_value_and_never_rewritten(vendor):
    """K13: an array-of-tables header ends the model's table, so a `key_file` or `key_env` of an item under
    `[[models.a.extra_body.items]]`, a value the request carries, keeps every byte, even one that already reads as the
    placeholder; only the model's own key location is replaced."""
    table = f'{MODEL}key_file = "/real/a.key"\n[[models.a.extra_body.items]]\n{vendor}\n'.encode()
    assert withhold_key_locations(table) == table.replace(b'"/real/a.key"', b'"<withheld>"')


@pytest.mark.parametrize(
    "value",
    [
        '"""\nkey_env = "INSIDE"\n"""',  # a line inside a multi-line basic string
        "'''\nkey_file = '/inside'\n'''",  # and inside a multi-line literal one
        '"""first \\\nkey_env = "INSIDE"\n"""',  # after a line-ending backslash, which escapes the newline
        '"""\nkey_env = "INSIDE"\nx"""""',  # closing on five quotes, two of them the string's own
    ],
    ids=["basic", "literal", "escaped newline", "five quotes"],
)
def test_k13_a_line_inside_a_multi_line_string_is_never_rewritten(value):
    """K13: a line that reads like a key location but lies inside a multi-line string is part of that string's value,
    so it keeps every byte; the model's own key location after the string is replaced."""
    table = (MODEL.replace('"a-1"', value) + 'key_env = "REAL_KEY"\n').encode()
    assert withhold_key_locations(table) == table.replace(b'"REAL_KEY"', b'"KEY_WITHHELD"')


def test_k13_a_models_toml_that_is_not_toml_as_its_bytes_are_is_refused_at_load_and_never_hashed(study_dir):
    """K13: the table is loaded from the very bytes that are rewritten and hashed, so a bare carriage return, which a
    reading as text would turn into a newline, is refused when the table is loaded and when the identity is hashed,
    naming the file and never the key location; it is never hashed, run or shipped as it is."""
    keyed = MODEL.replace('base_url = "https://a.example.invalid"\n', 'base_url = "https://a.example.invalid"\r')
    raw = (study_dir / "models.toml").read_bytes() + (keyed + 'key_file = "/srv/hidden/a.key"\n').encode()
    (study_dir / "models.toml").write_bytes(raw)
    with pytest.raises(ConfigError) as loading:
        load_models(study_dir / "models.toml")
    with pytest.raises(ConfigError) as hashing:
        tree_hash(study_dir, read_study_identity(study_dir).scope)
    assert str(loading.value).startswith(f"{study_dir / 'models.toml'}: ")
    assert str(hashing.value).startswith("models.toml: cannot be read as a TOML table as its bytes are")
    assert "/srv/hidden" not in str(loading.value) + str(hashing.value)
    with pytest.raises(ConfigError, match="cannot be read as a TOML table"):
        withhold_key_locations(b"not = [toml")


@pytest.mark.parametrize(
    ("key", "searched"),
    [
        (b"0123456789abcdef", [b"0123456789abcdef"]),
        (b"0123456789abcdef\n", [b"0123456789abcdef", b"0123456789abcdef\n"]),
        (b"  0123456789abcdef  \n", [b"  0123456789abcdef  \n", b"0123456789abcdef"]),
        (b"0123456789abcde", []),
        (b"0123456789abcde\n", [b"0123456789abcde\n"]),  # 16 bytes as it stands, 15 stripped
        (b"abcdefghijkl\n", []),
    ],
    ids=["16 bytes", "newline-ended", "padded", "15 bytes", "15 and a newline", "short"],
)
def test_k13_a_key_file_is_searched_for_inside_other_bytes_from_16_bytes_up_and_never_printed(tmp_path, key, searched):
    """K13: a key file's content is looked for inside other bytes as it is and with the white space around it
    stripped, each only from 16 bytes up (a shorter run occurs by chance); no representation of the finders holds the
    key."""
    path = tmp_path / "vault" / "lab.secret"
    path.parent.mkdir()
    path.write_bytes(key)
    finders = location_finders([SimpleNamespace(key_file=str(path), key_env=None, key="gamma")])
    assert sorted(finders.needles) == sorted(searched)
    assert finders.holds_key_file(b"\0" + key + b"\0") == bool(searched)
    assert key.strip().decode() not in repr(finders)
    assert "needles" not in repr(finders)
