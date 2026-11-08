"""The Markdown report: each contrast with its significance, the exploratory ones apart, provenance, determinism."""

import dataclasses
import math

import pytest

from expcore.report import render_report
from expcore.stats.analysis import CONCLUSIONS, AnalysisResult, ContrastResult, HolmDecision

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
MANIFEST = {"preregistration_sha256": "e" * 64, "factor_schema_sha256": "f" * 64}


def contrast(name: str, conclusion: str, **changes) -> ContrastResult:
    """A contrast result with plausible numbers; `changes` replace fields."""
    base = {
        "name": name,
        "outcome": "score",
        "estimator": "wilcoxon_cluster_bootstrap",
        "cluster": "group",
        "arms": ("cond-a", "cond-b"),
        "factor": None,
        "estimate": 0.25,
        "p_value": 0.012,
        "ci_low": 0.1,
        "ci_high": 0.4,
        "n_clusters": 12,
        "n_clusters_dropped": 1,
        "n_pairs": 40,
        "n_pairs_missing": 2,
        "n_unpaired": 0,
        "tasks_dropped": (),
        "counts": {},
        "per_model": (),
        "in_family": True,
        "exploratory": False,
        "significant": conclusion in ("difference", "trivial_difference"),
        "equivalence_margin": None,
        "equivalence_interval": None,
        "equivalent": conclusion in ("equivalent", "trivial_difference"),
        "conclusion": conclusion,
        "minimum_detectable_effect": None,
    }
    return ContrastResult(**{**base, **changes})


def analysis(*results: ContrastResult, family=None) -> AnalysisResult:
    family = [r.name for r in results if r.in_family] if family is None else family
    holm = tuple(HolmDecision(n, 0.01, 0.02, 0.025, True) for n in family)
    return AnalysisResult(tuple(results), holm, 0.05, dict(MANIFEST))


def export(**changes):
    base = {
        "planned_runs": 4,
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [
            {"condition": "cond-a", "dispatch_seq": 1},
            {"condition": "cond-a", "dispatch_seq": 4},
            {"condition": "cond-b", "dispatch_seq": 2},
            {"condition": "cond-b", "dispatch_seq": 3},
        ],
        "infrastructure_by_condition": [],
    }
    return {**base, **changes}


def render(result, data=None, **options):
    return render_report(result, data or export(), study="a-study", code=CODE, core_version="1.2.3", **options)


def row(report: str, name: str) -> list[str]:
    line = next(line for line in report.splitlines() if line.startswith(f"| {name} |"))
    return [cell.strip() for cell in line.strip("|").split(" | ")]


@pytest.mark.parametrize("conclusion", CONCLUSIONS)
def test_each_conclusion_word_is_shown_in_the_row_of_its_contrast_beside_the_raw_and_the_holm_p_value(conclusion):
    report = render(analysis(contrast("primary", conclusion)))
    cells = row(report, "primary")
    assert cells[5:8] == ["0.0120", "0.0200", conclusion]  # p, Holm p, conclusion: never apart


def test_a_contrast_outside_the_family_is_exploratory_in_a_section_of_its_own_and_in_its_row():
    members = contrast("in-family", "difference")
    other = contrast("extra", "difference", in_family=False, exploratory=True)
    report = render(analysis(members, other, family=["in-family"]))
    family_part, exploratory_part = report.split("## Exploratory contrasts")
    assert "in-family" in family_part
    assert "extra" not in family_part
    assert "extra" in exploratory_part
    assert "in-family" not in exploratory_part
    cells = row(report, "extra")
    assert (cells[6], cells[7]) == ("exploratory", "difference (exploratory)")  # never a bare conclusion


def test_without_an_exploratory_contrast_the_section_says_none_declared():
    report = render(analysis(contrast("primary", "not_detected")))
    assert (
        report.split("## Exploratory contrasts")[1].lstrip().startswith("(outside the Holm family)\n\nNone declared.")
    )


def test_a_factor_contrast_names_the_factor_and_its_levels():
    factor = contrast("by-factor", "difference", arms=("high", "low"), factor="gate", in_family=False)
    assert row(render(analysis(factor, family=[])), "by-factor")[1] == "factor gate: high vs low"


