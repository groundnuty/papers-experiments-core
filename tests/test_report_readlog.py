"""S20: the `## Read log` section of the report of a batch whose records carry the `read_log` instrument: the runs
whose code touched the channel, and the sensitivity analysis without them; and nothing new for a batch without it."""

import dataclasses
import json

import pytest
from readlog_support import FORGER, preregistration, rows_of_pairs, runs_for

from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.readlog import sensitivity
from expcore.readlog.flags import RunReadLog
from expcore.readlog.scan import scan_cells
from expcore.readlog.sensitivity import ReadLogAnalysis, read_log_analysis
from expcore.report import render_report, summary_lines
from expcore.report_readlog import read_log_lines
from expcore.stats.analysis import AnalysisResult, ContrastRefused, ContrastResult, HolmDecision
from expcore.study.pipeline import _json_text, _read_log_summary
from expcore.visibility.sensitivity import VisibilityAnalysis


@pytest.fixture
def prereg():
    return preregistration()


CODE = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
CAUSES = ("treatment_outcome_none", "treatment_excluded", "exhausted", "unpaired_or_left_out", "outcome_none")
HEADING = "## Read log: runs that touched the channel (detected, not prevented)"
SENSITIVITY = "#### Preregistered contrasts without the runs that touched the read-log channel"


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


def run(key: str, condition: str, *cells: str) -> RunReadLog:
    return RunReadLog(key, 1, condition, scan_cells(list(cells)))


def touched(**changes) -> ReadLogAnalysis:
    """Four runs: k1 sent a datagram of its own, k2 read a process's environment, k3 and k4 ran ordinary code; the
    rerun keeps 37 of the primary's 40 pairs."""
    runs = (
        run("k1", "cond-a", FORGER),
        run("k4", "cond-a", "print(1)"),
        run("k2", "cond-b", "open('/proc/self/environ').read()"),
        run("k3", "cond-b", "print(2)"),
    )
    again = analysis(contrast("a-vs-b", "difference", n_pairs=37, estimate=0.19))
    return dataclasses.replace(ReadLogAnalysis(runs, again), **changes)


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


def test_s20_a_report_without_a_read_log_analysis_is_the_text_it_was():
    """S20: a batch whose records carry no `read_log` instrument gets no section: the same bytes as before."""
    result = analysis(contrast("a-vs-b", "difference"))
    assert render(result) == render(result, read_log=None)
    assert "Read log" not in render(result)
    assert summary_lines(result) == summary_lines(result, read_log=None)


def test_s20_the_report_section_counts_the_flagged_runs_by_condition_and_lists_each_with_its_hits():
    report = render(analysis(contrast("a-vs-b", "difference")), read_log=touched())
    assert HEADING in report
    assert "2 of 4 analysed run(s) flagged (1 channel_path, 1 unix_datagram, 1 proc_environ" in report
    by_condition = table_of(report, "### Read-log channel by condition")
    assert by_condition["cond-a"][1:] == ["2", "1"]
    assert by_condition["cond-b"][1:] == ["2", "1"]
    rows = table_of(report, "### Runs that touched the channel")
    assert set(rows) == {"Run key", "k1", "k2"}
    assert "channel_path /expcore-readlog/sock (cell 0)" in rows["k1"][4]
    assert "proc_environ /proc/*/environ (cell 0)" in rows["k2"][4]


def test_s20_the_sensitivity_analysis_is_shown_in_the_primary_analysis_s_own_tables():
    report = render(analysis(contrast("a-vs-b", "difference")), read_log=touched())
    assert "Left out: 2 flagged run(s)." in report
    assert table_of(report, SENSITIVITY)["a-vs-b"][3] == "0.19"


def test_s20_with_no_run_flagged_the_section_says_the_rerun_is_the_primary_analysis():
    clean = ReadLogAnalysis((run("k1", "cond-a", "print(1)"),), None)
    report = render(analysis(contrast("a-vs-b", "difference")), read_log=clean)
    assert "### Runs that touched the channel\n\nNone:" in report
    assert SENSITIVITY not in report


def test_s20_the_section_comes_after_the_visibility_section_and_before_the_guards_and_left_out_attempts():
    report = render(
        analysis(contrast("a-vs-b", "difference")),
        mount_guard=MountGuardAnalysis((), None),
        visibility=VisibilityAnalysis((), None),
        read_log=touched(),
    )
    assert report.index("## Process visibility") < report.index(HEADING) < report.index("## Attempts left out")


def test_s20_the_terminal_summary_says_the_flagged_runs_and_each_contrast_without_them():
    lines = summary_lines(analysis(contrast("a-vs-b", "difference")), read_log=touched())
    assert "read log: 2 of 4 analysed run(s) flagged (1 channel_path, 1 unix_datagram, 1 proc_environ)" in lines
    assert lines[-1].startswith("read-log sensitivity a-vs-b: estimate 0.19")


