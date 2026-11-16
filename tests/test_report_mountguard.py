"""The `## Mount isolation` section of a batch whose mount was guarded (O60): its runs that are not clean and the
sensitivity analysis without them, read like the egress section; and nothing new for a batch that was not guarded."""

import dataclasses
import json

from family_support import named_families

from expcore.egress.sensitivity import EgressAnalysis
from expcore.mountguard.flags import RunMountGuard
from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, ContrastResult, HolmDecision
from expcore.study.pipeline import _guards_summary, _json_text, _mount_guard_summary

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


def no_mount(key: str, condition: str = "cond-a", kind: str = "rest") -> RunMountGuard:
    """A run whose record says it had no mount: the guard does not apply."""
    return RunMountGuard(key, 1, condition, "not_applicable", (), "not_applicable", (),
                         {"token_file": 0, "token_dir": 0}, 0, (), kind)  # fmt: skip


def mixed(**changes) -> MountGuardAnalysis:
    """k1 touched the token's directory (a mount run), k2 and k3 are REST runs, k4 is a clean mount run; the rerun keeps
    39 of the primary's 40 pairs, the runs with no mount among them."""
    runs = (
        run("k1", flags=("token_access",), accesses={"token_file": 0, "token_dir": 3}),
        no_mount("k2", "cond-b"),
        no_mount("k3", "cond-b", "none"),
        run("k4"),
    )
    again = analysis(contrast("a-vs-b", "difference", n_pairs=39, estimate=0.21))
    return dataclasses.replace(MountGuardAnalysis(runs, again), **changes)


def test_o69_the_runs_with_no_mount_are_counted_and_shown_as_not_applicable_and_never_as_clean():
    """O69: the report counts them apart, lists none of them as not clean, and the rerun keeps them."""
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=mixed())
    assert "1 of 4 analysed run(s) not clean: 1 flagged for an access" in report
    assert "2 run(s) had no mount, so the guard does not apply to them" in report
    by_condition = table_of(report, "### Mount isolation by condition")
    assert by_condition["Condition"][1:6] == ["Analysed", "Clean", "Flagged", "Unknown", "Not applicable"]
    assert by_condition["cond-a"][1:6] == ["2", "1", "1", "0", "0"]
    assert by_condition["cond-b"][1:6] == ["2", "0", "0", "0", "2"]
    assert set(table_of(report, "### Runs that are not clean")) == {"Run key", "k1"}  # k2 and k3 are not listed
    assert "Left out: 1 run(s)." in report
    assert table_of(report, "#### Pairs without the runs that are not clean")["a-vs-b"][1] == "39"


def test_a_batch_with_no_run_without_a_mount_has_no_column_and_no_sentence_for_them():
    """O60: the section of a batch whose every run had a mount is the text it was."""
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded())
    assert "Not applicable" not in report
    assert "had no mount" not in report


def test_with_every_run_applicable_clean_or_not_applicable_the_rerun_is_the_primary_analysis():
    guard = MountGuardAnalysis((run("k1"), no_mount("k2")), None)
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guard)
    assert "No run is left out (the runs with no mount stay in)" in report
    assert "Every run is clean," not in report


def test_a_batch_where_the_guard_applies_to_no_run_says_so():
    guard = MountGuardAnalysis((no_mount("k1"), no_mount("k2", "cond-b")), None)
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guard)
    assert "None: the guard applies to no analysed run" in report


def test_the_terminal_summary_counts_the_runs_with_no_mount_apart():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), None, mixed())
    assert lines[1].startswith("mount guard: 1 of 4 analysed run(s) not clean (1 flagged:")
    assert lines[1].endswith("; 2 not applicable (no mount)")
    assert lines[-1].startswith("mount sensitivity a-vs-b:")
    quiet = summary_lines(analysis(contrast("a-vs-b", "difference")), None, MountGuardAnalysis((no_mount("k1"),), None))
    assert quiet[-1] == "mount sensitivity: no run left out, the same as the analysis above"


