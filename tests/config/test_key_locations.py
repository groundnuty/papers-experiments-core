"""Where a model's key lives is left out of the code identity and the campaign fingerprint, and nothing else (K13)."""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from expcore.config.key_locations import hashed_form, withhold_key_locations
from expcore.config.loading import ConfigError
from expcore.config.models import load_models
from expcore.provenance.codestamp import CodeScope, tree_hash
from expcore.study.config import load_study
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
    assert withhold_key_locations(withheld) == withheld == hashed_form(table)
    plain = b'[models.alpha]\nbackend = "stub"\n'
    assert withhold_key_locations(plain) is plain
    assert hashed_form(b"not = [toml") == b"not = [toml"
