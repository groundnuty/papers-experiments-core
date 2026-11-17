"""The `## Egress` section of a batch checked for egress (#179): its flagged runs and the sensitivity analysis without
them, and nothing new for a batch that was not checked."""

import dataclasses
import json

import pytest
from family_support import declared_equivalence, flat_equivalence, named_families

from expcore.egress.flags import RunEgress
from expcore.egress.sensitivity import EgressAnalysis
from expcore.report import render_report, summary_lines
from expcore.stats.analysis import AnalysisResult, ContrastResult, HolmDecision
from expcore.study.pipeline import _egress_summary, _json_text

CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")


def contrast(name: str, conclusion: str, **changes) -> ContrastResult:
    """A contrast result with plausible numbers, as tests/test_report.py builds one; `changes` replace fields."""
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
        "n_clusters_dropped": 0,
        "n_pairs": 40,
        "n_pairs_missing": 0,
        "n_unpaired": 0,
        "tasks_dropped": (),
        "treatment_outcomes": None,
        "max_unscored_treatment": 0,
        "pairs_missing": dict.fromkeys(CAUSES, 0),
        "counts": {},
        "per_model": (),
        "in_family": True,
        "exploratory": False,
        "significant": conclusion == "difference",
        "equivalence_margin": None,
        "equivalence_interval": None,
        "equivalent": False,
        "conclusion": conclusion,
        "minimum_detectable_effect": None,
    }
    return ContrastResult(**{**base, **changes})


def analysis(*results: ContrastResult, family=None) -> AnalysisResult:
    family = [r.name for r in results if r.in_family] if family is None else family
    holm = tuple(HolmDecision(n, 0.01, 0.02, 0.025, True) for n in family)
    return AnalysisResult(tuple(results), holm, 0.05, {"preregistration_sha256": "e" * 64})


def export() -> dict:
    """Four analysed runs of one model, two per condition."""
    runs = [("k1", "cond-a"), ("k4", "cond-a"), ("k2", "cond-b"), ("k3", "cond-b")]
    return {
        "planned_runs": 4,
        "exhausted_runs": [],
        "unfinished_runs": [],
        "analysed_attempts": [
            {"run_key": k, "condition": c, "dispatch_seq": i, "factors": {"model": "model-a"}}
            for i, (k, c) in enumerate(runs)
        ],
        "infrastructure_by_condition": [],
        "retries_by_kind": [],
        "budget_holds": [],
    }


def render(result, data=None, **options):
    return render_report(result, data or export(), study="a-study", code=CODE, core_version="1.2.3", **options)


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


SCAN_CLEAN = {"version": 1, "cells": 2, "hits": [], "hits_cut": False, "flagged": False}
HEADING = "## Egress (detected, not prevented)"
SENSITIVITY = "#### Preregistered contrasts without the flagged runs"


def verdict(run_key: str, condition: str = "cond-a", *, flags=(), log="intact", unknown=(), outside=(), scan=None):
    return RunEgress(run_key, 1, condition, log, tuple(unknown), tuple(outside), scan or SCAN_CLEAN, tuple(flags))


def checked(**changes) -> EgressAnalysis:
    """Four analysed runs: k1 with an outside connection, k2 with an unknown log, k3 with a scan hit, k4 not flagged;
    the sensitivity analysis keeps 38 of the primary's 40 pairs."""
    destination = {"address": "192.0.2.1", "port": 443, "type": "tcp", "attempts": 2}
    hit = {"cell": 1, "kind": "download", "what": "pip install", "flag": True}
    runs = (
        verdict("k1", flags=("outside",), outside=[destination]),
        verdict("k2", "cond-b", flags=("log_unknown",), log="unknown", unknown=("not_closed", "no_canary")),
        verdict("k3", "cond-b", flags=("static_scan",), scan={**SCAN_CLEAN, "flagged": True, "hits": [hit]}),
        verdict("k4"),
    )
    again = analysis(contrast("a-vs-b", "difference", n_pairs=38, estimate=0.21))
    return dataclasses.replace(EgressAnalysis(runs, again), **changes)


