"""O56: the `## Process visibility` section of a batch whose sandbox watched for hidden processes: its runs that are not
clean (a hidden process, memory killed as hidden, a memory reading that is unknown, so a limit condition that is
unknown), and the sensitivity analysis without them, read like the mount guard's; and nothing new for a batch that was
not watched."""

import dataclasses
import json

from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, ContrastResult, HolmDecision
from expcore.study.pipeline import _json_text, _visibility_summary
from expcore.visibility.flags import RunVisibility
from expcore.visibility.sensitivity import VisibilityAnalysis

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")
HEADING = "## Process visibility (detected, not prevented)"
SENSITIVITY = "#### Preregistered contrasts without the runs that hid from the watch or whose memory is unknown"
FIGURES = {"margin_bytes": 134217728, "start_unexplained_bytes": 0, "unexplained_peak_bytes": 805306368,
           "cgroup_anon_bytes": 900000000, "visible_anon_bytes": 94693632}  # fmt: skip


def contrast(name: str, conclusion: str, **changes) -> ContrastResult:
    base = {
        "name": name, "outcome": "score", "estimator": "wilcoxon_cluster_bootstrap", "cluster": "group",
        "arms": ("cond-a", "cond-b"), "factor": None, "estimate": 0.25, "p_value": 0.012, "ci_low": 0.1,
        "ci_high": 0.4, "n_clusters": 12, "n_clusters_dropped": 0, "n_pairs": 40, "n_pairs_missing": 0,
        "n_unpaired": 0, "tasks_dropped": (), "treatment_outcomes": None, "max_unscored_treatment": 0,
        "pairs_missing": dict.fromkeys(CAUSES, 0), "counts": {}, "per_model": (), "in_family": True,
        "exploratory": False, "significant": conclusion == "difference", "equivalence_margin": None,
        "equivalence_interval": None, "equivalent": False, "conclusion": conclusion, "minimum_detectable_effect": None,
    }  # fmt: skip
    return ContrastResult(**{**base, **changes})


def analysis(*results: ContrastResult) -> AnalysisResult:
    holm = tuple(HolmDecision(r.name, 0.01, 0.02, 0.025, True) for r in results if r.in_family)
    return AnalysisResult(tuple(results), holm, 0.05, {"preregistration_sha256": "e" * 64})


def export() -> dict:
    runs = [("k1", "cond-a"), ("k4", "cond-a"), ("k2", "cond-b"), ("k3", "cond-b")]
    return {
        "planned_runs": 4, "exhausted_runs": [], "unfinished_runs": [],
        "analysed_attempts": [
            {"run_key": k, "condition": c, "dispatch_seq": i, "factors": {"model": "model-a"}}
            for i, (k, c) in enumerate(runs)
        ],
        "infrastructure_by_condition": [], "retries_by_kind": [], "budget_holds": [],
    }  # fmt: skip


def render(result, **options):
    return render_report(result, export(), study="a-study", code=CODE, core_version="1.2.3", **options)


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


def run(key: str, condition: str = "cond-a", *, memory="within", unknown=(), hidden_process=0, hidden_memory=0,
        flags=()) -> RunVisibility:  # fmt: skip
    return RunVisibility(key, 1, condition, memory, tuple(unknown), hidden_process, hidden_memory, dict(FIGURES),
                         tuple(flags))  # fmt: skip


def watched(**changes) -> VisibilityAnalysis:
    """Four runs: k1's cell was killed for hidden memory, k2 had a hidden child, k3's memory could not be told its
    own, k4 is clean; the rerun keeps 37 of the primary's 40 pairs."""
    runs = (
        run("k1", memory="over", hidden_memory=1, flags=("hidden_memory",)),
        run("k2", "cond-b", memory="unknown", unknown=("hidden_process",), hidden_process=1,
            flags=("hidden_process", "memory_unknown")),
        run("k3", "cond-b", memory="unknown", unknown=("unattributed",), flags=("memory_unknown",)),
        run("k4"),
    )  # fmt: skip
    again = analysis(contrast("a-vs-b", "difference", n_pairs=37, estimate=0.19))
    return dataclasses.replace(VisibilityAnalysis(runs, again), **changes)


