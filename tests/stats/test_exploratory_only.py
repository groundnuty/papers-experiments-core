"""A preregistration that makes no confirmatory claim: `exploratory_only = true`, and no Holm family (S12)."""

import dataclasses
import hashlib
import tomllib

import pytest
from stats_support import both_arms

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.stats import power
from expcore.stats.analysis import analyse, power_study
from expcore.stats.prereg import BootstrapSettings, Preregistration, load, parse

SCHEMA = FactorSchema(
    {"arm": ["a", "b", "x", "y"]},
    ["task", "arm", "model", "seed"],
    {f"cond-{arm}": {"arm": arm} for arm in "abxy"},
)

TEXT = """\
alpha = 0.05
exploratory_only = true

[bootstrap]
replicates = 50
seed = 11

[power]
seed = 2
n_simulations = 20
target_clusters = 10
target_difference = 0.2
sd_grid = [0.1]

[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"

[contrasts.x-vs-y]
conditions = ["cond-x", "cond-y"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""


def edited(old: str, new: str, text: str = TEXT) -> str:
    """The text with one exact piece replaced; the piece must be there."""
    assert old in text, old
    return text.replace(old, new, 1)


def parsed(text: str = TEXT) -> Preregistration:
    return parse(tomllib.loads(text), "inline", "a" * 64, SCHEMA)


def refusal(text: str) -> str:
    with pytest.raises(ConfigError) as caught:
        parsed(text)
    return str(caught.value)


def rows():
    """Six clusters in which cond-a scores higher than cond-b, then two pairs whose cond-a run is a treatment attempt
    the scorer left None: a drop a confirmatory contrast would be refused for."""
    found = []
    for i in range(6):
        found += both_arms(f"t{i}", "model-a", 1, 0.9 - 0.01 * i, 0.5, cluster=f"c{i}")
    for i in range(2):
        first, second = both_arms(f"outage-{i}", "model-a", 1, None, 0.5, cluster=f"c{i}")
        found += [dataclasses.replace(first, treatment=True), second]
    return found


def test_s12_exploratory_only_loads_with_no_family():
    """S12: a pilot's preregistration says it makes no confirmatory claim; it has no Holm family and says so."""
    prereg = parsed()
    assert prereg.exploratory_only is True
    assert prereg.holm_family == ()
    assert parsed(edited("exploratory_only = true", 'holm_family = ["a-vs-b"]')).exploratory_only is False


def test_s12_an_empty_family_alone_is_refused_and_the_refusal_names_exploratory_only():
    """S12: an empty list is more likely a mistake than a choice, so it is refused, and the refusal says how to make
    the choice explicitly."""
    message = refusal(edited("exploratory_only = true", "holm_family = []"))
    assert message.startswith("inline: top level: key 'holm_family' ")
    assert "exploratory_only = true" in message
    assert "mistake" in message


def test_s12_neither_a_family_nor_exploratory_only_is_refused_naming_both():
    """S12: a preregistration must say which of its contrasts are confirmatory, or that none are."""
    left_out = edited("exploratory_only = true\n", "")
    false = edited("exploratory_only = true", "exploratory_only = false")
    for text in (left_out, false):
        message = refusal(text)
        assert "key 'holm_family' is required" in message
        assert "exploratory_only = true" in message


@pytest.mark.parametrize("family", ['holm_family = ["a-vs-b"]', "holm_family = []"])
def test_s12_exploratory_only_with_a_family_is_refused(family):
    """S12: the two say opposite things, so the file is refused, whatever the family holds."""
    message = refusal(edited("exploratory_only = true", f"exploratory_only = true\n{family}"))
    assert "key 'holm_family' " in message
    assert "exploratory_only" in message


@pytest.mark.parametrize("value", ['"true"', "1", "[true]"])
def test_s12_exploratory_only_is_a_boolean(value):
    """S12: anything but true or false is refused, naming the key."""
    assert "key 'exploratory_only' must be true or false" in refusal(
        edited("exploratory_only = true", f"exploratory_only = {value}")
    )