def test_a_report_without_an_egress_analysis_is_the_text_it_was():
    """O52: a batch not checked for egress (a Docker host) gets no section: the same bytes as a report rendered
    before egress detection existed."""
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result) == render(result, egress=None)
    assert "Egress" not in render(result)


def test_the_section_counts_the_flagged_runs_by_flag():
    report = render(analysis(contrast("a-vs-b", "difference")), egress=checked())
    assert HEADING in report
    assert "3 of 4 analysed run(s) flagged: 1 with an outside connection, 1 with an unknown log, 1 with a" in report
    by_condition = table_of(report, "### Egress by condition")
    assert by_condition["cond-a"][1:] == ["2", "1", "1", "0", "0"]
    assert by_condition["cond-b"][1:] == ["2", "2", "0", "1", "1"]


def test_each_flagged_run_is_listed_with_its_flags_log_destinations_and_hits():
    flagged = table_of(render(analysis(contrast("a-vs-b", "difference")), egress=checked()), "### Flagged runs")
    assert set(flagged) == {"Run key", "k1", "k2", "k3"}
    assert flagged["k1"][3:7] == ["outside", "intact", "-", "192.0.2.1:443/tcp x2"]
    assert flagged["k2"][3:6] == ["log_unknown", "unknown", "not_closed, no_canary"]
    assert flagged["k3"][7] == "download pip install (cell 1)"


def test_an_ipv6_destination_is_written_in_brackets():
    run = verdict(
        "k1", flags=("outside",), outside=[{"address": "2001:db8::1", "port": 443, "type": "tcp", "attempts": 1}]
    )
    report = render(analysis(contrast("a-vs-b", "difference")), egress=EgressAnalysis((run,), None))
    assert "[2001:db8::1]:443/tcp x1" in report


def test_the_sensitivity_analysis_is_shown_in_the_primary_analysiss_own_tables():
    report = render(analysis(contrast("a-vs-b", "difference")), egress=checked())
    assert "Left out: 3 flagged run(s)." in report
    rows = table_of(report, SENSITIVITY)
    assert rows["a-vs-b"][3] == "0.21"
    assert table_of(report, "#### Pairs without the flagged runs")["a-vs-b"][1] == "38"


def test_with_no_run_flagged_the_section_says_the_sensitivity_analysis_is_the_primary_one():
    report = render(analysis(contrast("a-vs-b", "difference")), egress=EgressAnalysis((verdict("k1"),), None))
    assert "### Flagged runs\n\nNone:" in report
    assert "No run is flagged, so the analysis without them is the analysis above." in report
    assert SENSITIVITY not in report


def test_a_refused_rerun_is_said_and_no_table_is_shown():
    report = render(analysis(contrast("a-vs-b", "difference")), egress=checked(sensitivity=None, refused="why"))
    assert "Not computed: the analysis refused the rerun (why)." in report
    assert SENSITIVITY not in report


def test_an_exploratory_only_sensitivity_analysis_shows_every_contrast_as_exploratory():
    loose = dataclasses.replace(
        contrast("a-vs-b", "not_detected", in_family=False, exploratory=True),
    )
    explore = dataclasses.replace(analysis(loose, family=[]), exploratory_only=True)
    report = render(explore, egress=checked(sensitivity=explore))
    assert "#### Every contrast without the flagged runs (exploratory" in report


def test_the_terminal_summary_says_the_flagged_runs_and_each_contrast_without_them():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), checked())
    assert "egress: 3 of 4 analysed run(s) flagged (1 outside, 1 unknown log, 1 static scan)" in lines
    assert lines[-1].startswith("sensitivity a-vs-b: estimate 0.21, p 0.0120, Holm p 0.0200, difference")


def test_the_terminal_summary_is_unchanged_without_an_egress_analysis():
    result = analysis(contrast("a-vs-b", "difference"))
    assert summary_lines(result) == summary_lines(result, None)
    assert not any(line.startswith("egress") for line in summary_lines(result))


def test_the_section_comes_after_the_conditions_and_before_the_left_out_attempts():
    report = render(analysis(contrast("a-vs-b", "difference")), egress=checked())
    assert report.index("## Conditions") < report.index(HEADING) < report.index("## Attempts left out")


def test_the_report_is_a_pure_function_of_its_inputs():
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result, export(), egress=checked()) == render(result, export(), egress=checked())
    assert CODE["commit"] in render(result, egress=checked())


