"""A model's cap changed mid-campaign: up gives the next leases at once, down revokes nothing, an operator's command
works while a dispatcher holds the ledger, and a ledger made before the table still opens."""

import sqlite3

import pytest
from elastic_support import assert_invariants, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import CapChange, CapRefused, Ledger, LedgerReader, Wait
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.controlinputs import ledger_view

TWO = {"runs": plan(models=("m1", "m2"), tasks=4, conditions=("api-none",)), "caps": {"m1": 1, "m2": 1}}


def _config_bytes(ledger):
    return ledger.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0]


def _has_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cap_changes'").fetchone() is not None


def test_c42_raising_a_models_cap_gives_its_next_lease_at_once_and_the_controller_sees_the_room(tmp_path):
    """C42: m1 and m2 hold one lease each at a cap of 1, so a third federation waits and the controller wants no
    lease; raising m1's cap to 2 is recorded and the third federation takes an m1 run at once, the controller now
    wants one more, and m2's pinned cap stands (a fourth federation still waits)."""
    ledger, clock = make(tmp_path, **TWO)
    for label in ("fed-1", "fed-2", "fed-3", "fed-4"):
        join(ledger, clock, label)
    assert [leased(ledger, "fed-1").model, leased(ledger, "fed-2").model] == ["m1", "m2"]
    held = ledger.issue("fed-3")
    assert isinstance(held, Wait)
    assert "model m1 is at its cap of 1" in held.reason
    assert ledger_view(ledger).leases_wanted() == 0
    clock.advance(30)
    change = ledger.set_cap("m1", 2, "more LLM capacity", "operator-1")
    assert change == CapChange(1, clock(), "m1", 1, 2, "more LLM capacity", "operator-1")
    assert ledger.caps() == {"m1": 2, "m2": 1}
    view = ledger_view(ledger)
    assert (view.caps, view.leases_wanted()) == ({"m1": 2, "m2": 1}, 1)
    assert leased(ledger, "fed-3").model == "m1"
    full = ledger.issue("fed-4")
    assert isinstance(full, Wait)
    assert "model m1 is at its cap of 2" in full.reason
    assert "model m2 is at its cap of 1" in full.reason
    assert ledger.cap_history() == [change]
    assert dict(ledger.config.caps) == {"m1": 1, "m2": 1}  # the pinned starting caps never change
    assert [e["detail"] for e in ledger.events() if e["kind"] == "set_cap"] == [
        {"cap_id": 1, "model": "m1", "new_cap": 2, "old_cap": 1}
    ]
    assert_invariants(ledger)


def test_c42_a_lowered_cap_revokes_no_lease_and_stops_new_ones_until_the_model_is_under_it(tmp_path):
    """C42: three leases are out at a cap of 3; lowering it to 1 expires and cancels none of them, a fourth federation
    waits while the model holds 3 and then 1 slot, and only when the last of the three ends does it get a lease."""
    ledger, clock = make(tmp_path, runs=plan(tasks=4, conditions=("api-none",)), caps={"m1": 3})
    for label in ("fed-1", "fed-2", "fed-3", "fed-4"):
        join(ledger, clock, label)
    first, second, third = (leased(ledger, label) for label in ("fed-1", "fed-2", "fed-3"))
    assert isinstance(ledger.issue("fed-4"), Wait)
    ledger.set_cap("m1", 1, "the provider is throttling", "operator-1")
    assert [ledger.lease(lease.lease_id).state for lease in (first, second, third)] == ["issued"] * 3
    assert not any(e["kind"] in ("expire", "cancel") for e in ledger.events())
    waits = [ledger.issue("fed-4")]
    for lease in (first, second):
        assert finish(ledger, lease) == "counted"
        waits.append(ledger.issue("fed-4"))  # one slot is still held, at the new cap
    assert ledger.slots() == {"m1": 1}
    assert all(isinstance(w, Wait) and "cap of 1" in w.reason for w in waits)
    assert finish(ledger, third) == "counted"
    leased(ledger, "fed-4")
    assert_invariants(ledger)


