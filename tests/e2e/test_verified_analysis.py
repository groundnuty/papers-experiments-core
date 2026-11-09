"""The analysis uses only bytes the replay check verified (R04, R05, R08): the store is read from the check's snapshot,
the records from the check's replayed set, each once and against the check's hash, and the two must agree.

One finished campaign is built for the module; each test changes a copy of it and asks `analyze`, `report` or `check`
what they make of it.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from study_support import Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.campaign.export import read_export
from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.replay.check import inputs_sha256
from expcore.study import pipeline
from expcore.study.verified import UNCOVERED

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

FIRST_SCORED = "SELECT MIN(id) FROM attempts WHERE state = 'done' AND treatment = 0"
LAST = "(SELECT MAX(id) FROM attempts)"
RECORD_OF_FIRST = (
    "SELECT a.attempt, a.run_dir, a.run_key, r.digest FROM attempts a JOIN runs r USING(run_key) "
    f"WHERE a.id = ({FIRST_SCORED})"
)


@pytest.fixture(scope="module")
def finished(tmp_path_factory, clean_environment) -> Campaign:
    """The example, run to the end and checked, analyzed and reported."""
    campaign = Campaign(make_copy(tmp_path_factory.mktemp("verified"), "study"))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    return campaign


@pytest.fixture
def copy(finished, tmp_path) -> Campaign:
    return copy_campaign(finished, tmp_path, "copy")


def change_store(campaign: Campaign, statement: str) -> None:
    """Change the live campaign store, as a person with a shell could."""
    connection = sqlite3.connect(campaign.root / "out" / "state.sqlite")
    connection.execute(statement)
    connection.commit()
    connection.close()


def stored_check(campaign: Campaign) -> dict:
    return json.loads((campaign.runs / "replay-check.json").read_text(encoding="utf-8"))


def record_where(campaign: Campaign, attempt_id: str) -> Path:
    """The record of the attempt whose id the SQL expression gives."""
    row = campaign.sql(
        f"SELECT a.attempt, a.run_dir, r.digest FROM attempts a JOIN runs r USING(run_key) WHERE a.id = ({attempt_id})"
    )[0]
    return campaign.runs / row["digest"] / Path(row["run_dir"]).name / f"{row['digest']}.{row['attempt']}.json"


def some_record(campaign: Campaign) -> Path:
    """A record of a scored attempt: the first one the store lists as done."""
    return record_where(campaign, FIRST_SCORED)


def table_row(report: str, heading: str, first_cell: str) -> list[str]:
    """The cells of the row of a table under a heading of the report."""
    section = report.split(heading, 1)[1].split("\n#", 1)[0]
    line = next(line for line in section.splitlines() if line.startswith(f"| {first_cell} |"))
    return [cell.strip() for cell in line.strip("|").split(" | ")]


# --- the environment -------------------------------------------------------------------------------------------------


def another_environment(monkeypatch, distribution: str = "numpy", version: str = "0.0.1") -> dict:
    """Make the environment in use differ from the campaign's in one distribution's version; return it."""
    changed = pipeline.environment()
    changed["distributions"][distribution] = version
    monkeypatch.setattr(pipeline, "environment", lambda requires=(): json.loads(json.dumps(changed)))
    return changed


def test_r04_the_check_records_the_environment_it_ran_under_which_is_the_campaigns(finished):
    """R04: the check is bound to the environment as to the code: it records the one it ran under, and that is the
    environment the campaign's fingerprint holds in the snapshot a package ships."""
    check = stored_check(finished)
    assert check["environment"] == pipeline.environment()
    assert "papers-experiments-core" in check["environment"]["distributions"]
    snapshot = sqlite3.connect(finished.runs / SNAPSHOT_FILE)
    fingerprint = json.loads(snapshot.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()[0])
    snapshot.close()
    assert fingerprint["environment"] == check["environment"]


def test_r04_a_changed_environment_invalidates_the_check_and_analysis_refuses_until_it_is_run_again(
    copy, clean_environment, monkeypatch
):
    """R04: after a distribution changes version, analyze and report refuse, naming it; a check under the changed
    environment is refused too, since the campaign ran under the other one; back under the campaign's environment,
    the check runs again and the analysis goes through."""
    before = (copy.root / "out" / "report.md").read_bytes()
    with monkeypatch.context() as patch:
        another_environment(patch)
        for command in ("analyze", "report"):
            code, errors = cli(clean_environment, command, str(copy.root))
            assert code == 1, command
            assert "another environment than the one in use" in errors
            assert "numpy" in errors
            assert "0.0.1 in use" in errors
        code, errors = cli(clean_environment, "check", str(copy.runs))
        assert code == 1
        assert "the campaign ran under another environment" in errors
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0
    assert cli(clean_environment, "report", str(copy.root))[0] == 0
    assert (copy.root / "out" / "report.md").read_bytes() == before


