"""S16: a study that declares no smallest effect of interest keeps the bytes of its analysis.json and its report.

`tests/data/analysis-undeclared.json` and `tests/data/report-undeclared.md` were made by the code before
`smallest_effect` and `smallest_ratio` existed, from the deterministic analysis below: a confirmatory contrast of one
outcome with a margin, an exploratory ratio contrast and power settings, so every field a later change could add to a
contrast's result is exercised. A change of these bytes is a change every existing study would see.
"""

import json
import math
import tomllib
from pathlib import Path

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, analyse
from expcore.stats.prereg import parse
from expcore.stats.rows import Row
from expcore.study.pipeline import _json_text

DATA = Path(__file__).resolve().parents[1] / "data"
ARMS = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
TEXT = """\
alpha = 0.05
holm_family = ["score-a-vs-b"]

[bootstrap]
replicates = 200
seed = 11

[power]
seed = 5
n_simulations = 40
target_clusters = 10
target_difference = 0.1
sd_grid = [0.1]

[contrasts.score-a-vs-b]
conditions = ["cond-a", "cond-b"]
outcome = "score"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.3

[contrasts.spend-per-win]
conditions = ["cond-a", "cond-b"]
estimator = "ratio"
numerator = "spend"
denominator = "wins"
zero_denominator = "exclude"
cluster = "group"
"""
LIFTS = (0.12, 0.31, 0.05, 0.22, 0.4, 0.18, 0.09, 0.27)  # arm A's score above arm B's, per cluster
FACTORS = (1.5, 3.0, 0.8, 2.2, 1.1, 4.0, 1.7, 2.6)  # arm A's spend per win over arm B's, per cluster
EXPORT = {
    "planned_runs": 2,
    "exhausted_runs": [],
    "unfinished_runs": [],
    "analysed_attempts": [
        {"run_key": "k1", "condition": "cond-a", "dispatch_seq": 1, "factors": {"model": "model-a"}},
        {"run_key": "k2", "condition": "cond-b", "dispatch_seq": 2, "factors": {"model": "model-a"}},
    ],
    "infrastructure_by_condition": [],
    "retries_by_kind": [],
    "budget_holds": [],
}
CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}


def undeclared_analysis(text: str = TEXT) -> AnalysisResult:
    """The analysis the fixtures were made from: eight clusters, two tasks each, deterministic rows and seeds."""
    rows = []
    for i, (lift, factor) in enumerate(zip(LIFTS, FACTORS, strict=True)):
        for task in (f"t{i}a", f"t{i}b"):
            for arm, score, spend in (("cond-a", 0.4 + lift, 30.0 * factor), ("cond-b", 0.4, 30.0)):
                run_key = f"task={task}|model=model-a|seed=1|condition={arm}"
                factors = {"task": task, "model": "model-a", "seed": 1}
                outcomes = {"score": score, "spend": spend, "wins": 2.0}
                rows.append(Row(run_key, arm, factors, outcomes, {"group": f"c{i}"}))
    return analyse(rows, parse(tomllib.loads(text), "inline", "b" * 64, ARMS))


def analysis_text(result: AnalysisResult) -> str:
    """What analysis.json holds of the analysis: its contrasts and its Holm decisions, as the pipeline writes them."""
    return _json_text({"contrasts": result.contrasts, "holm": result.holm, "alpha": result.alpha})


def report_text(result: AnalysisResult) -> str:
    """The report and the terminal summary of the analysis."""
    report = render_report(result, EXPORT, study="a-study", code=CODE, core_version="1.2.3", interval_level=0.95)
    return report + "\n".join(summary_lines(result)) + "\n"


def test_s16_an_analysis_that_declares_no_smallest_effect_keeps_its_analysis_json_bytes():
    """S16: no `smallest_effect` key, null or other, enters analysis.json for a contrast that declares none."""
    assert analysis_text(undeclared_analysis()) == (DATA / "analysis-undeclared.json").read_text(encoding="utf-8")


def test_s16_an_analysis_that_declares_no_smallest_effect_keeps_its_report_and_summary_bytes():
    """S16: no section, column, note or summary text of a smallest effect for a study that declares none."""
    assert report_text(undeclared_analysis()) == (DATA / "report-undeclared.md").read_text(encoding="utf-8")


def test_s16_a_declared_smallest_effect_is_an_object_of_its_contrast_in_analysis_json():
    """S16: the score lifts above average 1.64 / 8 = 0.205, all eight positive (p = 2/256, significant alone in its
    family): at or beyond a smallest effect of 0.15, so it meets. The cost's log ratios average 0.6296, below ln 2 =
    0.6931, with one of eight negative (ln 0.8, the second smallest in size: p = 2 x 3/256 = 0.0234, significant at
    alpha), so it falls short; its minimum detectable ratio is exp of its log-scale MDE."""
    text = TEXT.replace(
        "equivalence_margin = 0.3", 'equivalence_margin = 0.3\nsmallest_effect = 0.15\ndirection = "higher"'
    )
    text = text.replace('zero_denominator = "exclude"', 'zero_denominator = "exclude"\nsmallest_ratio = 2')
    contrasts = {c["name"]: c for c in json.loads(analysis_text(undeclared_analysis(text)))["contrasts"]}
    lift, cost = contrasts["score-a-vs-b"], contrasts["spend-per-win"]
    assert set(lift["smallest_effect"]) == {
        "form",
        "declared",
        "threshold",
        "direction",
        "decision",
        "interval",
        "detectable",
        "minimum_detectable_ratio",
    }
    assert lift["estimate"] == pytest.approx(0.205)
    assert {k: lift["smallest_effect"][k] for k in ("form", "declared", "threshold", "direction", "decision")} == {
        "form": "difference",
        "declared": 0.15,
        "threshold": 0.15,
        "direction": "higher",
        "decision": "meets",
    }
    assert lift["smallest_effect"]["detectable"] is (lift["minimum_detectable_effect"] <= 0.15)
    assert lift["smallest_effect"]["minimum_detectable_ratio"] is None
    assert cost["estimate"] == pytest.approx(sum(math.log(f) for f in FACTORS) / 8)
    assert cost["p_value"] == pytest.approx(6 / 256)
    rule = cost["smallest_effect"]
    assert (rule["form"], rule["declared"], rule["direction"], rule["decision"]) == (
        "ratio",
        2,
        "either",
        "falls_short",
    )
    assert rule["threshold"] == pytest.approx(math.log(2))
    assert rule["minimum_detectable_ratio"] == pytest.approx(math.exp(cost["minimum_detectable_effect"]))
    assert rule["detectable"] is (cost["minimum_detectable_effect"] <= math.log(2))
