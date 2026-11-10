"""The command line: usage, and the refusals that exit non-zero with a message saying why (the full run: e2e)."""

import dataclasses
import shutil
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment

from expcore.campaign.holds import Hold
from expcore.campaign.scheduler import RunSummary
from expcore.cli import EXIT_PENDING, main
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tiny_study"
pytestmark = pytest.mark.usefixtures("no_secrets_in_the_environment")
__all__ = ["no_secrets_in_the_environment"]


@pytest.fixture
def study(tmp_path) -> Path:
    """A stamped copy of the example whose budget holds every run, so a run command ends at once."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    text = (copy / "campaign.toml").read_text(encoding="utf-8")
    (copy / "campaign.toml").write_text(text.replace("max_usd = 5.0", "max_usd = 0.000001"), encoding="utf-8")
    write_stamp(copy, "a" * 40, load_study(copy).scope)
    return copy


@pytest.mark.parametrize("argv", [[], ["frobnicate"], ["run"], ["check"], ["run", "a", "b"]])
def test_a_command_line_that_is_not_a_command_exits_with_the_usage_status(argv, capsys):
    assert main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_help_exits_zero_and_names_the_four_commands(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ("run", "check", "analyze", "report"))


def test_a_directory_that_is_no_study_is_refused_naming_the_missing_file(tmp_path, capsys):
    assert main(["run", str(tmp_path)]) == 1
    assert "refused:" in capsys.readouterr().err
    assert main(["check", str(tmp_path)]) == 1
    assert "no study.toml" in capsys.readouterr().err


def test_code_that_cannot_be_vouched_for_is_refused_before_anything_runs(study, capsys):
    (study / ".code-stamp.json").unlink()
    assert main(["run", str(study)]) == 1
    assert "the code version is unknown" in capsys.readouterr().err
    assert not (study / "out").exists()


def test_analyze_and_report_refuse_a_study_that_was_never_run(study, capsys):
    for command in ("analyze", "report"):
        assert main([command, str(study)]) == 1
        assert "no campaign here; run the study first" in capsys.readouterr().err


def test_a_run_that_ends_with_runs_held_exits_with_the_pending_status_and_prints_the_hold(study, capsys):
    assert main(["run", str(study)]) == EXIT_PENDING
    captured = capsys.readouterr()
    assert "dispatched 0 runs" in captured.out
    assert "hold on alpha-stub" in captured.out
    assert "over the ceiling" in captured.out
    assert "22 runs are still pending" in captured.err


def test_a_resume_under_a_changed_configuration_is_refused_by_the_command(study, capsys):
    assert main(["run", str(study)]) == EXIT_PENDING
    capsys.readouterr()
    text = (study / "campaign.toml").read_text(encoding="utf-8")
    (study / "campaign.toml").write_text(text.replace("order_seed = 20260101", "order_seed = 5"), encoding="utf-8")
    write_stamp(study, "a" * 40, load_study(study).scope)  # the stamp follows the edited files; the campaign does not
    assert main(["run", str(study)]) == 1
    assert "refused:" in capsys.readouterr().err


def test_l08_a_run_stopped_on_harness_errors_prints_where_their_tracebacks_are(tmp_path, monkeypatch, capsys):
    """L08: the attempt keeps a class and a stack hash; the command says which local file holds the tracebacks."""
    log = tmp_path / "out" / "logs" / "harness-errors.log"
    stop = Hold("harness_errors", "dispatch of every model is stopped by harness errors")
    summary = RunSummary(3, {"all": stop}, {"harness_error": 3, "pending": 1}, log)
    monkeypatch.setattr("expcore.cli.run_study", lambda *args, **kwargs: summary)
    assert main(["run", str(tmp_path)]) == EXIT_PENDING
    assert f"their tracebacks, redacted, are in {log}" in capsys.readouterr().out
    monkeypatch.setattr("expcore.cli.run_study", lambda *args, **kwargs: dataclasses.replace(summary, holds={}))
    assert main(["run", str(tmp_path)]) == EXIT_PENDING  # held by something else: no word of the log
    assert "tracebacks" not in capsys.readouterr().out