def test_analysis_json_names_a_run_with_no_mount_not_applicable_with_its_data_path():
    summary = _mount_guard_summary(mixed())
    by_key = {entry["run_key"]: entry for entry in summary["runs"]}
    assert (by_key["k2"]["verdict"], by_key["k2"]["data_path"], by_key["k2"]["flags"]) == ("not_applicable", "rest", [])
    assert by_key["k3"]["data_path"] == "none"
    assert by_key["k4"]["data_path"] is None  # a run with a mount
    assert summary["counts"]["not_applicable"] == 2
    assert summary["sensitivity"]["left_out"] == ["k1"]


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


def test_the_terminal_summary_gives_the_runs_either_guard_leaves_out_per_condition():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), None, None, combined())
    assert "both guards, cond-a: 1 of 2 left out (1 by egress alone, 0 by the mount guard alone, 0 by both)" in lines
    assert "both guards, cond-b: 2 of 2 left out (0 by egress alone, 1 by the mount guard alone, 1 by both)" in lines


# --- the families of the reruns (S17) ---------------------------------------------------------------------------------

FAMILIES = {"first": ("gain-x", "gain-y"), "second": ("loss-x", "loss-y")}
IN_FAMILIES = "(2 Holm families, each adjusted on its own at alpha = 0.05)"


def two_families() -> AnalysisResult:
    """A rerun of four confirmatory contrasts in two named Holm families."""
    result = analysis(
        contrast("gain-x", "difference"),
        contrast("gain-y", "difference"),
        contrast("loss-x", "not_detected"),
        contrast("loss-y", "not_detected"),
    )
    return named_families(result, FAMILIES)


def test_s17_the_mount_guards_rerun_shows_each_named_family_and_analysis_json_carries_them():
    """S17: the rerun without the runs that are not clean shows each named Holm family in a table with its steps, as the
    primary analysis does and as the egress rerun does, and `analysis.json` holds the families and their steps."""
    guard = guarded(sensitivity=two_families())
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guard)
    assert f"{SENSITIVITY} {IN_FAMILIES}" in report
    assert set(table_of(report, "##### Holm family first (2 contrasts)")) - {"Contrast"} == {"gain-x", "gain-y"}
    assert table_of(report, "Holm steps of second:")["2"][1] == "loss-y"
    assert "Holm family of" not in report.split(SENSITIVITY)[1].split("\n## ")[0]
    kept = json.loads(_json_text(_mount_guard_summary(guard)))["sensitivity"]
    assert [f["name"] for f in kept["holm_families"]] == ["first", "second"]
    assert [s["name"] for s in kept["holm_families"][0]["steps"]] == ["gain-x", "gain-y"]


def test_s17_the_combined_rerun_shows_each_named_family_and_analysis_json_carries_them():
    """S17: the rerun without the runs either guard leaves out is shown and written as the others are."""
    both = combined(sensitivity=two_families())
    report = render(analysis(contrast("a-vs-b", "difference")), mount_guard=guarded(), guards=both)
    assert f"{BOTH_SENSITIVITY} {IN_FAMILIES}" in report
    assert set(table_of(report, "##### Holm family second (2 contrasts)")) - {"Contrast"} == {"loss-x", "loss-y"}
    kept = json.loads(_json_text(_guards_summary(both)))["sensitivity"]
    assert [f["name"] for f in kept["holm_families"]] == ["first", "second"]


def test_s17_a_flat_family_rerun_keeps_its_analysis_json_keys_in_both_guards():
    """S17: one flat family gives each guard's rerun the keys it had before families were named."""
    assert list(_mount_guard_summary(guarded())["sensitivity"]) == ["left_out", "contrasts", "holm"]
    assert list(_guards_summary(combined())["sensitivity"]) == ["left_out", "contrasts", "holm"]
