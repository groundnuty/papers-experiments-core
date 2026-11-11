"""The command line: usage, and the refusals that exit non-zero with a message saying why (the full run: e2e)."""

import dataclasses
import shutil
from pathlib import Path

import pytest
from record_support import no_secrets_in_the_environment

from expcore.campaign.holds import Hold
from expcore.campaign.scheduler import RunSummary
from expcore.campaign.shard import ShardSpec
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
    assert "dispatched 0 attempts of 22 runs" in captured.out
    assert "hold on alpha-stub" in captured.out
    assert "over the ceiling" in captured.out
    assert "22 runs are still pending" in captured.err


def test_c23_run_counts_what_it_dispatched_in_attempts_of_the_campaigns_runs(tmp_path, monkeypatch, capsys):
    """C23: three runs were run again, so 65 attempts begun are 62 runs, none of them lost; the five workers that began
    no attempt (a lease that timed out, a gate that closed while one waited) are not counted."""
    summary = RunSummary(70, {}, {"done": 61, "infra_exhausted": 1}, attempts=65)
    monkeypatch.setattr("expcore.cli.run_study", lambda *args, **kwargs: summary)
    assert main(["run", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("dispatched 65 attempts of 62 runs; runs by status: {'done': 61, 'infra_exhausted': 1}\n")


@pytest.mark.parametrize("command", ["analyze", "report"])
def test_r04_a_refusal_names_the_command_that_refused_and_says_refused_once(study, command, capsys):
    """R04: records with no replay check are refused by the command that was run, named, and "refused" is said once."""
    (study / "out" / "runs").mkdir(parents=True)
    assert main([command, str(study)]) == 1
    err = capsys.readouterr().err.replace(str(study), "STUDY")
    assert err.startswith(f"{command} refused: STUDY/out/runs: ")
    assert err.count("refus") == 1
    assert "python -m expcore check" in err  # what to do


def test_c12_run_says_a_hold_on_stderr_as_it_begins_once_with_whether_it_ends_by_itself(study, capsys):
    """C12: the budget holds every run of the model; the command says so as it happens, not only in its summary."""
    assert main(["run", str(study)]) == EXIT_PENDING
    said = [line for line in capsys.readouterr().err.splitlines() if line.startswith("run: hold on alpha-stub")]
    assert len(said) == 1
    assert said[0].startswith("run: hold on alpha-stub (budget): ")
    assert said[0].endswith("; it does not end by itself: its runs stay pending until someone looks")


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


# --- one campaign on several hosts (#154) -----------------------------------------------------------------------


def captured_shard(monkeypatch, tmp_path, *argv):
    """Run `run` with a stand-in for `run_study`; return the exit status and the keyword `shard` it was given."""
    given = {}

    def stand_in(*args, **kwargs):
        given.update(kwargs)
        return RunSummary(0, {}, {"done": 1})

    monkeypatch.setattr("expcore.cli.run_study", stand_in)
    return main(["run", str(tmp_path), *argv]), given["shard"] if given else "not called"


def test_c28_run_takes_a_shard_and_an_optional_label_and_hands_the_spec_to_the_pipeline(tmp_path, monkeypatch):
    """C28: `--shard i/N` with the label defaulting to shard-i; without it the campaign is not a shard."""
    assert captured_shard(monkeypatch, tmp_path) == (0, None)
    assert captured_shard(monkeypatch, tmp_path, "--shard", "2/3") == (0, ShardSpec(2, 3, "shard-2"))
    assert captured_shard(monkeypatch, tmp_path, "--shard", "2/3", "--shard-label", "north-2") == (
        0,
        ShardSpec(2, 3, "north-2"),
    )


@pytest.mark.parametrize("argv", [["--shard", "two-of-three"], ["--shard", "2"], ["--shard-label", "north"]])
def test_c28_a_shard_that_is_not_i_over_n_or_a_label_without_a_shard_is_a_usage_error_that_repeats_nothing(
    argv, tmp_path, capsys
):
    """C28: what was typed is not repeated, as for every other usage error of the command."""
    assert main(["run", str(tmp_path), *argv]) == 2
    error = capsys.readouterr().err
    assert "usage:" in error
    assert not any(word in error for word in ("two-of-three", "north"))


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--shard", "3/2"], "shard index 3 is not between 1 and 2"),
        (["--shard", "1/1"], "a sharded campaign has at least 2 shards"),
        (["--shard", "0/2"], "shard index 0 is not between 1 and 2"),
        (["--shard", "1/2", "--shard-label", "North Rack"], "a shard label is 1 to 64 lower-case letters"),
    ],
)
def test_c28_a_spec_no_shard_can_have_is_refused_saying_why_and_never_quoting_the_label(
    argv, message, tmp_path, capsys
):
    """C28: exit 1 with the reason, as any refusal."""
    assert main(["run", str(tmp_path), *argv]) == 1
    error = capsys.readouterr().err
    assert f"run refused: {message}" in error
    assert "North Rack" not in error


def test_c30_a_total_below_the_number_of_shards_is_refused_by_the_command_naming_the_key(study, capsys):
    """C30: the example's unit caps are 1, which two shards cannot share."""
    assert main(["run", str(study), "--shard", "1/2"]) == 1
    assert "run refused: [scheduler.unit_caps] alpha-stub is 1, less than the 2 shards" in capsys.readouterr().err
    assert not (study / "out" / "state.sqlite").exists()