def test_r04_a_check_that_records_another_environment_than_the_campaigns_is_refused(
    copy, clean_environment, monkeypatch
):
    """R04: a check whose recorded environment is the one in use, but not the one the campaign's snapshot holds, does
    not authorise the analysis."""
    check = stored_check(copy)
    check["environment"] = another_environment(monkeypatch)
    (copy.runs / "replay-check.json").write_text(json.dumps(check), encoding="utf-8")
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "the replay check ran under another environment than the campaign's" in errors


def test_c06_a_resume_under_another_environment_is_refused_and_runs_nothing(copy, clean_environment, monkeypatch):
    """C06: the study's environment is in the campaign fingerprint, so `run` under another one is refused."""
    attempts = copy.sql("SELECT COUNT(*) AS n FROM attempts")[0]["n"]
    another_environment(monkeypatch)
    code, errors = cli(clean_environment, "run", str(copy.root))
    assert code != 0
    assert "environment.distributions.numpy" in errors
    assert copy.sql("SELECT COUNT(*) AS n FROM attempts")[0]["n"] == attempts


# --- the snapshot ----------------------------------------------------------------------------------------------------


def test_r04_the_check_holds_a_snapshot_of_the_store_as_a_hashed_input_that_is_not_a_record(finished):
    check = stored_check(finished)
    assert check["snapshot"] == {"file": SNAPSHOT_FILE, "attempts_running": 0, "partial": False}
    assert SNAPSHOT_FILE in check["hashed_only"]
    assert SNAPSHOT_FILE in check["inputs"]
    assert SNAPSHOT_FILE not in check["replayed"]
    assert len(check["replayed"]) == check["n_records"] == 22


def test_a_complete_analysis_says_in_every_output_that_it_is_not_partial_and_leaves_nothing_out(finished):
    for name in ("export.json", "analysis.json"):
        saved = finished.read(name)
        assert (saved["partial"], saved["excluded_attempts"]) == (False, [])
    assert finished.read("export.json")["running_attempts"] == 0
    assert "- Partial analysis: no" in finished.report
    assert len(finished.read("export.json")["analysed_attempts"]) == 22


def test_r04_the_analysis_reads_the_snapshot_and_never_the_live_store(copy, clean_environment, finished):
    """R04: a change to the live store after the check does not reach the analysis, and the store may even be gone."""
    change_store(copy, f"UPDATE attempts SET treatment = 1, termination = 'turn_cap' WHERE id = ({FIRST_SCORED})")
    for leftover in (copy.root / "out").glob("state.sqlite*"):
        leftover.unlink()
    assert cli(clean_environment, "report", str(copy.root))[0] == 0
    assert (copy.root / "out" / "report.md").read_text(encoding="utf-8") == finished.report


@pytest.mark.parametrize(
    ("statement", "fragment"),
    [
        ("UPDATE attempts SET treatment = 1 - treatment WHERE id = ({first})", "treatment flag"),
        ("UPDATE attempts SET termination = 'time_cap' WHERE id = ({first})", "outcome"),
        ("UPDATE attempts SET attempt = attempt + 5 WHERE id = ({first})", "no attempt of the campaign snapshot"),
    ],
)
def test_r04_a_store_that_disagrees_with_its_records_is_refused_by_analyze_naming_the_difference(
    copy, clean_environment, statement, fragment
):
    """R04: the store is edited before the check, so the snapshot carries the edit; the records do not."""
    change_store(copy, statement.format(first=FIRST_SCORED))
    assert (
        cli(clean_environment, "check", str(copy.runs))[0] == 0
    )  # the records still replay; it is the pair that is wrong
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "refusing to analyse" in errors
    assert fragment in errors


