"""The whole pipeline on the example study, through the command line: run, kill and resume, check, analyze, report.

The stub model plays a script per run, so the campaign meets a finished answer, a malformed tool call, a turn cap and,
under the condition that declares it as its treatment, an infrastructure failure. Everything is local: no network, no
key, and the platform is a stub whose health, probe and leases always succeed.

The campaigns are built once per module (the module-scoped fixtures) and each test asserts one thing about them.
"""

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from study_crash import KILLED
from study_support import HEAD, ROOT, Campaign, clean_environment, cli, copy_campaign, finish, make_copy, table

from expcore.cli import main
from expcore.records.record import load_record, record_filename
from expcore.stats.analysis import CONCLUSIONS
from expcore.study.config import load_study
from expcore.study.pipeline import OUTPUT_FILES, regenerate

CRASH = ROOT / "tests" / "support" / "study_crash.py"
TREATMENT, SUBSET = "relay-down", "extended"
CONTRASTS = {"extended-vs-baseline": False, "relay-vs-baseline": False, "tools-effect-on-rounds": True}  # exploratory?

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


@dataclass
class Killed(Campaign):
    """A campaign killed in the middle, resumed, then checked, analyzed and reported."""

    before: dict[int, int] = field(default_factory=dict)  # attempt id -> dispatch_seq, as the dead process left them


@dataclass
class Pair:
    """Two campaigns that were never killed, with the same seeds; the second was reported by `python -m expcore`."""

    first: Campaign
    second: Campaign
    analyze_before_check: tuple[int, str] = (0, "")


