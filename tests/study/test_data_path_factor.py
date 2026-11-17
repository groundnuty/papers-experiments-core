"""O75: a study names the factor that is its data path in `[study] data_path_factor`, the one place the core reads it
from; without the key the name is `data_path`, and a study that does not set it is exactly as it was."""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from study_support import HEAD

from expcore.campaign.factors import DATA_PATH_FACTOR
from expcore.campaign.manifest import compare_fingerprints
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import check_study, load_study
from expcore.study.pipeline import fingerprint_of, run_study
from expcore.study.plugin import StudyContext, check_data_path_factor, load_plugin

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


# --- a campaign starts only when the platform reads the data path by the factor the study names -----------------


def plugin_says(study: Path, value: object) -> None:
    """The copy's plugin passes `data_path_factor=<value>`, as `platform.data_path_factor` gives it."""
    path = study / "plugin.py"
    text = path.read_text(encoding="utf-8")
    old = "        executor=TinyExecutor(context),\n"
    assert text.count(old) == 1
    path.write_text(text.replace(old, f"{old}        data_path_factor={value!r},\n"), encoding="utf-8")


def loaded(study_dir: Path):
    study = load_study(study_dir)
    return study, load_plugin(StudyContext(study, {"commit": "0" * 40}))


def test_o75_a_study_that_names_its_factor_and_a_platform_built_without_it_is_refused_naming_both(study_dir):
    """O75: the platform reads `data_path` when it is built without the argument, and the study says `path`: refused,
    with both names, never read silently by the default."""
    with_path_factor(study_dir)
    study, plugin = loaded(study_dir)
    assert plugin.data_path_factor is None
    with pytest.raises(ConfigError) as raised:
        check_data_path_factor(study, plugin)
    text = str(raised.value)
    assert "study.toml" in text
    assert "[study]: key 'data_path_factor'" in text
    assert "'path'" in text
    assert "'data_path'" in text


def test_o75_a_platform_built_with_another_name_than_the_study_names_is_refused_naming_both(study_dir):
    with_path_factor(study_dir)
    study, plugin = loaded(study_dir)
    with pytest.raises(ConfigError, match=r"'path'.*'route'|'route'.*'path'"):
        check_data_path_factor(study, dataclasses.replace(plugin, data_path_factor="route"))


def test_o75_a_platform_built_with_the_name_the_study_names_passes(study_dir):
    with_path_factor(study_dir)
    study, plugin = loaded(study_dir)
    check_data_path_factor(study, dataclasses.replace(plugin, data_path_factor="path"))


def test_o75_a_study_with_no_key_and_a_platform_at_the_default_passes_unchanged(study_dir):
    """No key and a plugin that says nothing, or says `data_path`, as every study did: nothing is refused."""
    study, plugin = loaded(study_dir)
    assert (study.data_path_factor, plugin.data_path_factor) == ("data_path", None)
    check_data_path_factor(study, plugin)
    check_data_path_factor(study, dataclasses.replace(plugin, data_path_factor="data_path"))


def test_o75_a_platform_that_reads_another_factor_than_the_study_without_a_key_names_is_refused(study_dir):
    """The other direction: the platform was built with `path`, the study sets no key, so its factor is `data_path`."""
    study, plugin = loaded(study_dir)
    with pytest.raises(ConfigError, match=r"'data_path'.*'path'|'path'.*'data_path'"):
        check_data_path_factor(study, dataclasses.replace(plugin, data_path_factor="path"))


@pytest.mark.parametrize("bad", ["", " ", 3, ["path"]])
def test_o75_a_plugin_that_names_no_factor_is_refused_when_it_is_loaded(study_dir, bad):
    plugin_says(study_dir, bad)
    study = load_study(study_dir)
    with pytest.raises(ConfigError, match="data_path_factor"):
        load_plugin(StudyContext(study, {"commit": "0" * 40}))


def test_o75_a_campaign_whose_platform_reads_another_factor_is_refused_at_its_start_before_any_run(study_dir):
    """The start of the campaign holds the study to its platform: nothing is run, and no run directory is made."""
    with_path_factor(study_dir)
    write_stamp(study_dir, HEAD, load_study(study_dir).scope)
    with pytest.raises(ConfigError, match="data_path_factor") as raised:
        run_study(study_dir)
    assert "'path'" in str(raised.value)
    runs = study_dir / "out" / "runs"
    assert not runs.exists() or not any(runs.iterdir())


def test_o75_a_campaign_whose_platform_reads_the_factor_the_study_names_starts_and_runs(study_dir):
    with_path_factor(study_dir)
    plugin_says(study_dir, "path")
    write_stamp(study_dir, HEAD, load_study(study_dir).scope)
    summary = run_study(study_dir)
    assert summary.statuses == {"done": len(load_study(study_dir).manifest.rows)}
