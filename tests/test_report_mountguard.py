"""The `## Mount isolation` section of a batch whose mount was guarded (O60): its runs that are not clean and the
sensitivity analysis without them, read like the egress section; and nothing new for a batch that was not guarded."""

import dataclasses

from expcore.egress.sensitivity import EgressAnalysis
from expcore.mountguard.flags import RunMountGuard
from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, ContrastResult, HolmDecision

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")
HEADING = "## Mount isolation (detected, not prevented)"
SENSITIVITY = "#### Preregistered contrasts without the runs that are not clean"


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


def analysis(*results: ContrastResult, family=None) -> AnalysisResult:
    family = [r.name for r in results if r.in_family] if family is None else family
    holm = tuple(HolmDecision(n, 0.01, 0.02, 0.025, True) for n in family)
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


def run(key: str, condition: str = "cond-a", *, flags=(), log="intact", unknown=(), isolation="proven",
        unproven=(), accesses=None, traced=0) -> RunMountGuard:  # fmt: skip
    return RunMountGuard(key, 1, condition, log, tuple(unknown), isolation, tuple(unproven),
                         accesses or {"token_file": 0, "token_dir": 0}, traced, tuple(flags))  # fmt: skip


def guarded(**changes) -> MountGuardAnalysis:
    """Four runs: k1 touched the token's directory, k2's log was never closed, k3's isolation is unproven, k4 is
    clean; the rerun keeps 37 of the primary's 40 pairs."""
    runs = (
        run("k1", flags=("token_access",), accesses={"token_file": 0, "token_dir": 3}),
        run("k2", "cond-b", flags=("log_unknown", "isolation_unproven"), log="unknown", unknown=("not_closed",),
            isolation="unproven", unproven=("log_incomplete",)),
        run("k3", "cond-b", flags=("isolation_unproven",), isolation="unproven",
            unproven=("proof_failed", "same_pid_namespace")),
        run("k4"),
    )  # fmt: skip
    again = analysis(contrast("a-vs-b", "difference", n_pairs=37, estimate=0.19))
    return dataclasses.replace(MountGuardAnalysis(runs, again), **changes)


def test_a_report_without_a_mount_guard_analysis_is_the_text_it_was():
    """O60: a batch whose mount was not guarded (a Docker host) gets no section: the same bytes as before."""
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result) == render(result, mount_guard=None)
    assert "Mount isolation" not in render(result)


def test_the_section_counts_the_runs_that_are_not_clean_by_verdict_and_flag():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded())
    assert HEADING in report
    assert "3 of 4 analysed run(s) not clean: 1 flagged for an access (1 with an access to the token's" in report
    by_condition = table_of(report, "### Mount isolation by condition")
    assert by_condition["Condition"][1:] == ["Analysed", "Clean", "Flagged", "Unknown", "Token access",
                                             "Process access", "Log unknown", "Isolation unproven"]  # fmt: skip
    assert by_condition["cond-a"][1:] == ["2", "1", "1", "0", "1", "0", "0", "0"]
    assert by_condition["cond-b"][1:] == ["2", "0", "0", "2", "0", "0", "1", "2"]


def test_each_run_that_is_not_clean_is_listed_with_its_verdict_flags_and_reasons():
    rows = table_of(render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded()), "### Runs that are not")
    assert set(rows) == {"Run key", "k1", "k2", "k3"}
    assert rows["k1"][3:5] == ["flagged", "token_access"]
    assert rows["k1"][9] == "0 / 3"
    assert rows["k2"][3:7] == ["unknown", "log_unknown, isolation_unproven", "unknown", "not_closed"]
    assert rows["k3"][7:9] == ["unproven", "proof_failed, same_pid_namespace"]


def test_the_sensitivity_analysis_is_shown_in_the_primary_analysiss_own_tables():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded())
    assert "Left out: 3 run(s)." in report
    assert table_of(report, SENSITIVITY)["a-vs-b"][3] == "0.19"
    assert table_of(report, "#### Pairs without the runs that are not clean")["a-vs-b"][1] == "37"


def test_with_every_run_clean_the_section_says_the_rerun_is_the_primary_analysis():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=MountGuardAnalysis((run("k1"),), None))
    assert "### Runs that are not clean\n\nNone:" in report
    assert "Every run is clean, so the analysis without the others is the analysis above." in report
    assert SENSITIVITY not in report


def test_a_refused_rerun_is_said_and_no_table_is_shown():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded(sensitivity=None, refused="why"))
    assert "Not computed: the analysis refused the rerun (why)." in report
    assert SENSITIVITY not in report


