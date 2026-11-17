"""O75: a study names the factor that is its data path in `[study] data_path_factor`, the one place the core reads it
from; without the key the name is `data_path`, and a study that does not set it is exactly as it was."""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from expcore.campaign.factors import DATA_PATH_FACTOR
from expcore.campaign.manifest import compare_fingerprints
from expcore.config.loading import ConfigError
from expcore.study.config import check_study, load_study
from expcore.study.pipeline import fingerprint_of
from expcore.study.plugin import StudyContext, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
KEY = 'data_path_factor = "path"\n'


@pytest.fixture
def study_dir(tmp_path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def edit(study: Path, old: str, new: str) -> None:
    path = study / "study.toml"
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def with_path_factor(study: Path, name: str = "path", key: str | None = KEY) -> None:
    """Give the copy a factor `name` with the levels `A+` and `M`, used by two of its conditions, and `key` in
    `[study]` (None: no key)."""
    edit(study, "[identity]", f'[factors.{name}]\nlevels = ["A+", "M"]\n\n[identity]')
    edit(study, '"tools", "model"', f'"tools", "{name}", "model"')
    edit(study, 'access = "direct"\ntools = "basic"\n', f'access = "direct"\ntools = "basic"\n{name} = "M"\n')
    edit(study, 'access = "direct"\ntools = "extended"\n', f'access = "direct"\ntools = "extended"\n{name} = "M"\n')
    edit(study, 'access = "relay"\ntools = "basic"\n', f'access = "relay"\ntools = "basic"\n{name} = "A+"\n')
    if key is not None:
        edit(study, 'plugin = "plugin.py"\n', f'plugin = "plugin.py"\n{key}')


def fingerprint(study_dir: Path) -> dict:
    study = load_study(study_dir)
    return fingerprint_of(study, load_plugin(StudyContext(study, {"commit": "0" * 40})))


def test_o75_a_study_that_sets_no_key_reads_its_data_path_from_the_factor_data_path(study_dir):
    assert DATA_PATH_FACTOR == "data_path"
    assert load_study(study_dir).data_path_factor == "data_path"


def test_o75_a_study_names_its_data_path_factor_in_the_study_table(study_dir):
    """The key is a declared factor of the study, whatever the study calls it."""
    with_path_factor(study_dir)
    study = load_study(study_dir)
    assert study.data_path_factor == "path"
    assert "path" in study.schema.declared


def test_o75_a_study_with_the_default_name_may_say_it_or_not(study_dir):
    with_path_factor(study_dir, "data_path", 'data_path_factor = "data_path"\n')
    assert load_study(study_dir).data_path_factor == "data_path"


@pytest.mark.parametrize(
    ("key", "fragment"),
    [
        ('data_path_factor = "elsewhere"\n', "names no declared factor"),
        ('data_path_factor = "task"\n', "names no declared factor"),  # built in: not a factor a study declares
        ('data_path_factor = "data_path"\n', "names no declared factor"),  # said, so it must be declared
        ("data_path_factor = 3\n", "must be a non-empty string"),
        ('data_path_factor = ""\n', "must be a non-empty string"),
    ],
)
def test_o75_a_key_that_names_no_declared_factor_is_refused_naming_the_file_the_table_and_the_key(
    study_dir, key, fragment
):
    edit(study_dir, 'plugin = "plugin.py"\n', f'plugin = "plugin.py"\n{key}')
    with pytest.raises(ConfigError) as raised:
        load_study(study_dir)
    text = str(raised.value)
    assert "study.toml" in text
    assert "[study]" in text
    assert "data_path_factor" in text
    assert fragment in text


def test_o75_the_refusal_lists_the_factors_the_study_declares(study_dir):
    edit(study_dir, 'plugin = "plugin.py"\n', 'plugin = "plugin.py"\ndata_path_factor = "elsewhere"\n')
    with pytest.raises(ConfigError, match="access, tools"):
        load_study(study_dir)


def test_o75_a_study_built_in_code_is_held_to_the_same_name(study_dir):
    with_path_factor(study_dir)
    study = load_study(study_dir)
    check_study(study)
    with pytest.raises(ConfigError, match="names no declared factor"):
        check_study(dataclasses.replace(study, data_path_factor="elsewhere"))


def test_o75_the_key_adds_nothing_to_the_campaign_fingerprint_but_the_digest_of_the_file_it_is_in(study_dir, tmp_path):
    """The same factors with and without the key fingerprint alike but for the digest of study.toml, which holds the key
    like any setting; a study that sets no key has the entries and the digests it had."""
    other = tmp_path / "other"
    shutil.copytree(study_dir, other)
    with_path_factor(study_dir, key=None)
    with_path_factor(other)
    without, named = fingerprint(study_dir), fingerprint(other)
    assert "data_path_factor" not in json.dumps([without, named])
    assert set(without) == set(named)
    assert {k: v for k, v in without.items() if k != "configs"} == {k: v for k, v in named.items() if k != "configs"}
    assert set(without["configs"]) == set(named["configs"])
    assert {k for k in named["configs"] if named["configs"][k] != without["configs"][k]} == {"study.toml"}
    comparison = compare_fingerprints(without, named)
    assert [leaf.split(":")[0] for leaf in comparison.refused] == ["configs.study.toml"]
    assert not comparison.grown


def test_o75_a_study_that_sets_no_key_has_the_fingerprint_it_had(study_dir):
    """The example, which sets no key, fingerprints as it did before the key existed: the same entries, and the same
    digest of the entries its configuration decides (the schema, the tasks, the models, the seeds and the caps; the
    environment is the machine's, and the prompts and tools the plugin's). The digest was taken on main before the key
    was added; if the example's own configuration changes it changes with it, and this pin is updated with it."""
    plain = fingerprint(study_dir)
    assert set(plain) == {
        "caps", "configs", "environment", "models", "prompts", "schema", "scorer_digest", "seeds", "tasks", "tools",
    }  # fmt: skip
    decided = {key: plain[key] for key in ("schema", "tasks", "models", "seeds", "caps")}
    digest = hashlib.sha256(json.dumps(decided, sort_keys=True).encode()).hexdigest()
    assert digest == "c54eecf4e4b7c525ca74a6dee4baadfb974f0e73e1c81165dced736bf5f0dad8"
    assert "data_path" not in json.dumps(plain)