# --- the families of the sensitivity analysis (S15, S17, S18) -------------------------------------------------------

AFTER = "### Sensitivity analysis"
FAMILIES = {"first": ("gain-x", "gain-y"), "second": ("loss-x", "loss-y")}
DECLARED = "#### Equivalence families (Holm over two one-sided tests, within each declared equivalence family)"


def four(*extra: ContrastResult) -> AnalysisResult:
    """Four confirmatory contrasts (two gains, two losses) and any `extra`, each with the Holm decision 0.01 / 0.02."""
    return analysis(
        contrast("gain-x", "difference"),
        contrast("gain-y", "difference"),
        contrast("loss-x", "not_detected"),
        contrast("loss-y", "not_detected"),
        *extra,
    )


def sensitivity_of(result: AnalysisResult) -> EgressAnalysis:
    """`checked()` with `result` as its sensitivity analysis."""
    return dataclasses.replace(checked(), sensitivity=result)


def sensitivity_section(result: AnalysisResult) -> str:
    """The report's sensitivity analysis part, for a batch whose rerun is `result` (its primary one flat family)."""
    return render(analysis(contrast("a-vs-b", "difference")), egress=sensitivity_of(result)).split(AFTER, 1)[1]


def test_s17_the_sensitivity_analysis_shows_each_named_family_in_a_table_with_its_steps():
    """S17: a rerun of several named Holm families is shown family by family, each member's Holm p within its own
    family and each family's steps, as the primary analysis shows them, and is never titled as one Holm family."""
    section = sensitivity_section(named_families(four(), FAMILIES))
    title = "#### Preregistered contrasts without the flagged runs (2 Holm families, each adjusted on its own"
    assert f"{title} at alpha = 0.05)" in section
    first = table_of(section, "##### Holm family first (2 contrasts)")
    second = table_of(section, "##### Holm family second (2 contrasts)")
    assert set(first) - {"Contrast"} == {"gain-x", "gain-y"}
    assert set(second) - {"Contrast"} == {"loss-x", "loss-y"}
    assert first["gain-x"][7:10] == ["0.0120", "0.0200", "difference"]
    assert table_of(section, "Holm steps of first:")["2"][1:] == [
        "gain-y",
        "0.0100",
        "0.025 (alpha/1)",
        "0.0200",
        "yes",
    ]
    assert table_of(section, "Holm steps of second:")["1"][1] == "loss-x"
    assert "Holm family of" not in section
    assert table_of(section, "#### Pairs without the flagged runs").keys() >= {"gain-x", "loss-y"}


def test_s17_a_flat_family_sensitivity_analysis_keeps_its_title_and_has_no_steps_or_equivalence_table():
    """S17: one flat Holm family's rerun is the text it was: one table titled with the family's size, and no family
    steps or equivalence table."""
    section = sensitivity_section(four())
    assert "#### Preregistered contrasts without the flagged runs (Holm family of 4)" in section
    for absent in ("Holm steps of", "##### Holm family", "Equivalence famil", "each adjusted on its own"):
        assert absent not in section


def test_s17_the_terminal_summary_names_each_sensitivity_members_family():
    """S17: a member's adjusted p of the rerun is its own family's, and the line says which family that is, as the
    primary analysis's line does."""
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), sensitivity_of(named_families(four(), FAMILIES)))
    rerun = {line.split(":")[0]: line for line in lines if line.startswith("sensitivity ")}
    assert rerun["sensitivity gain-x"] == (
        "sensitivity gain-x: estimate 0.25, p 0.0120, Holm p 0.0200 (family first), difference"
    )
    assert rerun["sensitivity loss-y"].endswith("Holm p 0.0200 (family second), not_detected")


def test_s17_a_rerun_whose_families_disagree_with_its_decisions_is_refused():
    """S17: the report refuses the rerun as it refuses the primary analysis (`check_families`): a family whose step is
    not its member's decision would show it adjusted in another family than its own."""
    result = named_families(four(), FAMILIES)
    family = result.holm_families[0]
    step = dataclasses.replace(family.steps[0], p_holm=0.5)
    broken = dataclasses.replace(result, holm_families=(dataclasses.replace(family, steps=(step, family.steps[1])),))
    with pytest.raises(ValueError, match="its step in Holm family 'first' is not its Holm decision"):
        sensitivity_section(broken)


