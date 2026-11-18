"""`python -m expcore.campaign.elastic`: an operator's `status` and `raise-ceiling`, run while a dispatcher holds the
ledger."""

import sqlite3

import pytest
from elastic_support import assert_invariants, join, leased, ledger_path, lose, make, plan

from expcore.campaign.elastic import Wait
from expcore.campaign.elastic.__main__ import main

BUDGET = {"runs": plan(tasks=4, conditions=("api-none",)), "max_usd": 2.0, "grants_usd": {"m1": 1.0}}


def test_c42_the_cli_raises_the_ceiling_of_a_ledger_a_dispatcher_holds_open(tmp_path, capsys):
    """C42: the dispatcher's lock is held, yet `raise-ceiling` writes under the file's own lock, and the dispatcher's
    next lease sees the new ceiling."""
    ledger, clock = make(tmp_path, **BUDGET)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    leased(ledger, "fed-1"), leased(ledger, "fed-2")
    assert isinstance(ledger.issue("fed-3"), Wait)
    path = str(ledger_path(tmp_path))
    code = main(["raise-ceiling", path, "--usd", "3.5", "--reason", "a second wave", "--actor", "agent-7"])
    assert code == 0
    assert "3.500000" in capsys.readouterr().out
    (record,) = ledger.ceiling_history()
    assert (record.model, record.new_micro, record.reason, record.actor) == (
        None,
        3_500_000,
        "a second wave",
        "agent-7",
    )
    leased(ledger, "fed-3")
    assert_invariants(ledger)


def test_c42_the_cli_raises_a_models_ceiling_and_refuses_what_the_ledger_refuses(tmp_path, capsys):
    """C42: `--model` names the ceiling, and a lowering or a model without one exits 1 with the reason on stderr."""
    runs = plan(models=("m1", "m2"), tasks=2, conditions=("api-none",))
    ledger, _ = make(tmp_path, runs=runs, max_usd=10.0, per_model_usd={"m1": 4.0}, grants_usd={"m1": 1.0, "m2": 1.0})
    path = str(ledger_path(tmp_path))
    assert main(["raise-ceiling", path, "--model", "m1", "--usd", "6", "--reason", "r", "--actor", "a"]) == 0
    capsys.readouterr()
    for args, word in ((["--model", "m1", "--usd", "5"], "lower"), (["--model", "m2", "--usd", "9"], "no ceiling")):
        assert main(["raise-ceiling", path, *args, "--reason", "r", "--actor", "a"]) == 1
        assert word in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:  # no actor: a usage error, as argparse ends it
        main(["raise-ceiling", path, "--usd", "9", "--reason", "r"])
    assert usage.value.code == 2
    assert [(c.model, c.new_micro) for c in ledger.ceiling_history()] == [("m1", 6_000_000)]


def test_c42_status_prints_the_remaining_budget_the_locked_grants_and_the_ceilings_history(tmp_path, capsys):
    """C42: `status` reads the file without the dispatcher's lock and prints the ceiling, what is committed, left and
    locked in unsettled grants, and each raise with its reason and actor."""
    ledger, clock = make(tmp_path, **BUDGET)
    lost = lose(ledger, clock, "fed-1")
    path = str(ledger_path(tmp_path))
    assert main(["raise-ceiling", path, "--usd", "3", "--reason", "more headroom", "--actor", "agent-7"]) == 0
    capsys.readouterr()
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    for line in (
        "ceiling: 3.000000 USD",
        "committed: 1.000000 USD",
        "remaining: 2.000000 USD",
        "locked in unsettled leases: 1.000000 USD",
        "agent-7",
        "more headroom",
        "2.000000 -> 3.000000",
    ):
        assert line in out, out
    ledger.settle(lost.lease_id, 250_000, "root")
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "committed: 0.250000 USD" in out
    assert "locked in unsettled leases: 0.000000 USD" in out


def test_c42_the_cli_reports_a_missing_or_foreign_file(tmp_path, capsys):
    """C42: a path that holds no ledger is a usage error, not a traceback."""
    assert main(["status", str(tmp_path / "nothing.sqlite")]) == 2
    assert "error" in capsys.readouterr().err
    other = tmp_path / "other.sqlite"
    other.write_text("not a database")
    assert main(["status", str(other)]) == 2
    assert "error" in capsys.readouterr().err


def test_c42_the_cli_never_authorises_more_than_the_operator_typed(tmp_path, capsys):
    """C42: dollars are read exactly, not as a float: 1.00000099999999999 on a 1 USD ceiling rounds down to what it
    is and is refused as no raise; a figure past a 64-bit count of micro-dollars is a usage error, not a traceback."""
    ledger, _ = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)), max_usd=1.0, grants_usd={"m1": 1.0})
    path = str(ledger_path(tmp_path))
    argv = ["raise-ceiling", path, "--reason", "r", "--actor", "a", "--usd"]
    assert main([*argv, "1.00000099999999999"]) == 1
    assert "not above" in capsys.readouterr().err
    assert main([*argv, "10000000000000"]) == 2
    assert "new_max_micro_usd" in capsys.readouterr().err
    assert main([*argv, "nan"]) == 2
    assert main([*argv, "-3"]) == 2
    capsys.readouterr()
    assert main([*argv, "1.0000019"]) == 0
    assert ledger.ceilings().max_micro == 1_000_001


def test_c42_status_on_a_file_that_is_not_a_ledger_leaves_it_as_it_was(tmp_path, capsys):
    """C42: opening a foreign database to look at it must not switch its journal mode."""
    other = tmp_path / "other.sqlite"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE t(x)")
    db.commit()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    assert main(["status", str(other)]) == 2
    capsys.readouterr()
    db = sqlite3.connect(other)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
