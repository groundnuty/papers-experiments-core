"""Loading preregistration.toml: validation that names the file and key, the hash in the manifest, and overrides."""

import hashlib
import tomllib

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.stats.pairing import ContrastSpec
from expcore.stats.power import PowerSettings
from expcore.stats.prereg import BootstrapSettings, Preregistration, parse
from expcore.stats.prereg import load as load_checked

SCHEMA = FactorSchema(
    {"gate": ["open", "shut"]},
    ["task", "gate", "model", "seed"],
    {"cond-a": {"gate": "open"}, "cond-b": {"gate": "shut"}},
)


def load(path):
    """Load a preregistration against the schema above."""
    return load_checked(path, SCHEMA)


BASE = """\
alpha = 0.05
holm_family = ["a-vs-b", "gate-effect"]

[bootstrap]
replicates = 500
seed = 20260101
ci_level = 0.9

[power]
seed = 3
n_simulations = 100
target_clusters = 20
target_difference = 0.1
sd_grid = [0.1, 0.2]

[estimators.wilcoxon_cluster_bootstrap]
method = "exact"

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.gate-effect]
factor = "gate"
levels = ["open", "shut"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def write(tmp_path, text=BASE):
    """Write a preregistration file and return its path."""
    path = tmp_path / "preregistration.toml"
    path.write_text(text, encoding="utf-8")
    return path


def edited(old, new, text=BASE):
    """Return the base text with one exact piece replaced; the piece must exist."""
    assert old in text, old
    return text.replace(old, new, 1)


def test_a_valid_file_loads_into_named_contrasts_a_family_and_settings(tmp_path):
    """Both contrast forms, the family, alpha, the bootstrap, the power settings and the estimator parameters."""
    prereg = load(write(tmp_path))
    assert isinstance(prereg, Preregistration)
    assert prereg.alpha == 0.05
    assert prereg.holm_family == ("a-vs-b", "gate-effect")
    assert prereg.bootstrap == BootstrapSettings(replicates=500, seed=20260101, ci_level=0.9)
    assert prereg.power == PowerSettings(
        seed=3, n_simulations=100, target_clusters=20, target_difference=0.1, sd_grid=(0.1, 0.2)
    )
    assert list(prereg.contrasts) == ["a-vs-b", "gate-effect"]
    assert prereg.contrasts["a-vs-b"] == ContrastSpec(
        "a-vs-b", "score", "wilcoxon_cluster_bootstrap", "group", conditions=("cond-a", "cond-b")
    )
    assert prereg.contrasts["gate-effect"] == ContrastSpec(
        "gate-effect", "score", "wilcoxon_cluster_bootstrap", "group", factor="gate", levels=("open", "shut")
    )
    assert prereg.estimator_params == {"wilcoxon_cluster_bootstrap": {"method": "exact"}}
    assert prereg.source == str(tmp_path / "preregistration.toml")


def test_the_optional_parts_may_be_left_out(tmp_path):
    """The interval level defaults to 0.95, and the power section and the estimator tables are optional."""
    text = edited("ci_level = 0.9\n", "")
    text = text[: text.index("[power]")] + text[text.index("[contrasts.a-vs-b]") :]
    prereg = load(write(tmp_path, text))
    assert prereg.bootstrap.ci_level == 0.95
    assert prereg.power is None
    assert prereg.estimator_params == {}


def test_p04_the_hash_of_the_file_goes_into_the_manifest(tmp_path):
    """P04: the SHA-256 of the file's bytes is the preregistration hash every output manifest carries."""
    path = write(tmp_path)
    prereg = load(path)
    assert prereg.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert prereg.manifest() == {"preregistration_sha256": prereg.sha256, "factor_schema_sha256": SCHEMA.digest}


def test_p04_an_edit_of_the_file_changes_the_hash(tmp_path):
    """P04: a one-character edit is a different preregistration."""
    first = load(write(tmp_path))
    second = load(write(tmp_path, edited("seed = 20260101", "seed = 20260102")))
    assert first.sha256 != second.sha256


def test_p04_an_override_produces_a_hash_ending_in_override(tmp_path):
    """P04: a test copy with changed settings cannot pass for the file."""
    prereg = load(write(tmp_path))
    smaller = prereg.with_override(bootstrap=BootstrapSettings(replicates=20, seed=1))
    assert smaller.sha256 == prereg.sha256 + "+override"
    assert smaller.manifest() == {
        "preregistration_sha256": prereg.sha256 + "+override",
        "factor_schema_sha256": SCHEMA.digest,
    }
    assert smaller.bootstrap.replicates == 20
    assert smaller.alpha == prereg.alpha
    assert prereg.bootstrap.replicates == 500  # the original is untouched
    assert smaller.with_override(alpha=0.01).sha256 == prereg.sha256 + "+override"  # marked once


def test_p04_the_hash_itself_cannot_be_overridden(tmp_path):
    """P04: with_override marks the hash and refuses to set it."""
    with pytest.raises(TypeError, match="cannot be overridden"):
        load(write(tmp_path)).with_override(sha256="0" * 64)


