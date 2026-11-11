"""A study's own estimator: registered by a module `[study] platforms` names, never by the plugin.

The preregistration is validated when the study is loaded, before the plugin is imported, so the estimator a contrast
names must be registered by then. The modules of `[study] platforms` are imported first, so one of them can register
it; the plugin comes too late.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest
from study_support import HEAD, Campaign, clean_environment, cli, finish

from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.stats import estimators
from expcore.study.config import load_study

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
MODULE = "quorvane_estimators"  # an invented name for an installed module of the study's own
ESTIMATOR = "cluster_mean"
SOURCE = f'''\
"""A study's estimator: the mean cluster difference, with no test (p = 1) and no interval."""

import math

from expcore.stats import estimators
from expcore.stats.estimators import EstimatorResult


@estimators.register("{ESTIMATOR}")
def cluster_mean(paired, settings):
    values = paired.cluster_differences
    estimate = float(values.mean()) if len(values) else math.nan
    return EstimatorResult(estimate, 1.0, math.nan, math.nan, len(paired.clusters))
'''
EXPLORATORY = (  # the exploratory contrast of the example's preregistration, up to its estimator
    '[contrasts.tools-effect-on-rounds]\nfactor = "tools"\nlevels = ["extended", "basic"]\noutcome = "rounds"\n'
)

__all__ = ["clean_environment"]  # a fixture a test below uses, imported from the support module


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A registry of estimators that the test may add to, and a directory on the path for the study's module."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    installed = tmp_path / "site"
    installed.mkdir()
    (installed / f"{MODULE}.py").write_text(SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(installed))
    yield
    sys.modules.pop(MODULE, None)


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def study_naming(base: Path, *, platforms: bool) -> Path:
    """A copy of the example whose exploratory contrast names the study's estimator, with its module listed or not."""
    copy = base / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    old = EXPLORATORY + 'estimator = "wilcoxon_cluster_bootstrap"\n'
    edit(copy / "preregistration.toml", old, EXPLORATORY + f'estimator = "{ESTIMATOR}"\n')
    if platforms:
        edit(copy / "study.toml", 'plugin = "plugin.py"\n', f'plugin = "plugin.py"\nplatforms = ["{MODULE}"]\n')
    return copy


def test_p05_a_study_estimator_registered_by_a_module_in_its_platforms_is_preregistered_and_analysed(
    registry, tmp_path, clean_environment
):
    """P05: adds that a study's own inference code enters through an installed module `[study] platforms` names,
    imported before the preregistration is validated: a contrast may name the estimator it registers, and the
    analysis estimates with it. Without a p-values function the contrast has no minimum detectable effect."""
    study_dir = study_naming(tmp_path, platforms=True)
    assert load_study(study_dir).prereg.contrasts["tools-effect-on-rounds"].estimator == ESTIMATOR
    write_stamp(study_dir, HEAD, load_study(study_dir).scope)
    campaign = Campaign(study_dir)
    campaign.codes["run"], _ = cli(clean_environment, "run", str(study_dir))
    finish(clean_environment, campaign)
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    analysis = json.loads((study_dir / "out" / "analysis.json").read_text(encoding="utf-8"))
    contrast = next(c for c in analysis["contrasts"] if c["name"] == "tools-effect-on-rounds")
    assert (contrast["estimator"], contrast["p_value"], contrast["conclusion"]) == (ESTIMATOR, 1.0, "not_detected")
    assert contrast["estimate"] < 0  # the extended level looks once, the basic level twice
    assert contrast["minimum_detectable_effect"] is None  # NaN in the analysis, null in its JSON


def test_p05_an_estimator_only_the_plugin_would_register_is_unknown_when_the_study_is_loaded(registry, tmp_path):
    """P05: adds the limit: the plugin is imported after the study is loaded, so an estimator only it registers is
    refused as unknown, with the registered names, whatever the plugin does."""
    study_dir = study_naming(tmp_path, platforms=False)
    registers = f"import {MODULE}  # registers {ESTIMATOR}, but only once the plugin is imported\n"
    edit(study_dir / "plugin.py", '"""The plugin of the tiny study', registers + '"""The plugin of the tiny study')
    with pytest.raises(ConfigError, match=f"unknown estimator '{ESTIMATOR}'; registered: wilcoxon_cluster_bootstrap"):
        load_study(study_dir)
    assert MODULE not in sys.modules