def test_r04_a_snapshot_changed_after_the_check_is_refused(copy, clean_environment):
    change_store_file = sqlite3.connect(copy.runs / SNAPSHOT_FILE)
    change_store_file.execute(f"UPDATE attempts SET treatment = 1 WHERE id = ({FIRST_SCORED})")
    change_store_file.commit()
    change_store_file.close()
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert f"changed {SNAPSHOT_FILE}" in errors


def test_r04_a_check_made_by_the_replay_module_alone_has_no_snapshot_and_analysis_asks_for_the_command(
    copy, clean_environment
):
    check = stored_check(copy)
    del check["snapshot"]
    check["hashed_only"] = [p for p in check["hashed_only"] if p != SNAPSHOT_FILE]
    del check["inputs"][SNAPSHOT_FILE]
    (copy.runs / SNAPSHOT_FILE).unlink()
    check["inputs_sha256"] = inputs_sha256(check["inputs"])
    (copy.runs / "replay-check.json").write_text(json.dumps(check), encoding="utf-8")
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "holds no snapshot of the campaign" in errors
    assert "python -m expcore check" in errors


# --- the replayed set ------------------------------------------------------------------------------------------------


def hide_every_record_in_the_check(campaign: Campaign) -> dict:
    """Write the check a pattern `*` would have made: every file hashed, none replayed."""
    check = stored_check(campaign)
    check["hashed_only_patterns"] = ["*"]
    check["hashed_only"] = sorted(check["inputs"])
    check["replayed"] = []
    check["n_records"] = check["n_ok"] = 0
    (campaign.runs / "replay-check.json").write_text(json.dumps(check), encoding="utf-8")
    return check


def test_r04_a_check_that_replayed_nothing_does_not_authorise_the_analysis(copy, clean_environment):
    hide_every_record_in_the_check(copy)
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "is a run record, so it must be replayed" in errors


def test_r04_and_if_the_guard_were_passed_the_analysis_still_uses_no_record_the_check_did_not_replay(
    copy, clean_environment, monkeypatch
):
    check = hide_every_record_in_the_check(copy)
    monkeypatch.setattr(pipeline, "require_current_check", lambda batch, **options: check)
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "no replayed record covers it" in errors


# --- the exact bytes -------------------------------------------------------------------------------------------------


def test_r08_a_record_replaced_after_the_guard_is_refused_because_the_bytes_read_are_not_the_bytes_checked(
    copy, clean_environment, monkeypatch
):
    guard = pipeline.require_current_check
    target = some_record(copy)

    def guard_then_swap(batch, **options):
        check = guard(batch, **options)  # passes: the files are the checked ones at this moment
        data = json.loads(target.read_text(encoding="utf-8"))
        data["totals"]["rounds"] += 1
        target.write_text(json.dumps(data), encoding="utf-8")
        return check

    monkeypatch.setattr(pipeline, "require_current_check", guard_then_swap)
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "changed after the replay check read it" in errors


def test_r08_the_snapshot_replaced_after_the_guard_is_refused_too(copy, clean_environment, monkeypatch):
    guard = pipeline.require_current_check

    def guard_then_swap(batch, **options):
        check = guard(batch, **options)
        connection = sqlite3.connect(copy.runs / SNAPSHOT_FILE)
        connection.execute("UPDATE attempts SET treatment = 1")
        connection.commit()
        connection.close()
        return check

    monkeypatch.setattr(pipeline, "require_current_check", guard_then_swap)
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert f"{SNAPSHOT_FILE}: changed after the replay check read it" in errors


def test_r08_an_attempt_no_replayed_record_covers_is_refused_unless_partial_and_then_the_report_says_so(
    copy, clean_environment
):
    target = some_record(copy)
    key = copy.sql(f"SELECT run_key FROM attempts WHERE id = ({FIRST_SCORED})")[0]["run_key"]
    target.unlink()  # the record is gone before the check, so the check cannot have replayed it
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "no replayed record covers it; pass --partial" in errors
    assert cli(clean_environment, "report", str(copy.root), "--partial")[0] == 0
    report = (copy.root / "out" / "report.md").read_text(encoding="utf-8")
    assert "**Partial analysis**: yes. 0 attempt(s) were still running" in report
    assert "1 run(s) are left out of the analysis" in report
    assert "21 analysed" in report
    shown = key.replace("|", "\\|")  # a run key joins its factors with a bar, which a table cell escapes
    assert table_row(report, "## Attempts left out of the analysis", shown)[1:] == ["1", UNCOVERED]
    expected = [{"run_key": key, "attempt": 1, "reason": UNCOVERED}]
    for name in ("export.json", "analysis.json"):  # every saved output says it, so any one of them is enough
        saved = copy.read(name)
        assert saved["partial"] is True
        assert [{k: e[k] for k in ("run_key", "attempt", "reason")} for e in saved["excluded_attempts"]] == expected
    exported = copy.read("export.json")
    assert key not in [e["run_key"] for e in exported["analysed_attempts"]]
    assert len(exported["analysed_attempts"]) == 21
    assert read_export(copy.root / "out" / "export.json")["planned_runs"] == 22  # still accounts for every run


