"""S18: a study that declares no [equivalence_families] keeps the bytes of its analysis.json and its report.

`tests/data/analysis-equivalence-within-families.json`, `tests/data/report-equivalence-within-families.md`,
`tests/data/analysis-equivalence-flat.json` and `tests/data/report-equivalence-flat.md` were made by the code before
`[equivalence_families]` existed, from the deterministic analyses below: a flat `equivalence_family` of three claims,
under two named Holm families (#277's layout: each claim adjusted within its own Holm family) and under one flat Holm
family (#244's: the three adjusted together). A change of these bytes is a change every existing study would see.
"""

import tomllib
from pathlib import Path

from expcore.campaign.factors import FactorSchema
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, analyse
from expcore.stats.prereg import parse
from expcore.stats.rows import Row
from expcore.study.pipeline import _holm_entries, _json_text

DATA = Path(__file__).resolve().parents[1] / "data"
ARMS = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
NAMED = '[holm_families]\nphase1 = ["near", "wide"]\nphase2 = ["edge"]\n'
FLAT = 'holm_family = ["near", "wide", "edge"]\n'
TEXT = """\
alpha = 0.05
equivalence_family = ["near", "wide", "edge"]
FAMILIES
[bootstrap]
replicates = 200
seed = 11

[contrasts.near]
conditions = ["cond-a", "cond-b"]
outcome = "near"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.2

[contrasts.wide]
conditions = ["cond-a", "cond-b"]
outcome = "wide"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.2

[contrasts.edge]
conditions = ["cond-a", "cond-b"]
outcome = "edge"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
equivalence_margin = 0.2

[contrasts.side]
conditions = ["cond-a", "cond-b"]
outcome = "near"
estimator = "wilcoxon_cluster_bootstrap"
cluster = "group"
"""
LIFTS = {  # arm A's outcome above arm B's, per cluster: a tight one, a wider one, one near the margin
    "near": (0.01, -0.02, 0.03, 0.0, -0.01, 0.02, 0.015, -0.005),
    "wide": (0.05, 0.2, -0.04, 0.12, 0.18, 0.02, 0.1, 0.15),
    "edge": (0.02, 0.3, -0.05, 0.25, 0.1, 0.22, 0.04, 0.12),
}
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


def equivalence_analysis(families: str) -> AnalysisResult:
    """The analysis the fixtures were made from: eight clusters, two tasks each, deterministic rows and seeds."""
    rows = []
    for i in range(8):
        for task in (f"t{i}a", f"t{i}b"):
            for arm in ("cond-a", "cond-b"):
                outcomes = {name: 0.4 + (lifts[i] if arm == "cond-a" else 0.0) for name, lifts in LIFTS.items()}
                run_key = f"task={task}|model=model-a|seed=1|condition={arm}"
                factors = {"task": task, "model": "model-a", "seed": 1}
                rows.append(Row(run_key, arm, factors, outcomes, {"group": f"c{i}"}))
    text = TEXT.replace("FAMILIES", families)
    return analyse(rows, parse(tomllib.loads(text), "inline", "b" * 64, ARMS))


def analysis_text(result: AnalysisResult) -> str:
    """What analysis.json holds of the analysis: its contrasts and its Holm entries, as the pipeline writes them."""
    return _json_text({"contrasts": result.contrasts, **_holm_entries(result), "alpha": result.alpha})


def report_text(result: AnalysisResult) -> str:
    """The report and the terminal summary of the analysis."""
    report = render_report(result, EXPORT, study="a-study", code=CODE, core_version="1.2.3", interval_level=0.95)
    return report + "\n".join(summary_lines(result)) + "\n"


def test_s18_equivalence_claims_within_named_holm_families_keep_their_analysis_json_bytes():
    """S18: without [equivalence_families], #277's layout (each claim adjusted within its own Holm family) writes
    the analysis.json it wrote before the table existed."""
    expected = (DATA / "analysis-equivalence-within-families.json").read_text(encoding="utf-8")
    assert analysis_text(equivalence_analysis(NAMED)) == expected


def test_s18_equivalence_claims_within_named_holm_families_keep_their_report_and_summary_bytes():
    """S18: and its report and terminal summary, the equivalence table with its Holm family column."""
    expected = (DATA / "report-equivalence-within-families.md").read_text(encoding="utf-8")
    assert report_text(equivalence_analysis(NAMED)) == expected


def test_s18_a_flat_equivalence_family_under_one_flat_holm_family_keeps_its_analysis_json_bytes():
    """S18: #244's layout (one flat Holm family, the claims adjusted together) writes the analysis.json it wrote."""
    assert analysis_text(equivalence_analysis(FLAT)) == (DATA / "analysis-equivalence-flat.json").read_text("utf-8")


def test_s18_a_flat_equivalence_family_under_one_flat_holm_family_keeps_its_report_and_summary_bytes():
    """S18: and its report and terminal summary."""
    assert report_text(equivalence_analysis(FLAT)) == (DATA / "report-equivalence-flat.md").read_text("utf-8")
