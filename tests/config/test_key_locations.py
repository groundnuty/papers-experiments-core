"""Where a model's key lives is left out of the code identity and the campaign fingerprint, and nothing else (K13)."""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from expcore.config.key_locations import withhold_key_locations
from expcore.config.loading import ConfigError
from expcore.config.models import load_models
from expcore.provenance.codestamp import CodeScope, tree_hash
from expcore.study.config import load_study, read_study_identity
from expcore.study.pipeline import fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
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