def test_each_contrast_gets_its_own_seed_from_its_name_and_the_estimator_its_parameters(tmp_path):
    """A contrast draws the same resamples whichever contrasts run before it."""
    prereg = load(write(tmp_path))
    first = prereg.estimator_settings(prereg.contrasts["a-vs-b"])
    second = prereg.estimator_settings(prereg.contrasts["gate-effect"])
    assert first.seed[0] == second.seed[0] == 20260101
    assert first.seed[1] != second.seed[1]
    assert first == prereg.estimator_settings(prereg.contrasts["a-vs-b"])
    assert (first.replicates, first.level, dict(first.params)) == (500, 0.9, {"method": "exact"})


@pytest.mark.parametrize(
    ("old", "new", "where", "key"),
    [
        (
            'estimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n\n[contrasts.gate',
            'estimator = "bayes"\ncluster = "group"\n\n[contrasts.gate',
            "[contrasts.a-vs-b]",
            "estimator",
        ),
        ("[estimators.wilcoxon_cluster_bootstrap]", "[estimators.bayes]", "[estimators.bayes]", None),
        ('method = "exact"', 'method = "pratt"', "[estimators.wilcoxon_cluster_bootstrap]", "method"),
        ('method = "exact"', "depth = 3", "[estimators.wilcoxon_cluster_bootstrap]", "depth"),
        ("alpha = 0.05\n", "", "top level", "alpha"),
        ("alpha = 0.05", "alpha = 1.5", "top level", "alpha"),
        ("alpha = 0.05", 'alpha = "0.05"', "top level", "alpha"),
        ("alpha = 0.05", "alpha = true", "top level", "alpha"),
        ("replicates = 500", "replicates = 0", "[bootstrap]", "replicates"),
        ("replicates = 500", "replicates = 2.5", "[bootstrap]", "replicates"),
        ("seed = 20260101", "seed = -1", "[bootstrap]", "seed"),
        ("ci_level = 0.9", "ci_level = 1.0", "[bootstrap]", "ci_level"),
        ('holm_family = ["a-vs-b", "gate-effect"]\n', "", "top level", "holm_family"),
        ('holm_family = ["a-vs-b", "gate-effect"]', "holm_family = []", "top level", "holm_family"),
        ('holm_family = ["a-vs-b", "gate-effect"]', 'holm_family = ["a-vs-b", "missing"]', "top level", "holm_family"),
        ('holm_family = ["a-vs-b", "gate-effect"]', 'holm_family = ["a-vs-b", "a-vs-b"]', "top level", "holm_family"),
        (
            'outcome = "score"\nestimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n\n[contrasts.gate',
            'estimator = "wilcoxon_cluster_bootstrap"\ncluster = "group"\n\n[contrasts.gate',
            "[contrasts.a-vs-b]",
            "outcome",
        ),
        ('conditions = ["cond-a", "cond-b"]', 'conditions = ["cond-a"]', "[contrasts.a-vs-b]", "conditions"),
        ('conditions = ["cond-a", "cond-b"]', 'conditions = ["cond-a", "cond-a"]', "[contrasts.a-vs-b]", "conditions"),
        (
            'conditions = ["cond-a", "cond-b"]',
            'conditions = ["cond-a", "cond-b"]\nfactor = "gate"',
            "[contrasts.a-vs-b]",
            "factor",
        ),
        ('conditions = ["cond-a", "cond-b"]\n', "", "[contrasts.a-vs-b]", "conditions"),
        ('levels = ["open", "shut"]\n', "", "[contrasts.gate-effect]", "levels"),
        ('levels = ["open", "shut"]', 'levels = ["open", "open"]', "[contrasts.gate-effect]", "levels"),
        (
            'cluster = "group"\n\n[contrasts.gate',
            'cluster = "group"\ncolour = "red"\n\n[contrasts.gate',
            "[contrasts.a-vs-b]",
            "colour",
        ),
        ("target_clusters = 20", "target_clusters = 1", "[power]", "target_clusters"),
        ("target_difference = 0.1", "target_difference = 0", "[power]", "target_difference"),
        ("sd_grid = [0.1, 0.2]", "sd_grid = []", "[power]", "sd_grid"),
        ("sd_grid = [0.1, 0.2]", "sd_grid = [0.1, -0.2]", "[power]", "sd_grid"),
        ("sd_grid = [0.1, 0.2]", 'sd_grid = [0.1, "wide"]', "[power]", "sd_grid"),
        ("[bootstrap]", "[boot]", "top level", "boot"),
    ],
)
def test_a_bad_file_is_refused_with_the_file_the_table_and_the_key(tmp_path, old, new, where, key):
    """Validation names the file and the key; the file is the one that was loaded."""
    path = write(tmp_path, edited(old, new))
    with pytest.raises(ConfigError) as caught:
        load(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: {where}: ")
    if key:
        assert f"key {key!r}" in message


def test_an_unknown_estimator_is_refused_and_the_registered_ones_are_listed(tmp_path):
    """A preregistration naming an unknown estimator does not load."""
    path = write(tmp_path, edited('estimator = "wilcoxon_cluster_bootstrap"', 'estimator = "bayes"'))
    with pytest.raises(ConfigError, match=r"unknown estimator 'bayes'; registered: wilcoxon_cluster_bootstrap"):
        load(path)


def test_a_file_without_contrasts_is_refused(tmp_path):
    """Nothing to preregister is an error, not an empty analysis."""
    with pytest.raises(ConfigError, match="key 'contrasts' must declare at least one contrast"):
        load(write(tmp_path, BASE[: BASE.index("[contrasts.a-vs-b]")]))


def test_a_missing_file_bad_toml_and_bad_bytes_are_refused_by_name(tmp_path):
    """The reading errors name the file as well."""
    with pytest.raises(ConfigError, match=r"preregistration\.toml: file not found"):
        load(tmp_path / "preregistration.toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(write(tmp_path, "alpha = = 1"))
    bad = tmp_path / "bytes.toml"
    bad.write_bytes(b"alpha = \xff\xfe")
    with pytest.raises(ConfigError, match=r"bytes\.toml: not valid UTF-8"):
        load(bad)


def test_parse_takes_a_mapping_and_the_hash_it_is_given():
    """The same validation serves a mapping that did not come from a file, and the caller supplies the hash."""
    prereg = parse(tomllib.loads(BASE), "inline", "f" * 64, SCHEMA)
    assert (prereg.source, prereg.sha256) == ("inline", "f" * 64)


CHAIN = FactorSchema(
    {"stage": ["one", "two", "three"]},
    ["task", "stage", "model", "seed"],
    {"chain-one": {"stage": "one"}, "chain-two": {"stage": "two"}, "chain-three": {"stage": "three"}},
    tasks={"chain-two": ["task-a", "task-b"], "chain-three": ["task-c"]},
    file="study.toml",
)


@pytest.mark.parametrize(
    ("old", "new", "key", "fragment"),
    [
        (
            'conditions = ["cond-a", "cond-b"]',
            'conditions = ["cond-a", "cond-z"]',
            "conditions",
            "unknown condition 'cond-z'",
        ),
        ('factor = "gate"', 'factor = "tempo"', "factor", "unknown factor 'tempo'"),
        ('factor = "gate"', 'factor = "model"', "factor", "'model' is built in"),
        ('levels = ["open", "shut"]', 'levels = ["open", "ajar"]', "levels", "unknown level 'ajar' of 'gate'"),
    ],
)
def test_a_contrast_naming_what_the_factor_schema_lacks_is_refused_at_load(tmp_path, old, new, key, fragment):
    """The arms of a contrast must exist in the schema; the message names the file, the contrast and the key."""
    path = write(tmp_path, edited(old, new))
    with pytest.raises(ConfigError) as caught:
        load(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: [contrasts.")
    assert f"key {key!r}" in message
    assert fragment in message


def test_the_message_for_an_unknown_condition_lists_the_conditions_of_the_schema_file(tmp_path):
    """The author sees which conditions exist, and in which file they were declared."""
    text = edited('conditions = ["cond-a", "cond-b"]', 'conditions = ["chain-one", "cond-z"]')
    with pytest.raises(ConfigError, match=r"the conditions of study.toml are: chain-one, chain-two, chain-three"):
        load_checked(write(tmp_path, text), CHAIN)


def test_two_conditions_that_run_on_no_task_in_common_are_refused_at_load(tmp_path):
    """A chain design lists the tasks of an arm; two arms with disjoint lists could never be paired."""
    text = edited('conditions = ["cond-a", "cond-b"]', 'conditions = ["chain-two", "chain-three"]')
    with pytest.raises(ConfigError, match="'chain-two' and 'chain-three' run on no task in common"):
        load_checked(write(tmp_path, text), CHAIN)


def test_conditions_with_overlapping_or_unlisted_tasks_load(tmp_path):
    """An arm without a task list runs on every task, so it overlaps with any list."""
    for pair in ('["chain-one", "chain-three"]', '["chain-one", "chain-two"]', '["chain-two", "chain-one"]'):
        text = edited('conditions = ["cond-a", "cond-b"]', f"conditions = {pair}")
        text = text.replace('factor = "gate"\nlevels = ["open", "shut"]', 'factor = "stage"\nlevels = ["one", "two"]')
        assert load_checked(write(tmp_path, text), CHAIN).contrasts["a-vs-b"].conditions is not None


def test_the_manifest_names_the_factor_schema_the_contrasts_were_checked_against(tmp_path):
    """Another schema is another digest in the output."""
    text = edited('conditions = ["cond-a", "cond-b"]', 'conditions = ["chain-one", "chain-two"]')
    text = text.replace('factor = "gate"\nlevels = ["open", "shut"]', 'factor = "stage"\nlevels = ["one", "two"]')
    prereg = load_checked(write(tmp_path, text), CHAIN)
    assert prereg.manifest()["factor_schema_sha256"] == CHAIN.digest != SCHEMA.digest