def test_s12_a_tolerance_is_refused_with_no_family_because_it_would_do_nothing():
    """S12 and S10: a tolerance for unscored treatment attempts only matters to a member of the family, and there is
    none, so it is refused, saying why."""
    text = edited('cluster = "group"\n\n[contrasts.x-vs-y]', 'cluster = "group"\ntreatment_outcomes = "scored"\n'
                  'max_unscored_treatment = 1\n\n[contrasts.x-vs-y]')  # fmt: skip
    message = refusal(text)
    assert "key 'exploratory_only' is true, so there is no family" in message
    assert "the max_unscored_treatment of 'a-vs-b' would do nothing" in message


def test_s12_the_setting_is_part_of_the_preregistrations_hash(tmp_path):
    """S12 and P04: the hash is of the file's bytes, so a file that says it is exploratory only never shares a hash
    with one that names a family, and a test copy that changes the setting is marked."""
    exploratory, confirmatory = tmp_path / "exploratory.toml", tmp_path / "confirmatory.toml"
    exploratory.write_text(TEXT, encoding="utf-8")
    confirmatory.write_text(edited("exploratory_only = true", 'holm_family = ["a-vs-b"]'), encoding="utf-8")
    first, second = load(exploratory, SCHEMA), load(confirmatory, SCHEMA)
    assert first.sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    assert first.sha256 != second.sha256
    changed = second.with_override(exploratory_only=True, holm_family=())
    assert changed.sha256.endswith("+override")
    assert changed.exploratory_only


def test_s12_a_built_preregistration_cannot_say_both_or_neither():
    """S12: the object refuses what the file refuses, so no copy can carry a family and the flag together."""
    args = ("inline", "a" * 64, 0.05, BootstrapSettings(10, 1), {})
    with pytest.raises(ValueError, match="exploratory_only"):
        Preregistration(*args, ("a-vs-b",), exploratory_only=True)
    with pytest.raises(ValueError, match="exploratory_only"):
        Preregistration(*args, ())


def test_s12_every_contrast_is_exploratory_and_holm_decides_nothing():
    """S12: no contrast is in a family, so each is exploratory, judged by its raw p-value, and there is no Holm
    decision; the result says that the preregistration makes no confirmatory claim."""
    result = analyse(rows(), parsed())
    assert result.exploratory_only is True
    assert result.holm == ()
    for contrast in result.contrasts:
        assert (contrast.exploratory, contrast.in_family) == (True, False)
        assert contrast.significant == (contrast.p_value <= 0.05)


def test_s12_no_treatment_refusal_applies_with_no_family():
    """S12 and S10: the refusal for a treatment attempt left None is for a member of the family; with none, the drop is
    counted and reported and the analysis goes on."""
    contrast = analyse(rows(), parsed()).contrast("a-vs-b")
    assert contrast.pairs_missing["treatment_outcome_none"] == 2
    assert contrast.n_pairs == 6


def test_s12_the_minimum_detectable_effect_is_at_alpha_with_no_family(monkeypatch):
    """S12: with no family there is nothing to divide alpha by; each contrast's minimum detectable effect is found at
    alpha, as for any exploratory contrast."""
    levels = []

    def found_at(sd, clusters, level, *rest):
        levels.append(level)
        return 0.5

    monkeypatch.setattr(power, "minimum_detectable_effect", found_at)
    result = analyse(rows(), parsed())
    assert levels == [0.05]  # x-vs-y has no rows: untestable, with no minimum detectable effect
    assert result.contrast("a-vs-b").minimum_detectable_effect == 0.5


def test_s12_the_power_study_has_no_family_rows_with_no_family():
    """S12: the power table gives power at alpha only; a row at alpha over the family size would divide by zero."""
    result = power_study(parsed())
    assert [(row["sd"], row["alpha_level"], row["alpha"]) for row in result.rows] == [(0.1, "alpha", 0.05)]
