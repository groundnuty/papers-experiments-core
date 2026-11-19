"""`python -m expcore.campaign.elastic`: an operator's `create`, `status`, `raise-ceiling` and `set-cap`; the last
three run while a dispatcher holds the ledger."""

import hashlib
import sqlite3

import pytest
from elastic_support import assert_invariants, join, leased, ledger_path, lose, make, plan
from shard_support import IDENTITY, declaring
from study_support import clean_environment, make_copy

from expcore.campaign.elastic import LedgerReader, Wait, ledger_runs
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.manifest import fingerprint_sha256
from expcore.study.config import load_study
from expcore.study.pipeline import campaign_fingerprint, campaign_identity
from expcore.study.selection_start import pricing_of

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

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


def test_c46_create_makes_a_campaigns_ledger_from_its_study_and_refuses_an_existing_one(
    tmp_path, capsys, clean_environment
):
    """C46: `create` writes the ledger of a study (its runs in the seeded order, the fingerprint a federation joins
    with, the study's attempt limit, one grant per model, the budget given) that `status` reads, and a second `create`
    refuses with the first untouched."""
    study = make_copy(tmp_path, "study")
    loaded = load_study(study)
    path = ledger_path(tmp_path)
    args = ["create", str(path), "--study", str(study), "--hard-term-s", "5400", "--max-usd", "7.5"]
    assert main(args) == 0
    assert "created" in capsys.readouterr().out
    reader = LedgerReader(path)
    try:
        config = reader.config
        assert reader.status().ceiling_micro == 7_500_000
        assert len(reader.runs()) == len(ledger_runs(loaded.manifest, loaded.schema))
    finally:
        reader.close()
    assert (config.hard_term_s, config.max_attempts) == (5400.0, loaded.campaign.scheduler.max_attempts)
    assert set(config.grants_usd) == set(pricing_of(loaded).models)
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        joined, _ = campaign_identity(study)
    assert config.fingerprint == fingerprint_sha256(joined) == fingerprint_sha256(campaign_fingerprint(study))
    assert main(["status", str(path)]) == 0
    assert "7.500000" in capsys.readouterr().out
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert main(args) != 0
    assert "already holds a ledger" in capsys.readouterr().err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_c42_create_writes_the_starting_caps_and_refuses_a_model_the_study_does_not_plan(
    tmp_path, capsys, clean_environment
):
    """C42: each `--cap MODEL=N` is the model's starting cap (pinned, and in force until a change); a model the study
    does not plan would cap nothing and is refused with no ledger made, and so are a cap below 1 and a model named
    twice."""
    study = make_copy(tmp_path, "study")
    models = sorted(pricing_of(load_study(study)).models)
    path = ledger_path(tmp_path)
    args = ["create", str(path), "--study", str(study), "--hard-term-s", "5400"]
    assert main([*args, "--cap", "nobody=2"]) == 1
    assert "does not plan" in capsys.readouterr().err
    assert not path.exists()
    for bad in (f"{models[0]}=0", "no-number"):
        with pytest.raises(SystemExit) as usage:  # a usage error, as argparse ends it
            main([*args, "--cap", bad])
        assert usage.value.code == 2
    assert main([*args, "--cap", f"{models[0]}=1", "--cap", f"{models[0]}=2"]) == 2
    assert "more than once" in capsys.readouterr().err
    assert not path.exists()
    chosen = {model: 2 + index for index, model in enumerate(models[:2])}
    assert main([*args, *(a for model, cap in chosen.items() for a in ("--cap", f"{model}={cap}"))]) == 0
    assert "cap" in capsys.readouterr().out
    reader = LedgerReader(path)
    try:
        assert (dict(reader.config.caps), reader.caps(), reader.status().caps) == (chosen, chosen, chosen)
    finally:
        reader.close()
