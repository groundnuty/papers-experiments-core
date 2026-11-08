"""The whole pipeline on the example study, through the command line: run, kill and resume, check, analyze, report.

The stub model plays a script per run, so the campaign meets a finished answer, a malformed tool call, a turn cap and,
under the condition that declares it as its treatment, an infrastructure failure. Everything is local: no network, no
key, and the platform is a stub whose health, probe and leases always succeed.

The campaigns are built once per module (the module-scoped fixtures) and each test asserts one thing about them.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from record_support import SECRET_NAME, clear_registry
from study_crash import KILLED

from expcore.cli import main
from expcore.provenance.codestamp import write_stamp
from expcore.records.record import load_record, record_filename
from expcore.stats.analysis import CONCLUSIONS
from expcore.study.config import load_study

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "tiny_study"
CRASH = ROOT / "tests" / "support" / "study_crash.py"
HEAD = "a" * 40  # the stamped commit: one value for every copy, so every campaign has the same code identity
TREATMENT, SUBSET = "relay-down", "extended"
CONTRASTS = {"extended-vs-baseline": False, "relay-vs-baseline": False, "tools-effect-on-rounds": True}  # exploratory?


@dataclass
class Campaign:
    """One copy of the example, and what the pipeline did to it."""

    root: Path
    codes: dict[str, int] = field(default_factory=dict)
    report: str = ""

    @property
    def runs(self) -> Path:
        return self.root / "out" / "runs"

    def sql(self, query: str, *args: Any) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.root / "out" / "state.sqlite")
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(query, args).fetchall()
        finally:
            connection.close()

    def read(self, name: str) -> Any:
        return json.loads((self.root / "out" / name).read_text(encoding="utf-8"))

    def planned(self) -> list[tuple[str, str, str, str]]:
        """The (run key, condition, task, model) of every planned run."""
        schema = load_study(self.root).schema
        found = []
        for row in self.sql("SELECT run_key, factors FROM runs"):
            factors = json.loads(row["factors"])
            found.append((row["run_key"], schema.condition_named(factors), factors["task"], factors["model"]))
        return found


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
def clean_environment():
    """No variable named like a secret: a short value would redact ordinary numbers of a record, as it should."""
    with pytest.MonkeyPatch.context() as patch:
        for name in [n for n in os.environ if SECRET_NAME.search(n) and not n.startswith("GIT_")]:
            patch.delenv(name)
        clear_registry()
        yield dict(os.environ)
        clear_registry()


def make_copy(base: Path, name: str) -> Path:
    """Copy the example to a directory of its own and stamp it, as a snapshot made without .git is."""
    copy = base / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    write_stamp(copy, HEAD, load_study(copy).scope)
    return copy


def cli(env: dict[str, str], *argv: str, process: bool = False) -> tuple[int, str]:
    """Run a command and return its exit status and stderr, in this process or as `python -m expcore`."""
    if process:
        done = subprocess.run([sys.executable, "-m", "expcore", *argv], capture_output=True, text=True, env=env)
        return done.returncode, done.stderr
    errors = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
        return main(list(argv)), errors.getvalue()


def finish(env: dict[str, str], campaign: Campaign, *, process: tuple[str, ...] = ()) -> None:
    """Check, analyze and report; remember each command's exit status and the report.

    The commands named in `process` run as `python -m expcore`, which costs a start of the interpreter and of the
    vendor libraries the core imports; the others run in this process.
    """
    steps = {"check": str(campaign.runs), "analyze": str(campaign.root), "report": str(campaign.root)}
    for name, target in steps.items():
        campaign.codes[name], _ = cli(env, name, target, process=name in process)
    campaign.report = (campaign.root / "out" / "report.md").read_text(encoding="utf-8")


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


def table(report: str, heading: str) -> dict[str, list[str]]:
    """The rows of the table under a heading of the report, by the text of their first cell."""
    section = report.split(heading, 1)[1].split("\n#", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")][1:]
    split = [[cell.strip() for cell in line.strip("|").split(" | ")] for line in rows]
    return {cells[0].strip(): cells for cells in split}


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
        {"condition": TREATMENT, "kind": "knowledge", "declared_treatment": True, "set_aside": 0, "treatment": 2}
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
    assert kinds[TREATMENT] == [TREATMENT, "knowledge", "yes", "0", "2"]
    conditions = table(resumed.report, "## Conditions")
    assert set(conditions) == {"baseline", SUBSET, TREATMENT}
    assert {name: cells[1] for name, cells in conditions.items()} == {"baseline": "8", SUBSET: "6", TREATMENT: "8"}
    assert all(cells[2] != "n/a" for cells in conditions.values())


def test_the_contrast_that_left_a_task_out_names_it(resumed):
    row = table(resumed.report, "## Preregistered contrasts")["extended-vs-baseline"]
    assert "dune" in row  # the subset condition did not run it


def test_the_report_is_identical_across_two_runs_with_the_same_seeds_and_across_processes(clean_pair):
    assert clean_pair.first.codes == clean_pair.second.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    assert clean_pair.first.report == clean_pair.second.report
    assert "tiny-study" in clean_pair.first.report