@pytest.fixture(scope="module")
def resumed(tmp_path_factory, clean_environment) -> Killed:
    """Kill the dispatcher before the marker of its fifth attempt, resume with the command, then finish the pipeline."""
    campaign = Killed(make_copy(tmp_path_factory.mktemp("killed"), "study"))
    died = subprocess.run(
        [sys.executable, str(CRASH), str(campaign.root), "before_marker", "5"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert died.returncode == KILLED, died.stderr
    campaign.before = {r["id"]: r["dispatch_seq"] for r in campaign.sql("SELECT id, dispatch_seq FROM attempts")}
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    return campaign


@pytest.fixture(scope="module")
def clean_pair(tmp_path_factory, clean_environment) -> Pair:
    """Run the example twice from the start, with the first one's analyze asked for before its check."""
    base = tmp_path_factory.mktemp("clean")
    first, second = Campaign(make_copy(base, "first")), Campaign(make_copy(base, "second"))
    first.codes["run"], _ = cli(clean_environment, "run", str(first.root))
    pair = Pair(first, second, cli(clean_environment, "analyze", str(first.root)))
    finish(clean_environment, first)
    second.codes["run"], _ = cli(clean_environment, "run", str(second.root))
    finish(clean_environment, second, process=("report",))
    return pair


def record_of(campaign: Campaign, entry: dict[str, Any]):
    """The record of an analysed attempt of the export."""
    where = campaign.runs / entry["digest"] / Path(entry["run_dir"]).name
    return load_record(where / record_filename(entry["run_key"], entry["attempt"]))


# --- the campaign ----------------------------------------------------------------------------------------------------


def test_every_planned_run_ends_and_every_command_succeeds(resumed):
    export = resumed.read("export.json")
    assert resumed.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    assert len(resumed.planned()) == len(load_study(resumed.root).manifest.rows) == 22
    assert {r["status"] for r in resumed.sql("SELECT status FROM runs")} == {"done"}
    assert (export["planned_runs"], export["unfinished_runs"], export["exhausted_runs"]) == (22, [], [])
    assert len(export["analysed_attempts"]) == 22


def test_c04_the_attempt_the_kill_cut_short_was_settled_and_run_again(resumed):
    """C04: through the command line: the cut attempt is kept as interrupted, never charged, and run again."""
    states = [(r["state"], r["charged"]) for r in resumed.sql("SELECT state, charged FROM attempts ORDER BY id")]
    assert states.count(("interrupted", 0)) == 1
    assert states.count(("done", 0)) == 22
    assert len(states) == 23


def test_c01_the_treatment_failure_is_scored_and_never_run_again(resumed):
    """C01: under the condition that declares the kind as its treatment, the failure is a final scored attempt."""
    keys = [key for key, condition, task, _ in resumed.planned() if (condition, task) == (TREATMENT, "lake")]
    assert len(keys) == 2  # the task the stub fails, under both models
    for key in keys:
        rows = resumed.sql("SELECT state, charged, treatment, infra_kind FROM attempts WHERE run_key=?", key)
        assert [tuple(r) for r in rows] == [("done", 0, 1, "knowledge")]  # one attempt, ever
    assert resumed.read("export.json")["infrastructure_by_condition"] == [
        {
            "condition": TREATMENT,
            "kind": "knowledge",
            "declared_treatment": True,
            "set_aside": 0,
            "treatment": 2,
            "excluded": 0,
        }
    ]
    for key in keys:
        result = resumed.read("scores.json")[key]["tiny"]["result"]
        assert result["verdict"] == "not_determined"
        assert result["outcomes"] == {"accuracy": None, "lookups": None, "rounds": None}  # missing, never zero


def test_the_scripts_end_as_a_finished_answer_a_malformed_call_a_turn_cap_and_an_infrastructure_failure(resumed):
    records = [record_of(resumed, entry) for entry in resumed.read("export.json")["analysed_attempts"]]
    assert {r.outcome["name"] for r in records} == {"finished", "turn_cap", "infrastructure"}
    assert any(r.totals["malformed_calls"] == 1 and r.outcome["name"] == "finished" for r in records)


def test_the_subset_condition_only_ran_on_its_tasks(resumed):
    ran = {task for _, condition, task, _ in resumed.planned() if condition == SUBSET}
    assert ran == {"lake", "river", "hill"}
    everywhere = {task for _, condition, task, _ in resumed.planned() if condition == "baseline"}
    assert everywhere == {"lake", "river", "hill", "dune"}


# --- the replay check ------------------------------------------------------------------------------------------------


def test_the_replay_check_passes_over_every_record(resumed):
    check = json.loads((resumed.runs / "replay-check.json").read_text(encoding="utf-8"))
    on_disk = [p for p in resumed.runs.rglob("*.json") if p.name not in ("attempt-marker.json", "replay-check.json")]
    assert (check["failures"], check["n_ok"], check["n_records"]) == ([], 23, 23)  # 22 attempts and the cut one
    assert len(on_disk) == 23


def test_a_tampered_record_makes_analyze_and_report_refuse(resumed, clean_environment):
    path = next(resumed.runs.rglob("*.1.json"))
    original = path.read_bytes()
    data = json.loads(original)
    data["totals"]["rounds"] += 1
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        for command in ("analyze", "report"):
            code, errors = cli(clean_environment, command, str(resumed.root))
            assert code == 1
            assert "inputs changed since the replay check" in errors
            assert f"python -m expcore check {resumed.runs}" in errors  # the command that makes the check again
    finally:
        path.write_bytes(original)
    assert cli(clean_environment, "analyze", str(resumed.root))[0] == 0  # the refusal was the tampering's


def test_analyze_refuses_a_batch_that_was_never_checked(clean_pair):
    code, errors = clean_pair.analyze_before_check
    assert code == 1
    assert "never shown to replay" in errors


# --- dispatch order survives the kill ------------------------------------------------------------------------------


def test_each_analysed_attempt_says_where_it_stood_and_how_it_got_its_lease(resumed):
    entries = resumed.read("export.json")["analysed_attempts"]
    assert all(e["lease_outcome"] == "granted" and e["lease_wait_s"] >= 0 for e in entries)  # the stub platform leases
    assert all(e["started_at"].endswith("Z") for e in entries)
    places = [r["dispatch_seq"] for r in resumed.sql("SELECT dispatch_seq FROM attempts ORDER BY id")]
    assert places == sorted(places) == list(range(1, 24))  # monotone in the order the attempts began, none skipped


def test_c04_the_attempts_the_kill_left_keep_their_places_and_the_resumed_ones_come_after(resumed):
    """C04: the dead process's attempts are unchanged by the resume, and what the next process dispatches is later."""
    now = {r["id"]: r["dispatch_seq"] for r in resumed.sql("SELECT id, dispatch_seq FROM attempts")}
    assert len(resumed.before) == 5  # four settled attempts and the one the kill cut short
    assert {i: now[i] for i in resumed.before} == resumed.before
    assert min(now[i] for i in now if i not in resumed.before) > max(resumed.before.values())


def test_the_analyze_summary_gives_every_conclusion_with_its_holm_p_or_the_word_exploratory(resumed, capsys):
    assert main(["analyze", str(resumed.root)]) == 0
    lines = {line.split(":")[0]: line for line in capsys.readouterr().out.splitlines() if "estimate" in line}
    assert set(lines) == set(CONTRASTS)
    for name, exploratory in CONTRASTS.items():
        basis = ", Holm p exploratory, " if exploratory else ", Holm p "
        assert basis in lines[name]
        assert lines[name].endswith("(exploratory)") == exploratory
        assert lines[name].removesuffix(" (exploratory)").split(", ")[-1] in CONCLUSIONS


def test_analysis_json_carries_the_kernels_exploratory_flag_and_holm_decides_only_the_family(resumed):
    """The machine-readable table labels the contrast outside the family exploratory with the flag the report uses."""
    analysis = resumed.read("analysis.json")
    flags = {c["name"]: (c["exploratory"], c["in_family"]) for c in analysis["contrasts"]}
    assert flags == {name: (flag, not flag) for name, flag in CONTRASTS.items()}
    assert {d["name"] for d in analysis["holm"]} == {name for name, flag in CONTRASTS.items() if not flag}


# --- the report ----------------------------------------------------------------------------------------------------


def test_the_report_shows_each_contrast_with_an_allowed_conclusion_beside_its_significance(resumed):
    family = table(resumed.report, "## Preregistered contrasts")
    exploratory = table(resumed.report, "## Exploratory contrasts")
    assert set(family) == {n for n, flag in CONTRASTS.items() if not flag}
    assert set(exploratory) == {n for n, flag in CONTRASTS.items() if flag}
    for cells in family.values():
        assert cells[7] in CONCLUSIONS  # the conclusion column, beside the raw and the Holm p-values at 5 and 6
        assert cells[6] != "exploratory"
    for cells in exploratory.values():
        assert cells[7].split(" (")[0] in CONCLUSIONS
        assert cells[6] == "exploratory"
        assert cells[7].endswith("(exploratory)")


def test_the_report_names_the_preregistration_the_code_and_the_core(resumed):
    prereg = resumed.read("analysis.json")["preregistration"]["preregistration_sha256"]
    assert f"`{prereg}`" in resumed.report
    assert f"commit `{HEAD}`" in resumed.report
    assert "Core version:" in resumed.report
    assert "+override" not in resumed.report


def test_the_report_counts_the_treatment_failure_and_gives_each_condition_a_median_dispatch_position(resumed):
    kinds = table(resumed.report, "### Infrastructure failures")
    assert kinds[TREATMENT] == [TREATMENT, "knowledge", "yes", "0", "2", "0"]  # set aside, scored, left out
    conditions = table(resumed.report, "## Conditions")
    assert set(conditions) == {"baseline", SUBSET, TREATMENT}
    assert {name: cells[1] for name, cells in conditions.items()} == {"baseline": "8", SUBSET: "6", TREATMENT: "8"}
    assert {cells[2] for cells in conditions.values()} == {"0"}  # nothing is left out of a complete analysis
    assert all(cells[3] != "n/a" for cells in conditions.values())


def test_the_contrast_that_left_a_task_out_names_it(resumed):
    row = table(resumed.report, "## Preregistered contrasts")["extended-vs-baseline"]
    assert "dune" in row  # the subset condition did not run it


def test_the_report_is_identical_across_two_runs_with_the_same_seeds_and_across_processes(clean_pair):
    assert clean_pair.first.codes == clean_pair.second.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    assert clean_pair.first.report == clean_pair.second.report
    assert "tiny-study" in clean_pair.first.report


def test_every_output_regenerates_from_the_records_byte_for_byte_and_regenerating_writes_nothing(clean_pair, tmp_path):
    """What a replication package's check runs: the four outputs derived anew equal the files, and none is touched."""
    copy = copy_campaign(clean_pair.second, tmp_path, "copy").root
    out = copy / "out"
    before = {name: (out / name).stat().st_mtime_ns for name in OUTPUT_FILES}
    assert regenerate(copy) == []
    assert {name: (out / name).stat().st_mtime_ns for name in OUTPUT_FILES} == before
    (out / "scores.json").write_text("{}\n", encoding="utf-8")
    assert regenerate(copy) == ["scores.json: differs from what the records give"]


@pytest.mark.parametrize("platform_loaded", [False, True], ids=["core-only", "platform-loaded"])
def test_k07_the_outputs_regenerate_alike_whether_or_not_the_platform_is_loaded(clean_pair, tmp_path, platform_loaded):
    """K07: the outputs depend on the study, never on which modules a process imported: in a fresh process, with the
    platform layer's kinds registered or not, every output regenerates byte for byte."""
    copy = copy_campaign(clean_pair.second, tmp_path, "copy").root
    preload = (
        "import expcore_onedata.sandbox.kinds\n"
        "from expcore.errors import infra_kinds\n"
        "assert 'data_gate_integrity' in infra_kinds(), 'the platform registered no kind'\n"
        if platform_loaded
        else ""
    )
    code = preload + (
        "import sys\nfrom expcore.study.pipeline import regenerate\n"
        f"problems = regenerate({str(copy)!r})\nprint(problems)\nsys.exit(1 if problems else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