def test_o56_a_report_without_a_visibility_analysis_is_the_text_it_was():
    """O56: a batch not watched for hidden processes (a Docker host) gets no section: the same bytes as before."""
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result) == render(result, visibility=None)
    assert "Process visibility" not in render(result)
    assert summary_lines(result) == summary_lines(result, visibility=None)


def test_o56_the_section_counts_the_runs_that_are_not_clean_and_the_unknown_limit_conditions():
    report = render(analysis(contrast("a-vs-b", "difference")), visibility=watched())
    assert HEADING in report
    assert (
        "3 of 4 analysed run(s) not clean: 2 flagged (1 with a hidden process, 1 killed for hidden memory) and 1 "
        "unknown; 2 with an unknown memory reading, whose limit condition is unknown"
    ) in report
    by_condition = table_of(report, "### Process visibility by condition")
    assert by_condition["Condition"][1:] == ["Analysed", "Clean", "Flagged", "Unknown", "Hidden process",
                                             "Hidden memory", "Memory unknown", "Limit condition unknown"]  # fmt: skip
    assert by_condition["cond-a"][1:] == ["2", "1", "1", "0", "0", "1", "0", "0"]
    assert by_condition["cond-b"][1:] == ["2", "0", "1", "1", "1", "0", "2", "2"]


def test_o56_each_run_that_is_not_clean_is_listed_with_its_reading_never_within():
    rows = table_of(render(analysis(contrast("a-vs-b", "difference")), visibility=watched()), "### Runs that are not")
    assert set(rows) == {"Run key", "k1", "k2", "k3"}
    assert rows["k1"][3:6] == ["flagged", "hidden_memory", "over"]
    assert rows["k1"][8:10] == ["1", "805306368 / 134217728"]
    assert rows["k2"][3:8] == ["flagged", "hidden_process, memory_unknown", "unknown", "hidden_process", "1"]
    assert rows["k3"][3:7] == ["unknown", "memory_unknown", "unknown", "unattributed"]


def test_o56_the_sensitivity_analysis_is_shown_in_the_primary_analysis_s_own_tables():
    report = render(analysis(contrast("a-vs-b", "difference")), visibility=watched())
    assert "Left out: 3 run(s)." in report
    assert table_of(report, SENSITIVITY)["a-vs-b"][3] == "0.19"


def test_o56_with_every_run_clean_the_section_says_the_rerun_is_the_primary_analysis():
    report = render(analysis(contrast("a-vs-b", "difference")), visibility=VisibilityAnalysis((run("k1"),), None))
    assert "### Runs that are not clean\n\nNone:" in report
    assert SENSITIVITY not in report


def test_o56_the_section_comes_after_the_mount_guard_s_and_before_the_left_out_attempts():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=MountGuardAnalysis((), None),
                    visibility=watched())  # fmt: skip
    assert report.index("## Mount isolation") < report.index(HEADING) < report.index("## Attempts left out")


def test_o56_the_terminal_summary_says_the_runs_that_are_not_clean_and_each_contrast_without_them():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), visibility=watched())
    assert (
        "process visibility: 3 of 4 analysed run(s) not clean (2 flagged: 1 hidden process, 1 hidden memory; "
        "1 unknown); memory reading within 1, over 1, unknown 2"
    ) in lines
    assert lines[-1].startswith("visibility sensitivity a-vs-b: estimate 0.19")


def test_o56_analysis_json_holds_every_run_s_verdict_reading_and_figures():
    summary = json.loads(_json_text(_visibility_summary(watched())))
    assert summary["counts"]["left_out"] == 3
    k2 = next(r for r in summary["runs"] if r["run_key"] == "k2")
    assert (k2["verdict"], k2["memory"], k2["unknown"]) == ("flagged", "unknown", ["hidden_process"])
    assert summary["runs"][0]["figures"] == FIGURES
    assert summary["sensitivity"]["left_out"] == ["k1", "k2", "k3"]