def test_c42_the_cli_sets_a_cap_while_a_dispatcher_holds_the_ledger_and_status_shows_it(tmp_path, capsys):
    """C42: the dispatcher's lock is held, yet `set-cap` writes under the file's own lock, leaves the pinned
    configuration as it was, the dispatcher's next lease sees the new cap, and `status` shows the caps in force and
    the change with its reason and actor."""
    ledger, clock = make(tmp_path, **TWO)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    leased(ledger, "fed-1"), leased(ledger, "fed-2")
    assert isinstance(ledger.issue("fed-3"), Wait)
    path = str(ledger_path(tmp_path))
    pinned = _config_bytes(ledger)
    args = ["set-cap", path, "--model", "m1", "--cap", "2", "--reason", "a second wave", "--actor", "agent-7"]
    assert main(args) == 0
    assert "1 -> 2" in capsys.readouterr().out
    (record,) = ledger.cap_history()
    assert (record.model, record.old, record.new, record.reason, record.actor) == (
        "m1",
        1,
        2,
        "a second wave",
        "agent-7",
    )
    leased(ledger, "fed-3")
    assert _config_bytes(ledger) == pinned
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    for line in ("model m1: cap 2", "model m2: cap 1", "model m1 1 -> 2", "agent-7", "a second wave"):
        assert line in out, out
    assert_invariants(ledger)


def test_c42_a_cap_on_a_model_the_ledger_has_no_runs_of_or_not_a_whole_number_from_1_is_refused(tmp_path, capsys):
    """C42: a typo'd model would silently cap nothing, so it is refused (exit 1), and so are 0, a fraction and a cap
    the model already has; a usage error leaves no trace in the ledger."""
    ledger, _ = make(tmp_path, **TWO)
    with pytest.raises(CapRefused, match="no runs of model m9"):
        ledger.set_cap("m9", 2, "why", "op")
    with pytest.raises(CapRefused, match="already 1"):
        ledger.set_cap("m1", 1, "why", "op")
    for value in (0, -1, 2.5, True, "3"):
        with pytest.raises(ValueError, match="whole number"):
            ledger.set_cap("m1", value, "why", "op")
    path = str(ledger_path(tmp_path))
    assert main(["set-cap", path, "--model", "m9", "--cap", "2", "--reason", "r", "--actor", "a"]) == 1
    assert "no runs of model m9" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        main(["set-cap", path, "--model", "m1", "--cap", "0", "--reason", "r", "--actor", "a"])
    assert usage.value.code == 2
    assert ledger.cap_history() == []
    assert not any(e["kind"] == "set_cap" for e in ledger.events())
    assert ledger.caps() == {"m1": 1, "m2": 1}


def test_c42_a_ledger_made_before_cap_changes_opens_keeps_its_pinned_caps_and_takes_its_first_change(tmp_path, capsys):
    """C42: a file without the table opens and leases as it did (its pinned cap of 1 holds, `status` reads it, and
    looking at it creates nothing); the first `set-cap`, while the dispatcher holds it, creates the table and the next
    lease sees the new cap."""
    ledger, clock = make(tmp_path, runs=plan(tasks=3, conditions=("api-none",)), caps={"m1": 1})
    ledger.close()
    raw = sqlite3.connect(ledger_path(tmp_path))
    raw.execute("DROP TABLE cap_changes")
    raw.commit()
    raw.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    path = str(ledger_path(tmp_path))
    join(ledger, clock, "fed-1"), join(ledger, clock, "fed-2")
    leased(ledger, "fed-1")
    held = ledger.issue("fed-2")
    assert isinstance(held, Wait)
    assert "cap of 1" in held.reason
    assert (ledger.caps(), ledger.cap_history()) == ({"m1": 1}, [])
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "model m1: cap 1" in out
    assert "cap history: no change" in out
    reader = LedgerReader(ledger_path(tmp_path))
    try:
        assert reader.status().caps == {"m1": 1}
    finally:
        reader.close()
    assert not _has_table(ledger.db)
    assert main(["set-cap", path, "--model", "m1", "--cap", "2", "--reason", "r", "--actor", "a"]) == 0
    leased(ledger, "fed-2")
    assert [(c.old, c.new) for c in ledger.cap_history()] == [(1, 2)]
    assert_invariants(ledger)
