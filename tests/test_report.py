"""The Markdown report: each contrast with its significance, the exploratory ones apart, provenance, determinism."""

import dataclasses
import math

import pytest

from expcore.report import render_report, summary_lines
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
        "treatment_outcomes": None,
        "max_unscored_treatment": 0,
        "pairs_missing": {
            "treatment_outcome_none": 0,
            "treatment_excluded": 0,
            "exhausted": 0,
            "unpaired_or_left_out": 0,
            "outcome_none": 2,
        },
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
            {"run_key": "k1", "condition": "cond-a", "dispatch_seq": 1},
            {"run_key": "k4", "condition": "cond-a", "dispatch_seq": 4},
            {"run_key": "k2", "condition": "cond-b", "dispatch_seq": 2},
            {"run_key": "k3", "condition": "cond-b", "dispatch_seq": 3},
        ],
        "infrastructure_by_condition": [],
    }
    return {**base, **changes}


def render(result, data=None, **options):
    return render_report(result, data or export(), study="a-study", code=CODE, core_version="1.2.3", **options)


def row(report: str, name: str) -> list[str]:
    line = next(line for line in report.splitlines() if line.startswith(f"| {name} |"))
    return [cell.strip() for cell in line.strip("|").split(" | ")]


def table_of(report: str, heading: str) -> dict[str, list[str]]:
    """The first table after a heading, its header row included, keyed by the first cell of each row."""
    lines = report.split(heading, 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows = {}
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        if not line.startswith("|---"):
            cells = [cell.strip() for cell in line.strip("|").split(" | ")]
            rows[cells[0]] = cells
    return rows


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


def test_the_kernels_flag_decides_and_a_result_whose_flags_disagree_with_the_holm_decisions_is_refused():
    """No table or summary line can show an exploratory contrast as confirmatory, or the other way round."""
    member = contrast("in-family", "difference")
    outside = contrast("extra", "difference", in_family=False, exploratory=True)
    given_a_decision = AnalysisResult(
        (member, outside),
        tuple(HolmDecision(n, 0.01, 0.02, 0.025, True) for n in ("in-family", "extra")),
        0.05,
        dict(MANIFEST),
    )
    unflagged = analysis(member, dataclasses.replace(outside, exploratory=False), family=["in-family"])
    flagged_in_family = analysis(dataclasses.replace(member, exploratory=True), outside, family=["in-family"])
    for result in (given_a_decision, unflagged, flagged_in_family):
        with pytest.raises(ValueError, match="the flags must agree"):
            render(result)
        with pytest.raises(ValueError, match="the flags must agree"):
            summary_lines(result)


def test_without_an_exploratory_contrast_the_section_says_none_declared():
    report = render(analysis(contrast("primary", "not_detected")))
    assert (
        report.split("## Exploratory contrasts")[1].lstrip().startswith("(outside the Holm family)\n\nNone declared.")
    )


def test_a_factor_contrast_names_the_factor_and_its_levels():
    factor = contrast("by-factor", "difference", arms=("high", "low"), factor="gate", in_family=False, exploratory=True)
    assert row(render(analysis(factor, family=[])), "by-factor")[1] == "factor gate: high vs low"


def test_tasks_dropped_and_clusters_are_shown():
    dropped = contrast("primary", "difference", tasks_dropped=("alpha-task", "beta-task"))
    cells = row(render(analysis(dropped)), "primary")
    assert (cells[8], cells[11]) == ("12 (dropped 1)", "alpha-task, beta-task")


def test_the_minimum_detectable_effect_is_shown_when_present_and_the_column_is_absent_otherwise():
    with_power = render(analysis(contrast("primary", "difference", minimum_detectable_effect=0.31)))
    assert row(with_power, "primary")[12] == "0.31"
    assert "Min. detectable effect" not in render(analysis(contrast("primary", "difference")))
    unreachable = render(analysis(contrast("primary", "difference", minimum_detectable_effect=math.nan)))
    assert row(unreachable, "primary")[12] == "n/a"
    assert "Min. detectable effect: the smallest effect" in unreachable


def test_a_value_that_could_not_be_estimated_reads_n_a_and_never_zero():
    cells = row(render(analysis(contrast("primary", "not_detected", estimate=math.nan, p_value=math.nan))), "primary")
    assert cells[3] == "n/a"
    assert cells[5] == "n/a"


def test_the_terminal_summary_gives_each_conclusion_with_its_basis_and_labels_the_exploratory_ones():
    member = contrast("in-family", "difference", p_value=0.012)
    outside = contrast("extra", "equivalent", in_family=False, exploratory=True, estimate=math.nan, p_value=0.3)
    lines = summary_lines(analysis(member, outside, family=["in-family"]))
    assert lines == [
        "in-family: estimate 0.25, p 0.0120, Holm p 0.0200, difference",
        "extra: estimate n/a, p 0.3000, Holm p exploratory, equivalent (exploratory)",
    ]


def test_the_header_names_the_preregistration_hash_with_its_override_mark_the_code_and_the_core_version():
    marked = dataclasses.replace(
        analysis(contrast("primary", "difference")), manifest={"preregistration_sha256": "e" * 64 + "+override"}
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
    assert "| cond-a | 2 | 2 | 0 | 0 | 0 | 0 | 2.5 |" in conditions
    assert "| cond-b | 2 | 2 | 0 | 0 | 0 | 0 | 2.5 |" in conditions
    late = export(
        planned_runs=5,
        analysed_attempts=[{"run_key": f"l{s}", "condition": "late", "dispatch_seq": s} for s in (7, 9, 30)]
        + [
            {"run_key": "early-run", "condition": "early", "dispatch_seq": 1},
            {"run_key": "g0", "condition": "legacy", "dispatch_seq": None},
        ],
    )
    conditions = render(analysis(contrast("primary", "difference")), late).split("## Conditions")[1]
    assert "| late | 3 | 3 | 0 | 0 | 0 | 0 | 9 |" in conditions
    assert "| early | 1 | 1 | 0 | 0 | 0 | 0 | 1 |" in conditions
    assert "| legacy | 1 | 1 | 0 | 0 | 0 | 0 | n/a |" in conditions  # a position not kept is unknown, not zero


def test_set_aside_and_treatment_counts_are_listed_per_condition_and_kind():
    data = export(
        infrastructure_by_condition=[
            {"condition": "cond-a", "kind": "endpoint", "declared_treatment": False, "set_aside": 3, "treatment": 0},
            {"condition": "cond-b", "kind": "knowledge", "declared_treatment": True, "set_aside": 1, "treatment": 2},
        ]
    )
    report = render(analysis(contrast("primary", "difference")), data)
    assert "| cond-a | endpoint | no | 3 | 0 | 0 |" in report
    assert "| cond-b | knowledge | yes | 1 | 2 | 0 |" in report
    assert "| cond-a | none |" not in report  # a condition with a row of its own needs no row of zeros


def test_c23_each_condition_shows_its_planned_runs_how_each_ended_and_its_attempts_set_aside():
    """C23: planned is analysed + exhausted + unfinished + left out, per condition, so an exhausted run is seen under
    its own condition and not only in the header; set-aside attempts are summed over the condition's kinds."""
    data = export(
        planned_runs=8,
        exhausted_runs=[{"run_key": "k5", "condition": "cond-a"}],
        unfinished_runs=[{"run_key": "k6", "condition": "cond-b"}, {"run_key": "k7", "condition": "cond-c"}],
        partial=True,
        running_attempts=0,
        excluded_attempts=[{"run_key": "k8", "condition": "cond-c", "attempt": 2, "reason": "no record"}],
        infrastructure_by_condition=[
            {"condition": "cond-a", "kind": "endpoint", "declared_treatment": False, "set_aside": 3, "treatment": 0},
            {"condition": "cond-a", "kind": "sandbox", "declared_treatment": False, "set_aside": 2, "treatment": 0},
        ],
    )
    conditions = table_of(render(analysis(contrast("primary", "difference")), data), "## Conditions")
    header = ["Planned", "Analysed", "Exhausted", "Unfinished", "Left out", "Attempts set aside"]
    assert conditions["Condition"][1:7] == header
    assert conditions["cond-a"][1:7] == ["3", "2", "1", "0", "0", "5"]
    assert conditions["cond-b"][1:7] == ["3", "2", "0", "1", "0", "0"]
    assert conditions["cond-c"][1:8] == ["2", "0", "0", "1", "1", "0", "n/a"]  # nothing analysed: no position


def test_c23_every_condition_is_in_the_infrastructure_table_with_a_row_of_zeros_when_nothing_failed():
    """C23: a condition that met no infrastructure failure has a row of zeros, so 0 is not mistaken for unreported;
    that includes a condition known only from a run that was not analysed."""
    data = export(planned_runs=5, exhausted_runs=[{"run_key": "k5", "condition": "cond-c"}])
    kinds = render(analysis(contrast("primary", "difference")), data).split("### Infrastructure failures")[1]
    for name in ("cond-a", "cond-b", "cond-c"):
        assert f"| {name} | none | no | 0 | 0 | 0 |" in kinds
    assert "No infrastructure failure." not in kinds


def test_c23_an_export_whose_run_lists_do_not_add_up_to_the_planned_runs_is_refused():
    """C23: the per-condition counts partition the plan; an export that loses a run is not reported as complete."""
    with pytest.raises(ValueError, match="planned_runs is 5, but its run lists hold 4 runs"):
        render(analysis(contrast("primary", "difference")), export(planned_runs=5))


def test_c23_each_contrast_shows_the_pairs_it_used_and_the_pairs_it_is_missing_by_cause():
    """C23: pairs used in the contrast's own row, and every cause of a missing pair in a table of its own, zeros
    included, for every contrast, exploratory ones too."""
    missing = {
        "treatment_outcome_none": 1,
        "treatment_excluded": 0,
        "exhausted": 2,
        "unpaired_or_left_out": 3,
        "outcome_none": 4,
    }
    member = contrast("primary", "difference", n_pairs=14, pairs_missing=missing, treatment_outcomes="scored")
    other = contrast("extra", "difference", in_family=False, exploratory=True)
    report = render(analysis(member, other, family=["primary"]))
    assert row(report, "primary")[9] == "14"
    family = table_of(report, "### Pairs of the family's contrasts")
    assert family["Contrast"] == [
        "Contrast",
        "Pairs used",
        "Treatment outcome None",
        "Treatment excluded",
        "Exhausted run",
        "Unpaired or left out",
        "Outcome None (other)",
    ]
    assert set(family) == {"Contrast", "primary"}  # an exploratory contrast is never in a table of the family
    assert family["primary"] == ["primary", "14", "1", "0", "2", "3", "4"]
    assert table_of(report, "### Pairs of the exploratory contrasts")["extra"] == [
        "extra",
        "40",
        "0",
        "0",
        "0",
        "0",
        "2",
    ]
    assert "- *Exhausted run*: the run of either arm used all its attempts" in report.split("## Missing pairs")[1]


def test_s10_a_contrast_that_excludes_treatment_attempts_is_named_beside_its_result_with_how_many_it_left_out():
    """S10: an excluded outage must not read as the absence of an outage effect, so the contrast's own row says that
    it excludes treatment attempts and how many pairs that left out; the other declarations and an undeclared loss
    are shown the same way."""
    zero = dict.fromkeys(("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out"), 0)
    excluded = contrast(
        "outage-excluded",
        "not_detected",
        treatment_outcomes="excluded",
        pairs_missing={**zero, "treatment_excluded": 2, "outcome_none": 0},
    )
    scored = contrast("outage-scored", "difference", treatment_outcomes="scored")
    lost = contrast(
        "outage-lost",
        "not_detected",
        in_family=False,
        exploratory=True,
        pairs_missing={**zero, "treatment_outcome_none": 3, "outcome_none": 0},
    )
    plain = contrast("no-outage", "difference")
    report = render(analysis(excluded, scored, lost, plain, family=["outage-excluded", "outage-scored", "no-outage"]))
    cells = row(report, "outage-excluded")
    assert cells[7:11] == ["not_detected", "12 (dropped 1)", "40", "excluded (2 pair(s) left out)"]
    assert row(report, "outage-scored")[10] == "scored"
    assert row(report, "outage-lost")[10] == "not declared (3 pair(s) None)"
    assert row(report, "no-outage")[10] == "-"
    assert "Treatment attempts:" in report  # what the column says is stated beside the table


def test_runs_that_were_not_analysed_are_counted_in_the_header():
    data = export(
        planned_runs=7,
        exhausted_runs=[{"condition": "cond-a"}],
        unfinished_runs=[{"condition": "cond-b"}, {"condition": "cond-b"}],
    )
    expected = (
        "4 analysed, 1 exhausted (charged infrastructure attempts reached the limit; no scored attempt), "
        "2 unfinished, 0 left out"
    )
    assert expected in render(analysis(contrast("primary", "difference")), data)


def test_the_interval_level_is_named_when_given():
    assert "95% interval" in render(analysis(contrast("primary", "difference")), interval_level=0.95)


def test_the_same_inputs_give_the_same_text_and_a_conclusion_outside_the_vocabulary_is_refused():
    result = analysis(contrast("primary", "difference"))
    assert render(result) == render(result)
    bad = dataclasses.replace(contrast("primary", "difference"), conclusion="no_difference")
    with pytest.raises(ValueError, match="no_difference"):
        render(analysis(bad))


def left_out(run_key: str, condition: str = "cond-a", attempt: int = 1) -> dict:
    return {
        "run_key": run_key,
        "condition": condition,
        "attempt": attempt,
        "reason": "no replayed record covers its final attempt",
    }


def partial_export() -> dict:
    """The export of a partial analysis: k4 is left out, and the attempts of its run are counted apart."""
    data = export(partial=True, running_attempts=2, excluded_attempts=[left_out("k4")])
    data["analysed_attempts"] = [e for e in data["analysed_attempts"] if e["run_key"] != "k4"]
    data["planned_runs"] = 4
    data["infrastructure_by_condition"] = [
        {
            "condition": "cond-a",
            "kind": "knowledge",
            "declared_treatment": True,
            "set_aside": 1,
            "treatment": 0,
            "excluded": 2,
        }
    ]
    return data


def test_a_partial_analysis_says_so_and_lists_each_attempt_it_leaves_out_with_the_reason():
    report = render(analysis(contrast("primary", "difference")), partial_export())
    assert "**Partial analysis**: yes. 2 attempt(s) were still running when the replay check was made" in report
    assert "1 run(s) are left out of the analysis" in report
    assert "3 analysed" in report
    assert "1 left out of the analysis" in report
    listed = report.split("## Attempts left out of the analysis")[1]
    assert "| k4 | 1 | no replayed record covers its final attempt |" in listed


def test_every_count_comes_from_the_analysed_population_and_the_left_out_ones_have_a_column_of_their_own():
    report = render(analysis(contrast("primary", "difference")), partial_export())
    conditions = report.split("## Conditions")[1]
    assert "| cond-a | 2 | 1 | 0 | 0 | 1 | 1 | 1 |" in conditions  # one analysed, one left out, the analysed median
    assert "| cond-b | 2 | 2 | 0 | 0 | 0 | 0 | 2.5 |" in conditions
    assert "| cond-a | knowledge | yes | 1 | 0 | 2 |" in conditions  # the two excluded attempts are not "scored"


def test_a_complete_analysis_says_it_is_not_partial_and_leaves_nothing_out():
    report = render(analysis(contrast("primary", "difference")))
    assert "- Partial analysis: no" in report
    assert report.split("## Attempts left out of the analysis")[1].strip() == "None."


def test_a_report_of_a_partial_analysis_and_one_of_a_complete_analysis_are_different_texts():
    result = analysis(contrast("primary", "difference"))
    assert render(result, partial_export()) != render(result)


def test_c09_set_aside_counts_are_described_as_covering_every_run_not_only_the_analysed_ones():
    """C09: a pending run's set-aside attempt is counted, and the table says the count covers every run, finished
    or not, so it is not mistaken for part of the analysed population."""
    data = export(
        planned_runs=5,
        unfinished_runs=[{"run_key": "k5", "condition": "cond-a"}],
        infrastructure_by_condition=[
            {
                "condition": "cond-a",
                "kind": "endpoint",
                "declared_treatment": False,
                "set_aside": 1,
                "treatment": 0,
                "excluded": 0,
            }
        ],
    )
    report = render(analysis(contrast("primary", "difference")), data)
    assert "Set aside (all runs)" in report
    assert "Counted over every run of the campaign whatever its status" in report
    assert "- *Left out*: the set-aside and treatment attempts of runs a partial analysis leaves out" in report
    assert "every attempt failed on infrastructure" not in report


def test_r13_the_report_names_each_scorer_with_its_version_or_says_it_does_not_know_them():
    """R13: the version of every scorer that made the numbers is in the header, in name order."""
    given = render(analysis(contrast("primary", "difference")), scorers={"tiny": "2", "alpha": "sha256:ab"})
    assert "- Scorers: alpha `sha256:ab`, tiny `2`" in given
    assert "- Scorers: n/a" in render(analysis(contrast("primary", "difference")))


def test_s10_a_scored_contrast_that_used_its_tolerance_shows_the_pairs_it_lost_and_the_number_it_was_allowed():
    """S10: a preregistered tolerance is used in the open: the row gives what it cost and what was allowed."""
    zero = dict.fromkeys(("treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none"), 0)
    tolerated = contrast(
        "outage",
        "difference",
        treatment_outcomes="scored",
        max_unscored_treatment=2,
        pairs_missing={**zero, "treatment_outcome_none": 1},
    )
    report = render(analysis(tolerated, family=["outage"]))
    assert row(report, "outage")[10] == "scored (1 pair(s) None, at most 2 preregistered)"