def test_s20_the_terminal_summary_says_a_refused_rerun_and_a_batch_with_nothing_flagged():
    result = analysis(contrast("a-vs-b", "difference"))
    refused = summary_lines(result, read_log=touched(sensitivity=None, refused="no pair is left"))
    assert refused[-1] == "read-log sensitivity: not computed (no pair is left)"
    clean = ReadLogAnalysis((run("k1", "cond-a", "print(1)"),), None)
    assert summary_lines(result, read_log=clean)[-2:] == [
        "read log: 0 of 1 analysed run(s) flagged",
        "read-log sensitivity: no run flagged, the same as the analysis above",
    ]


def test_s20_analysis_json_holds_every_run_s_hits_and_the_sensitivity_analysis():
    summary = json.loads(_json_text(_read_log_summary(touched())))
    assert summary["counts"]["flagged"] == 2
    k1 = next(r for r in summary["runs"] if r["run_key"] == "k1")
    assert k1["flagged"] is True
    assert [(h["kind"], h["what"], h["cell"]) for h in k1["hits"]] == [
        ("channel_path", "/expcore-readlog/sock", 0),
        ("unix_datagram", "AF_UNIX+SOCK_DGRAM", 0),
    ]
    assert summary["scan_version"] == 1
    assert summary["sensitivity"]["left_out"] == ["k1", "k2"]


def table(header, rows):
    return [" | ".join(header), *(" | ".join(str(c) for c in row) for row in rows)]


def section(done) -> str:
    return "\n".join(read_log_lines(done, table, lambda result: ["SENSITIVITY TABLES"]))


def test_the_section_says_it_is_a_heuristic_that_detects_and_does_not_prevent(prereg):
    rows = rows_of_pairs(4)
    text = section(read_log_analysis(runs_for(rows, lambda i: ["print(1)"]), rows, [], prereg))
    assert text.startswith("## Read log: runs that touched the channel")
    assert "heuristic" in text
    assert "not prevented" in text.split("\n")[0]


def test_a_flagged_run_has_a_line_naming_its_key_condition_and_hits_by_kind_and_cell(prereg):
    """S20: one row per run that touched the channel, with kind, fixed name and cell, never the cell's text."""
    rows = rows_of_pairs(4)
    done = read_log_analysis(runs_for(rows, lambda i: [FORGER] if i % 2 == 0 else ["print(1)"]), rows, [], prereg)
    text = section(done)
    flagged = [run for run in done.runs if run.flagged]
    assert flagged
    for run in flagged:
        line = next(
            row for row in text.split("\n") if row.startswith(f"{run.run_key} | {run.attempt} | {run.condition}")
        )
        assert "channel_path /expcore-readlog/sock (cell 0)" in line
        assert "unix_datagram AF_UNIX+SOCK_DGRAM (cell 0)" in line
    assert "s.sendto" not in text


def test_a_batch_with_no_flagged_run_says_so_and_has_no_rerun(prereg):
    rows = rows_of_pairs(4)
    text = section(read_log_analysis(runs_for(rows, lambda i: ["print(1)"]), rows, [], prereg))
    assert "None: no analysed run's code names the channel" in text
    assert "No run is flagged, so the analysis without them is the analysis above." in text
    assert "SENSITIVITY TABLES" not in text


def test_the_flagged_runs_sensitivity_analysis_is_shown_in_the_callers_tables(prereg):
    rows = rows_of_pairs(8)
    text = section(read_log_analysis(runs_for(rows, lambda i: [FORGER] if i == 0 else ["print(1)"]), rows, [], prereg))
    assert "Left out: 1 flagged run(s)." in text
    assert "SENSITIVITY TABLES" in text


def test_a_refused_rerun_is_reported_with_its_reason(prereg, monkeypatch):
    def refuse(*args, **kwargs):
        raise ContrastRefused("no pair is left")

    monkeypatch.setattr(sensitivity, "analyse", refuse)
    rows = rows_of_pairs(4)
    text = section(read_log_analysis(runs_for(rows, lambda i: [FORGER] if i == 0 else ["print(1)"]), rows, [], prereg))
    assert "Not computed: the analysis refused the rerun (no pair is left)." in text


def test_a_run_with_more_hits_than_the_cell_limit_shows_a_few_and_the_number_left(prereg):
    rows = rows_of_pairs(2)
    cell = "\n".join(f"open('{p}')" for p in ("/expcore-readlog", "/proc/1/environ", "/proc/net/unix", "x = 'readlog'"))
    cell += "\nimport expcore_readlog_client\nS_ISSOCK\nos.system('ss -xl')"
    text = section(read_log_analysis(runs_for(rows, lambda i: [cell]), rows, [], prereg))
    assert "and 1 more" in text