def test_tasks_dropped_and_clusters_are_shown():
    dropped = contrast("primary", "difference", tasks_dropped=("alpha-task", "beta-task"))
    cells = row(render(analysis(dropped)), "primary")
    assert (cells[8], cells[9]) == ("12 (dropped 1)", "alpha-task, beta-task")


def test_the_minimum_detectable_effect_is_shown_when_present_and_the_column_is_absent_otherwise():
    with_power = render(analysis(contrast("primary", "difference", minimum_detectable_effect=0.31)))
    assert row(with_power, "primary")[10] == "0.31"
    assert "Min. detectable effect" not in render(analysis(contrast("primary", "difference")))
    unreachable = render(analysis(contrast("primary", "difference", minimum_detectable_effect=math.nan)))
    assert row(unreachable, "primary")[10] == "n/a"
    assert "Min. detectable effect: the smallest effect" in unreachable


def test_a_value_that_could_not_be_estimated_reads_n_a_and_never_zero():
    cells = row(render(analysis(contrast("primary", "not_detected", estimate=math.nan, p_value=math.nan))), "primary")
    assert cells[3] == "n/a"
    assert cells[5] == "n/a"


def test_the_header_names_the_preregistration_hash_with_its_override_mark_the_code_and_the_core_version():
    marked = AnalysisResult(
        (contrast("primary", "difference"),), (), 0.05, {"preregistration_sha256": "e" * 64 + "+override"}
    )
    report = render(marked)
    assert f"`{'e' * 64}+override`" in report
    assert f"commit `{'c' * 40}`" in report
    assert f"tree `{'d' * 64}`" in report
    assert "(clean)" in report
    assert "Core version: 1.2.3" in report
    dirty = render_report(
        analysis(contrast("primary", "difference")),
        export(),
        study="s",
        code={**CODE, "dirty": True, "why": "uncommitted changes"},
        core_version=None,
    )
    assert "dirty: uncommitted changes" in dirty
    assert "Core version: n/a" in dirty


def test_each_condition_has_its_runs_and_the_median_dispatch_position_of_its_attempts():
    report = render(analysis(contrast("primary", "difference")))
    conditions = report.split("## Conditions")[1]
    assert "| cond-a | 2 | 2.5 |" in conditions
    assert "| cond-b | 2 | 2.5 |" in conditions
    late = export(
        analysed_attempts=[{"condition": "late", "dispatch_seq": s} for s in (7, 9, 30)]
        + [{"condition": "early", "dispatch_seq": 1}, {"condition": "legacy", "dispatch_seq": None}]
    )
    conditions = render(analysis(contrast("primary", "difference")), late).split("## Conditions")[1]
    assert "| late | 3 | 9 |" in conditions
    assert "| early | 1 | 1 |" in conditions
    assert "| legacy | 1 | n/a |" in conditions  # a position the database did not keep is unknown, not zero


def test_set_aside_and_treatment_counts_are_listed_per_condition_and_kind():
    data = export(
        infrastructure_by_condition=[
            {"condition": "cond-a", "kind": "endpoint", "declared_treatment": False, "set_aside": 3, "treatment": 0},
            {"condition": "cond-b", "kind": "knowledge", "declared_treatment": True, "set_aside": 1, "treatment": 2},
        ]
    )
    report = render(analysis(contrast("primary", "difference")), data)
    assert "| cond-a | endpoint | no | 3 | 0 |" in report
    assert "| cond-b | knowledge | yes | 1 | 2 |" in report
    assert "No infrastructure failure." in render(analysis(contrast("primary", "difference")))


def test_runs_that_were_not_analysed_are_counted_in_the_header():
    data = export(
        exhausted_runs=[{"condition": "cond-a"}], unfinished_runs=[{"condition": "cond-b"}, {"condition": "cond-b"}]
    )
    assert "4 analysed, 1 exhausted (every attempt failed on infrastructure), 2 unfinished" in render(
        analysis(contrast("primary", "difference")), data
    )


def test_the_interval_level_is_named_when_given():
    assert "95% interval" in render(analysis(contrast("primary", "difference")), interval_level=0.95)


def test_the_same_inputs_give_the_same_text_and_a_conclusion_outside_the_vocabulary_is_refused():
    result = analysis(contrast("primary", "difference"))
    assert render(result) == render(result)
    bad = dataclasses.replace(contrast("primary", "difference"), conclusion="no_difference")
    with pytest.raises(ValueError, match="no_difference"):
        render(analysis(bad))
