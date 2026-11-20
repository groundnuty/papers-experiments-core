"""S21: a study of one condition, whose preregistration declares no contrast, runs end to end through the command line.

The example study is cut down to its baseline condition (eight runs: four tasks, two models, one seed) and given a
preregistration with `exploratory_only = true` and two one-arm estimates. The campaign is run, checked, analyzed and
reported once, and each test asserts one thing about what the pipeline wrote.
"""

import hashlib
import shutil
from pathlib import Path

import pytest
from study_support import EXAMPLE, HEAD, Campaign, clean_environment, cli, finish

from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PREREGISTRATION = """\
alpha = 0.05
exploratory_only = true

[bootstrap]
replicates = 200
seed = 20260101

[descriptives.accuracy-in-baseline]
conditions = ["baseline"]
outcome = "accuracy"
cluster = "region"

[descriptives.lookups-per-round]
conditions = ["baseline"]
numerator = "lookups"
denominator = "rounds"
cluster = "region"
"""


def one_condition_copy(base: Path) -> Path:
    """The example with only its baseline condition and the preregistration above, stamped as a snapshot is."""
    copy = base / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    text = (copy / "study.toml").read_text(encoding="utf-8")
    others = text[text.index("# Restricted to a subset") : text.index("# Four tasks.")]
    (copy / "study.toml").write_text(text.replace(others, ""), encoding="utf-8")
    (copy / "preregistration.toml").write_text(PREREGISTRATION, encoding="utf-8")
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


@pytest.fixture(scope="module")
def campaign(tmp_path_factory, clean_environment) -> Campaign:
    """Run the one-condition study, then check, analyze and report it."""
    made = Campaign(one_condition_copy(tmp_path_factory.mktemp("descriptives-only")))
    made.codes["run"], _ = cli(clean_environment, "run", str(made.root))
    finish(clean_environment, made)
    return made


def test_s21_the_study_loads_with_one_condition_eight_runs_and_no_contrast(campaign):
    study = load_study(campaign.root)
    assert (len(study.manifest.rows), dict(study.prereg.contrasts)) == (8, {})
    assert list(study.prereg.descriptives) == ["accuracy-in-baseline", "lookups-per-round"]


def test_s21_every_command_succeeds(campaign):
    assert campaign.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    assert len(campaign.read("export.json")["analysed_attempts"]) == 8


def test_s21_analysis_json_has_no_contrast_and_holds_each_estimate(campaign):
    """S21: the analysis says it makes no claim and holds the estimates, with the hash of the file they came from.
    Accuracy: the models answer right on 6 of 8 runs; the clusters (two regions) average 0.75 and 0.25, so 0.5."""
    analysis = campaign.read("analysis.json")
    assert (analysis["contrasts"], analysis["holm"], analysis["exploratory_only"]) == ([], [], True)
    preregistration = (campaign.root / "preregistration.toml").read_bytes()
    assert analysis["preregistration"]["preregistration_sha256"] == hashlib.sha256(preregistration).hexdigest()
    rate, ratio = analysis["descriptives"]
    assert (rate["name"], rate["kind"], rate["n_clusters"], rate["n_runs"]) == ("accuracy-in-baseline", "rate", 2, 8)
    assert rate["estimate"] == pytest.approx(0.5)
    assert rate["ci_low"] <= rate["estimate"] <= rate["ci_high"]
    assert (ratio["name"], ratio["kind"], ratio["n_runs"]) == ("lookups-per-round", "ratio_of_totals", 8)
    assert ratio["estimate"] > 0
    assert ratio["ci_low"] <= ratio["estimate"] <= ratio["ci_high"]


def test_s21_the_report_shows_the_estimates_and_no_contrast_section(campaign):
    report = campaign.report
    assert "## One-arm estimates (descriptive)" in report
    assert "| accuracy-in-baseline " in report
    for heading in ("## Exploratory contrasts", "## Preregistered contrasts", "### Pairs", "## Missing pairs by cause"):
        assert heading not in report
    assert "**Confirmatory claims**: none" in report