def test_s18_the_sensitivity_analysis_shows_each_declared_equivalence_family_with_its_tost_steps():
    """S18: a rerun with a declared equivalence family shows each claim beside its equivalence family and, with named
    Holm families, its Holm family, then the family's TOST steps over its own claims, as the primary shows them."""
    result = declared_equivalence(
        named_families(four(), FAMILIES), "same", {"gain-x": 0.004, "loss-x": 0.02, "gain-y": 0.3}
    )
    section = sensitivity_section(result)
    claims = table_of(section, DECLARED)
    assert claims["Contrast"][:3] == ["Contrast", "Equivalence family", "Holm family"]
    assert claims["gain-x"][1:3] == ["same", "first"]
    assert claims["loss-x"][1:3] == ["same", "second"]
    steps = table_of(section, "TOST steps of equivalence family same:")
    assert [steps[n][1] for n in ("1", "2", "3")] == ["gain-x", "loss-x", "gain-y"]
    assert steps["1"][4:] == ["0.0120", "yes"]
    assert section.index("##### Holm family first") < section.index(DECLARED)


def test_s15_the_sensitivity_analysis_shows_the_flat_equivalence_family_with_its_tost_p_values():
    """S15: a rerun with one flat equivalence family shows each claim's TOST p, Holm TOST p and verdict in the table the
    primary analysis uses, under the sensitivity analysis's own heading level."""
    section = sensitivity_section(flat_equivalence(four(), {"gain-x": 0.002, "loss-x": 0.04}))
    claims = table_of(section, "#### Equivalence family (Holm over two one-sided tests)")
    assert claims["Contrast"] == [
        "Contrast", "Margin", "TOST p", "Holm TOST p", "Interval level", "Adjusted interval", "Equivalent"
    ]  # fmt: skip
    assert claims["gain-x"][2:4] == ["0.0020", "0.0040"]
    assert claims["loss-x"][2:4] == ["0.0400", "0.0400"]
    assert "Holm family |" not in section  # one flat Holm family: no family column


def test_s17_analysis_json_gives_the_sensitivity_analysis_its_named_families_and_their_steps():
    """S17: `analysis.json`'s egress sensitivity holds each named family's members and steps beside its Holm
    decisions, as the primary analysis does."""
    egress = sensitivity_of(named_families(four(), FAMILIES))
    sensitivity = json.loads(_json_text(_egress_summary(egress)))["sensitivity"]
    assert [f["name"] for f in sensitivity["holm_families"]] == ["first", "second"]
    assert sensitivity["holm_families"][0]["members"] == ["gain-x", "gain-y"]
    assert [s["name"] for s in sensitivity["holm_families"][1]["steps"]] == ["loss-x", "loss-y"]
    assert {d["name"] for d in sensitivity["holm"]} == {"gain-x", "gain-y", "loss-x", "loss-y"}
    assert "equivalence_families" not in sensitivity


def test_s18_analysis_json_gives_the_sensitivity_analysis_its_declared_equivalence_families():
    """S18: `analysis.json`'s egress sensitivity holds each declared equivalence family's members and steps."""
    result = declared_equivalence(four(), "same", {"gain-x": 0.004, "loss-x": 0.02, "gain-y": 0.3})
    sensitivity = json.loads(_json_text(_egress_summary(sensitivity_of(result))))["sensitivity"]
    (family,) = sensitivity["equivalence_families"]
    assert (family["name"], family["members"]) == ("same", ["gain-x", "loss-x", "gain-y"])
    assert [s["name"] for s in family["steps"]] == ["gain-x", "loss-x", "gain-y"]
    assert "holm_families" not in sensitivity


def test_s17_a_flat_family_sensitivity_analysis_keeps_its_analysis_json_keys():
    """S17: one flat family, and no equivalence family, gives the sensitivity analysis the keys it had before families
    were named: `left_out`, `contrasts`, `holm`."""
    assert list(_egress_summary(checked())["sensitivity"]) == ["left_out", "contrasts", "holm"]