def test_the_egress_section_keeps_its_own_titles_beside_the_mount_section():
    egress = EgressAnalysis((), None)
    report = render(analysis(contrast("a-vs-b", "difference")), egress=egress, mount_guard=guarded())
    assert report.index("## Egress (detected, not prevented)") < report.index(HEADING)
    assert "No run is flagged, so the analysis without them is the analysis above." in report


def test_the_terminal_summary_says_the_runs_that_are_not_clean_and_each_contrast_without_them():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), None, guarded())
    assert (
        "mount guard: 3 of 4 analysed run(s) not clean (1 flagged: 1 token access, 0 process access; 2 unknown: "
        "1 log unknown, 2 isolation unproven)"
    ) in lines
    assert lines[-1].startswith("mount sensitivity a-vs-b: estimate 0.19, p 0.0120, Holm p 0.0200, difference")


def test_the_terminal_summary_is_unchanged_without_a_mount_guard_analysis():
    result = analysis(contrast("a-vs-b", "difference"))
    assert summary_lines(result) == summary_lines(result, None, None)
    assert not any(line.startswith("mount") for line in summary_lines(result))


def test_the_section_comes_after_the_conditions_and_before_the_left_out_attempts():
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded())
    assert report.index("## Conditions") < report.index(HEADING) < report.index("## Attempts left out")


BOTH_HEADING = "## Egress and mount isolation together (detected, not prevented)"
BOTH_SENSITIVITY = "#### Preregistered contrasts without the runs either guard leaves out"


def combined(**changes):
    """k1 left out by egress alone, k2 by the mount guard alone (unknown to it), k3 by both, k4 clean to both; the
    rerun keeps 34 of the primary's 40 pairs."""
    from expcore.guards import GuardedRun, GuardsAnalysis

    runs = (
        GuardedRun("k1", 1, "cond-a", "flagged", "clean"),
        GuardedRun("k2", 1, "cond-b", "clean", "unknown"),
        GuardedRun("k3", 1, "cond-b", "unknown", "flagged"),
        GuardedRun("k4", 1, "cond-a", "clean", "clean"),
    )
    again = analysis(contrast("a-vs-b", "difference", n_pairs=34, estimate=0.17))
    return dataclasses.replace(GuardsAnalysis(runs, again), **changes)


def test_the_combined_section_counts_the_runs_either_guard_leaves_out_by_condition_and_guard():
    """O60: beside each guard's own rerun, the one without every run either leaves out, with its counts."""
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded(), guards=combined())
    assert report.index(HEADING) < report.index(BOTH_HEADING) < report.index("## Attempts left out")
    assert (
        "3 of 4 analysed run(s) left out by either guard: 1 by egress alone, 1 by the mount guard alone, 1 by both."
        in report
    )
    by_condition = table_of(report, "### Left out by either guard, by condition")
    assert by_condition["cond-a"][1:] == ["2", "1", "1", "0", "0"]
    assert by_condition["cond-b"][1:] == ["2", "2", "0", "1", "1"]
    rows = table_of(report, "### Runs either guard leaves out")
    assert rows["k2"][3:] == ["clean", "unknown", "mount guard"]
    assert rows["k3"][3:] == ["unknown", "flagged", "both"]
    assert table_of(report, BOTH_SENSITIVITY)["a-vs-b"][3] == "0.17"
    assert table_of(report, "#### Pairs without the runs either guard leaves out")["a-vs-b"][1] == "34"


def test_without_a_combined_analysis_the_report_is_the_text_it_was():
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result, mount_guard=guarded()) == render(result, mount_guard=guarded(), guards=None)
    assert BOTH_HEADING not in render(result, mount_guard=guarded())


def test_a_refused_combined_rerun_is_said():
    report = render(analysis(contrast("a-vs-b", "difference")), guards=combined(sensitivity=None, refused="why"))
    assert "Not computed: the analysis refused the rerun (why)." in report
    assert BOTH_SENSITIVITY not in report


def test_the_terminal_summary_says_the_runs_either_guard_leaves_out_and_each_contrast_without_them():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), None, None, combined())
    assert (
        "both guards: 3 of 4 analysed run(s) left out (1 by egress alone, 1 by the mount guard alone, 1 by both)"
        in lines
    )
    assert lines[-1].startswith("both-guards sensitivity a-vs-b: estimate 0.17, p 0.0120, Holm p 0.0200, difference")
