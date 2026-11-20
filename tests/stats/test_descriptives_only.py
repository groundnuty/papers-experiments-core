"""S21: a preregistration with no contrast is accepted when it is exploratory_only and declares one-arm estimates.

A campaign that runs one condition alone has nothing to contrast: its analysis is descriptive, compared with others
outside the core. The loader used to refuse such a file for having no contrast; it now accepts exactly this form, and
every other file with no contrast is refused as it was.
"""

import json
import math
import tomllib

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.report import _sensitivity, render_report, summary_lines
from expcore.stats.analysis import analyse, pilot_power, power_study
from expcore.stats.federation_effect import federation_analysis
from expcore.stats.prereg import parse
from expcore.stats.rows import Row
from expcore.study.pipeline import _json_text

SCHEMA = FactorSchema(
    {"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{arm}": {"arm": arm} for arm in "ab"}
)
TEXT = """\
alpha = 0.05
exploratory_only = true

[bootstrap]
replicates = 100
seed = 11

[power]
seed = 2
n_simulations = 20
target_clusters = 10
target_difference = 0.2
sd_grid = [0.1]

[descriptives.waited-in-a]
conditions = ["cond-a"]
outcome = "waited"
cluster = "group"

[descriptives.queries-per-placement]
conditions = ["cond-a"]
numerator = "queries"
denominator = "placements"
cluster = "group"
"""
CONTRAST = """
[contrasts.a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "waited"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""
CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
WAITED = ((1, 1), (1, 0), (0, 0), (1, 1))  # per cluster, its two tasks' outcome: the clusters' means are 1, .5, 0, 1


def parsed(text: str = TEXT):
    return parse(tomllib.loads(text), "inline", "a" * 64, SCHEMA)


def refusal(text: str) -> str:
    with pytest.raises(ConfigError) as caught:
        parsed(text)
    return str(caught.value)


def rows(condition: str = "cond-a"):
    """Eight runs of one condition in four clusters, one model, each with a rate outcome and a ratio's two parts."""
    found = []
    for i, waited in enumerate(WAITED):
        for j, value in enumerate(waited):
            task, outcomes = f"task-{i}-{j}", {"waited": value, "queries": 2.0, "placements": 4.0}
            key = f"task={task}|model=model-a|seed=1|condition={condition}"
            found.append(
                Row(key, condition, {"task": task, "model": "model-a", "seed": 1}, outcomes, {"group": f"grp-{i}"})
            )
    return found


def test_s21_a_descriptives_only_preregistration_loads():
    """S21: no contrast, no family, exploratory_only, and its one-arm estimates declared in order."""
    prereg = parsed()
    assert dict(prereg.contrasts) == {}
    assert (prereg.holm_family, prereg.exploratory_only) == ((), True)
    assert list(prereg.descriptives) == ["waited-in-a", "queries-per-placement"]


@pytest.mark.parametrize(
    "text",
    [
        TEXT.replace("exploratory_only = true\n", ""),
        TEXT.replace("exploratory_only = true", "exploratory_only = false"),
        TEXT.replace("exploratory_only = true", 'holm_family = ["a-vs-b"]'),
    ],
    ids=["no exploratory_only", "exploratory_only false", "a family instead"],
)
def test_s21_no_contrast_without_exploratory_only_is_still_refused_and_the_refusal_names_the_allowed_form(text):
    """S21: the descriptives alone do not make a preregistration: it must also say it makes no confirmatory claim."""
    message = refusal(text)
    assert "key 'contrasts' must declare at least one contrast" in message
    assert "exploratory_only = true" in message
    assert "[descriptives.<name>]" in message


@pytest.mark.parametrize("tail", ["", "[descriptives]\n"], ids=["no table", "an empty table"])
def test_s21_no_contrast_with_exploratory_only_and_no_descriptives_is_still_refused(tail):
    """S21: nothing declared is not a preregistration, whether the table is left out or is empty."""
    text = TEXT[: TEXT.index("[descriptives.waited-in-a]")] + tail
    assert "key 'contrasts' must declare at least one contrast" in refusal(text)


def test_s21_a_file_refused_before_for_a_bad_flag_is_refused_for_the_same_reason():
    """S21: the loader reads exploratory_only for this form only when it is true, so a file with no contrast and a flag
    of the wrong type is refused for having no contrast, as it was."""
    text = TEXT.replace("exploratory_only = true", 'exploratory_only = "true"')
    assert "key 'contrasts' must declare at least one contrast" in refusal(text)