def test_r08_a_treatment_failure_left_out_is_counted_apart_and_not_as_scored(copy, clean_environment):
    """R08: the per-condition infrastructure table counts the analysed population; the left out have a column."""
    treatment = "SELECT MIN(id) FROM attempts WHERE treatment = 1"
    record_where(copy, treatment).unlink()
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0
    assert cli(clean_environment, "report", str(copy.root), "--partial")[0] == 0
    report = (copy.root / "out" / "report.md").read_text(encoding="utf-8")
    assert table_row(report, "### Infrastructure failures", "relay-down")[1:] == ["knowledge", "yes", "0", "1", "1"]
    (cell,) = [c for c in copy.read("export.json")["infrastructure_by_condition"] if c["kind"] == "knowledge"]
    assert (cell["treatment"], cell["excluded"]) == (1, 1)
    planned, analysed, exhausted, unfinished, left_out = table_row(report, "## Conditions", "relay-down")[1:6]
    assert (planned, analysed, exhausted, unfinished, left_out) == ("8", "7", "0", "0", "1")


def test_r08_a_record_that_belongs_to_no_attempt_of_the_snapshot_is_refused(copy, clean_environment):
    target = some_record(copy)
    stray = target.parent.parent / "try9" / target.name
    stray.parent.mkdir()
    shutil.copy(target, stray)
    assert cli(clean_environment, "check", str(copy.runs))[0] == 0  # it replays: it is a good record in the wrong place
    code, errors = cli(clean_environment, "analyze", str(copy.root))
    assert code == 1
    assert "try9" in errors
    assert "no attempt of the campaign snapshot has this record" in errors


# --- a campaign with attempts still running ---------------------------------------------------------------------------


@pytest.fixture
def running(copy, clean_environment) -> Campaign:
    """The copy, with its last attempt marked as still running, as a kill leaves it."""
    change_store(
        copy, f"UPDATE runs SET status = 'pending' WHERE run_key = (SELECT run_key FROM attempts WHERE id = {LAST})"
    )
    change_store(
        copy,
        f"UPDATE attempts SET state = 'running', ended_utc = NULL, termination = NULL, treatment = 0 WHERE id = {LAST}",
    )
    return copy


def test_r04_a_store_with_attempts_still_running_is_refused_by_check_and_leaves_the_earlier_snapshot_alone(
    running, clean_environment
):
    before = (running.runs / SNAPSHOT_FILE).read_bytes()
    code, errors = cli(clean_environment, "check", str(running.runs))
    assert code == 1
    assert "1 attempt(s) are still running" in errors
    assert (running.runs / SNAPSHOT_FILE).read_bytes() == before
    assert [p.name for p in running.runs.iterdir() if p.name.startswith(".")] == []


def test_r04_partial_check_records_that_attempts_were_running_and_analysis_then_needs_partial_too(
    running, clean_environment
):
    assert cli(clean_environment, "check", str(running.runs), "--partial")[0] == 0
    assert stored_check(running)["snapshot"] == {"file": SNAPSHOT_FILE, "attempts_running": 1, "partial": True}
    code, errors = cli(clean_environment, "analyze", str(running.root))
    assert code == 1
    assert "still running; analysing it needs --partial" in errors
    assert cli(clean_environment, "report", str(running.root), "--partial")[0] == 0
    report = (running.root / "out" / "report.md").read_text(encoding="utf-8")
    assert "**Partial analysis**: yes. 1 attempt(s) were still running when the replay check was made" in report
    assert "21 analysed" in report
    assert "1 unfinished" in report
    for name in ("export.json", "analysis.json"):
        saved = running.read(name)
        assert (saved["partial"], saved["running_attempts"], saved["excluded_attempts"]) == (True, 1, [])
