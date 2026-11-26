"""`python -m expcore.campaign.elastic recover-run`: what it prints, its exit status, and that a refusal or a dry run
changes nothing."""

import pytest
from recover_support import WHY, events_of, lost_run, open_ledger
from reopen_support import dump, sha256_of

from expcore.campaign.elastic import awareness
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.lock import DispatchLock


def _args(found, *, charged="0", source=None):
    return [
        "recover-run",
        str(found.path),
        "--run",
        found.run_key,
        "--charged",
        charged,
        "--source",
        str(source or found.source),
    ]


def test_the_command_recovers_prints_one_line_and_a_second_call_is_refused(tmp_path, capsys):
    found = lost_run(tmp_path)
    final = sha256_of(found.source)
    assert main([*_args(found), "--reason", WHY, "--actor", "op-1"]) == 0
    seq = events_of(found.path)[0]["seq"]
    out = capsys.readouterr()
    assert out.out == (
        f"recovered run {found.run_key}: infra_exhausted with 3 charged -> pending with 0 charged, rule waiver, "
        f"event #{seq}\n"
    )
    assert out.err == ""
    before = dump(found.path)
    assert main([*_args(found), "--reason", WHY, "--actor", "op-1"]) == 1
    assert "condition 1 (the run): run " in capsys.readouterr().err
    assert dump(found.path) == before
    assert sha256_of(found.source) == final


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(tmp_path, capsys):
    found = lost_run(tmp_path, state="harness_error", charges=(1,))
    before = dump(found.path)
    assert main([*_args(found, charged="1"), "--reason", WHY, "--actor", "op-1", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"would recover run {found.run_key}: harness_error with 1 charged -> pending with 1 charged, rule declared",
        "dry run: nothing written",
    ]
    assert dump(found.path) == before
    assert events_of(found.path) == []


def test_a_dry_run_refuses_what_a_real_run_refuses(tmp_path, capsys):
    found = lost_run(tmp_path)
    assert main([*_args(found, charged="3"), "--reason", WHY, "--actor", "op-1", "--dry-run"]) == 1
    assert "condition 3 (charged count)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("charged", "phrase"), [("-1", "is not a count from 0 to 2"), ("7", "is not a count from 0 to 2")]
)
def test_a_charged_count_out_of_range_is_refused_by_the_ledger_not_by_the_parser(tmp_path, capsys, charged, phrase):
    found = lost_run(tmp_path)
    before = dump(found.path)
    assert main([*_args(found, charged=charged), "--reason", WHY, "--actor", "op-1"]) == 1
    assert phrase in capsys.readouterr().err
    assert dump(found.path) == before


@pytest.mark.parametrize("charged", ["two", "1.5", ""])
def test_a_charged_count_that_is_not_a_whole_number_is_a_usage_error(tmp_path, charged):
    found = lost_run(tmp_path)
    with pytest.raises(SystemExit) as usage:
        main([*_args(found, charged=charged), "--reason", "r", "--actor", "a"])
    assert usage.value.code == 2


def test_a_sealed_final_that_is_not_there_is_refused_with_exit_one(tmp_path, capsys):
    found = lost_run(tmp_path)
    assert main([*_args(found, source=found.root / "none.sqlite"), "--reason", WHY, "--actor", "op-1"]) == 1
    assert "condition 4 (sealed final)" in capsys.readouterr().err


def test_the_command_is_refused_while_a_controller_that_honours_operator_writes_holds_the_ledger(tmp_path, capsys):
    found = lost_run(tmp_path)
    before = dump(found.path)
    ledger = open_ledger(found)
    try:
        assert main([*_args(found), "--reason", WHY, "--actor", "op-1"]) == 1
    finally:
        ledger.close()
    assert "condition 5 (controller): a controller holds this ledger and says it honours operator writes" in (
        capsys.readouterr().err
    )
    assert dump(found.path) == before


def test_the_command_is_refused_while_a_controller_holds_the_ledger(tmp_path, capsys):
    found = lost_run(tmp_path)
    before = dump(found.path)
    with DispatchLock(found.path.parent):  # a controller of an older core: the lock held, no declaration
        assert main([*_args(found), "--reason", WHY, "--actor", "op-1"]) == 1
    assert "before its controller starts" in capsys.readouterr().err
    assert dump(found.path) == before


def test_the_command_reports_a_ledger_that_is_not_there_and_a_reason_that_says_nothing_as_usage_errors(
    tmp_path, capsys
):
    found = lost_run(tmp_path)
    assert main([*_args(found), "--reason", "  ", "--actor", "op-1"]) == 2
    assert "reason" in capsys.readouterr().err
    args = _args(found)
    args[1] = str(tmp_path / "none.sqlite")
    assert main([*args, "--reason", WHY, "--actor", "op-1"]) == 2
    assert "no ledger there" in capsys.readouterr().err


def test_a_controller_that_cannot_be_told_refuses_the_recovery_with_exit_one(tmp_path, capsys, monkeypatch):
    """The locks could not be read: nothing says no controller holds the ledger, so nothing is written."""
    found = lost_run(tmp_path)
    before = dump(found.path)
    monkeypatch.setattr(awareness, "controller_of", lambda path: awareness.UNKNOWN)
    assert main([*_args(found), "--reason", WHY, "--actor", "op-1"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "condition 5 (controller): could not tell whether a controller holds this ledger" in out.err
    assert dump(found.path) == before