def test_s21_a_contrast_beside_the_descriptives_loads_as_before():
    """S21: the new form changes nothing for a preregistration that has a contrast."""
    prereg = parsed(TEXT + CONTRAST)
    assert list(prereg.contrasts) == ["a-vs-b"]
    assert list(prereg.descriptives) == ["waited-in-a", "queries-per-placement"]


def test_s21_the_analysis_reports_its_descriptives_and_no_contrast():
    """S21: the Holm family is empty, there is no contrast and no decision, and each estimate is its hand-computed
    value: the clusters' means 1, .5, 0 and 1 average to 0.625; a ratio of totals is 16 / 32."""
    result = analyse(rows(), parsed())
    assert (result.contrasts, result.holm, result.exploratory_only) == ((), (), True)
    assert (result.holm_families, result.equivalence_holm, result.equivalence_families) == ((), (), ())
    rate, ratio = result.descriptives
    assert (rate.name, rate.kind, rate.n_clusters, rate.n_runs) == ("waited-in-a", "rate", 4, 8)
    assert rate.estimate == pytest.approx(0.625)
    assert rate.ci_low <= rate.estimate <= rate.ci_high
    assert (ratio.kind, ratio.n_clusters, ratio.n_runs, ratio.estimate) == ("ratio_of_totals", 4, 8, 0.5)


def export_of(data):
    """The campaign export of runs that all finished: what the report reads for its conditions."""
    attempts = [
        {"run_key": row.run_key, "condition": row.condition, "dispatch_seq": i, "factors": {"model": "model-a"}}
        for i, row in enumerate(data)
    ]
    return {
        "planned_runs": len(attempts),
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": attempts,
        "infrastructure_by_condition": [],
        "retries_by_kind": [],
        "budget_holds": [],
    }


def test_s21_the_report_has_no_contrast_section_and_says_it_makes_no_claim():
    """S21: the one-arm estimates are the whole analysis: no contrast table, pairs, missing pairs or arm runs."""
    report = render_report(analyse(rows(), parsed()), export_of(rows()), study="a-study", code=CODE, core_version="1")
    assert "## One-arm estimates (descriptive)" in report
    assert "| waited-in-a " in report
    for heading in (
        "## Exploratory contrasts",
        "## Preregistered contrasts",
        "### Pairs",
        "## Missing pairs by cause",
        "### Runs of each arm",
        "None declared",
    ):
        assert heading not in report
    assert "**Confirmatory claims**: none" in report


def test_s21_the_terminal_summary_prints_the_claim_line_and_each_estimate():
    """S21: no contrast line, the estimates as descriptive."""
    lines = summary_lines(analyse(rows(), parsed()))
    assert lines[0].startswith("No confirmatory claim: ")
    assert [line.split(" (descriptive")[0] for line in lines[1:]] == ["waited-in-a", "queries-per-placement"]
    assert len(lines) == 3


def test_s21_a_sensitivity_rerun_of_it_says_no_contrast_is_declared():
    """S21: a rerun that leaves runs out has no contrast to show, and says so instead of an empty family."""
    shown = "\n".join(_sensitivity(analyse(rows(), parsed()), None, "the flagged runs"))
    assert "No contrast declared" in shown
    assert "in the family" not in shown


def test_s21_the_federation_rerun_has_nothing_to_adjust_and_says_so():
    """S21: the rerun refits the contrasts, so with none it is not computed, and the reason says there is none."""
    data = rows()
    placement = {row.run_key: ("site-a", f"fed-{i % 2}") for i, row in enumerate(data)}
    federation = federation_analysis(data, [], parsed(), placement)
    assert federation.sensitivity is None
    assert "there is none" in (federation.refused or "")


def test_s21_the_power_settings_need_no_contrast():
    """S21: the power study reads no contrast (at alpha alone, as with no family), and a pilot's has no row."""
    prereg = parsed()
    assert [row["alpha_level"] for row in power_study(prereg).rows] == ["alpha"]
    assert pilot_power(rows(), prereg).rows == ()


def test_s21_analysis_json_holds_the_descriptives_with_a_missing_interval_as_null():
    """S21: an analysis with no contrast would write nothing of its estimates unless they are there; a NaN interval (an
    arm with no usable run) is written null, as every other missing number is."""
    [rate, _] = analyse(rows("cond-b"), parsed()).descriptives  # no run of the arm: nothing to estimate
    assert math.isnan(rate.estimate)
    written = json.loads(_json_text({"descriptives": [rate]}))["descriptives"][0]
    assert [written[key] for key in ("name", "estimate", "ci_low", "ci_high", "n_runs")] == [
        "waited-in-a",
        None,
        None,
        None,
        0,
    ]
