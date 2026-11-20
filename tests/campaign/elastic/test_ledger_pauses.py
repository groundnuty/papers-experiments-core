"""C55: an operator pauses one model's issue and resumes it, in an append-only pair of ledger events. A paused model
gets no new lease while the others go on, a lease in flight settles as usual, `resume` lifts only the operator's pause
(C48's stays until `declare`), a refusal writes nothing, nothing already in the ledger changes, and the command works
while a dispatcher holds the ledger."""

import dataclasses
import hashlib
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from elastic_support import assert_invariants, completion, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, LedgerReader, OperatorPause, PauseRefused, SiteReading, Wait
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.controlinputs import ledger_view
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot

TWO = {"runs": plan(models=("m1", "m2"), tasks=2, conditions=("api-none",)), "declared": {"m1": ["fp-a"]}}
WHY = "provider 429 burst"


def _tables(ledger):
    """Every table of the file with its rows, as the file holds them."""
    db = ledger.db
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
    # sqlite_sequence is left out: SQLite moves the events table's autoincrement counter with every event appended
    # (the guard's tests check it)
    return {name: [tuple(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in sorted(names)}


def _seen_by(ledger, lease, *undeclared):
    found = dataclasses.replace(completion(lease, state="infra_fail", charged=1), undeclared=undeclared)
    ledger.observe_site("site-a", SiteReading(completions=(found,)))
    return ledger.verify_archive(lease.lease_id, found.archive_sha256)


def test_c55_a_paused_model_gets_no_new_lease_while_the_others_go_on_and_says_why(tmp_path):
    """C55: with m1 paused by an operator, three free federations take m2's two runs and the third waits, the wait
    naming the pause and its reason; m1's runs stay pending, and the controller still counts them (as for C48)."""
    ledger, clock = make(tmp_path, **TWO)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    paused = ledger.pause_model("m1", WHY, "operator-1")
    assert paused == OperatorPause(paused.event_seq, clock(), "m1", "pause", WHY, "operator-1")
    assert [leased(ledger, "fed-1").model, leased(ledger, "fed-2").model] == ["m2", "m2"]
    held = ledger.issue("fed-3")
    assert isinstance(held, Wait)
    assert f"model m1 is paused by an operator: {WHY}" in held.reason
    assert [r.state for r in ledger.runs() if r.model == "m1"] == ["pending", "pending"]
    assert ledger_view(ledger).demand.pending == {"m1": 2}
    assert_invariants(ledger)


def test_c55_a_lease_in_flight_settles_as_usual_and_resume_lets_the_model_issue_again(tmp_path):
    """C55: m1's lease is out when the pause comes: it is not cancelled or expired, it counts, and its run is done;
    the next m1 run waits through the pause and is leased the moment `resume` is recorded."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    join(ledger, clock, "fed-1")
    out = leased(ledger, "fed-1")
    ledger.pause_model("m1", WHY, "operator-1")
    assert ledger.lease(out.lease_id).state == "issued"
    assert finish(ledger, out) == "counted"
    assert ledger.run(out.run_key).state == "done"
    assert not any(e["kind"] in ("expire", "cancel", "lost") for e in ledger.events())
    waiting = ledger.issue("fed-1")
    assert isinstance(waiting, Wait)
    assert "model m1 is paused by an operator" in waiting.reason
    clock.advance(60)
    resumed = ledger.resume_model("m1", "the provider is back", "operator-2")
    assert (resumed.kind, resumed.model, resumed.actor, resumed.at) == ("resume", "m1", "operator-2", clock())
    assert leased(ledger, "fed-1").model == "m1"
    assert ledger.operator_pauses() == {}
    assert_invariants(ledger)


def test_c55_resume_lifts_only_the_operators_pause_and_a_model_also_paused_by_c48_stays_paused(tmp_path, capsys):
    """C55: m1 is paused by an undeclared fingerprint and by an operator; the wait and `status` name both; `resume`
    succeeds, says C48's pause holds, and m1 stays paused until `declare`; `resume` with only C48's pause is refused."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)), declared={"m1": ["fp-a"]})
    join(ledger, clock, "fed-1")
    assert _seen_by(ledger, leased(ledger, "fed-1"), "fp-new") == "counted"
    path = str(ledger_path(tmp_path))
    with pytest.raises(PauseRefused, match="declare"):
        ledger.resume_model("m1", "r", "op")  # only C48 holds it: nothing of the operator's to lift
    assert main(["pause", path, "--model", "m1", "--reason", WHY, "--actor", "op-1"]) == 0  # allowed beside C48's
    capsys.readouterr()
    both = ledger.issue("fed-1")
    assert isinstance(both, Wait)
    assert f"model m1 is paused by an operator: {WHY}" in both.reason
    assert "model m1 is paused: undeclared fingerprint(s) seen: fp-new" in both.reason
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "model m1: paused: undeclared fingerprint(s) seen: fp-new" in out
    assert "model m1: paused by an operator" in out
    assert main(["resume", path, "--model", "m1", "--reason", "over", "--actor", "op-1"]) == 0
    printed = capsys.readouterr().out
    assert "resumed model m1" in printed
    assert "model m1 stays paused: undeclared fingerprint(s) seen: fp-new" in printed
    still = ledger.issue("fed-1")
    assert isinstance(still, Wait)
    assert "undeclared fingerprint(s) seen: fp-new" in still.reason
    assert "paused by an operator" not in still.reason
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "model m1: paused: undeclared fingerprint(s) seen: fp-new" in out
    assert "model m1: paused by an operator" not in out
    declare = ["declare", path, "--model", "m1", "--fingerprint", "fp-new", "--reason", "r", "--actor", "op-1"]
    assert main(declare) == 0
    assert leased(ledger, "fed-1").model == "m1"
    assert_invariants(ledger)


def test_c55_a_refusal_writes_nothing(tmp_path, capsys):
    """C55: an unknown model, a model an operator already paused, and a resume of a model no operator paused each
    refuse (exit 1, a message naming the cause) and leave every table of the file as it was; a blank reason or actor
    is a usage error and writes nothing either."""
    ledger, _ = make(tmp_path, **TWO)
    path = str(ledger_path(tmp_path))
    ledger.pause_model("m1", WHY, "operator-1")
    before = _tables(ledger)
    with pytest.raises(PauseRefused, match="no runs of model m9"):
        ledger.pause_model("m9", WHY, "op")
    with pytest.raises(PauseRefused, match="no runs of model m9"):
        ledger.resume_model("m9", "r", "op")
    with pytest.raises(PauseRefused, match=r"already paused by an operator.*operator-1.*provider 429 burst"):
        ledger.pause_model("m1", "again", "op")
    with pytest.raises(PauseRefused, match="model m2 is not paused by an operator"):
        ledger.resume_model("m2", "r", "op")
    for args, message in (
        (["pause", path, "--model", "m9"], "no runs of model m9"),
        (["pause", path, "--model", "m1"], "already paused by an operator"),
        (["resume", path, "--model", "m2"], "is not paused by an operator"),
    ):
        assert main([*args, "--reason", "r", "--actor", "a"]) == 1
        assert message in capsys.readouterr().err
    for blank in ("", "  "):
        assert main(["pause", path, "--model", "m2", "--reason", blank, "--actor", "a"]) == 2
        assert main(["pause", path, "--model", "m2", "--reason", "r", "--actor", blank]) == 2
        capsys.readouterr()
    assert _tables(ledger) == before
    assert ledger.caps() == {}
    assert_invariants(ledger)


def test_c55_status_shows_each_operator_pause_with_reason_actor_and_time_and_nothing_without_one(tmp_path, capsys):
    """C55: `status` of a ledger no operator paused prints no pause line; after a pause it shows the model, since when,
    who and why, and the log; after the resume the line is gone and the log keeps both entries."""
    ledger, _ = make(tmp_path, **TWO)
    path = str(ledger_path(tmp_path))
    assert main(["status", path]) == 0
    assert "operator" not in capsys.readouterr().out
    started = time.time()  # the command stamps its events with the wall clock
    assert main(["pause", path, "--model", "m1", "--reason", WHY, "--actor", "agent-7"]) == 0
    capsys.readouterr()
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    (entry,) = ledger.operator_pauses().values()
    assert started <= entry.at <= time.time()
    when = datetime.fromtimestamp(entry.at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f"model m1: paused by an operator since {when} by agent-7: {WHY}" in out, out
    assert "model m2: paused by an operator" not in out
    assert f"{when} model m1 pause by agent-7: {WHY}" in out
    assert main(["resume", path, "--model", "m1", "--reason", "all clear", "--actor", "agent-8"]) == 0
    capsys.readouterr()
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "model m1: paused by an operator" not in out
    assert f"model m1 pause by agent-7: {WHY}" in out
    assert "model m1 resume by agent-8: all clear" in out
    reader = LedgerReader(ledger_path(tmp_path))
    try:
        assert [(p.kind, p.model) for p in reader.status().pause_log] == [("pause", "m1"), ("resume", "m1")]
        assert reader.status().operator_pauses == {}
    finally:
        reader.close()


def test_c55_the_cli_pauses_beside_a_dispatcher_and_leaves_everything_already_in_the_ledger_as_it_was(tmp_path, capsys):
    """C55: the dispatcher's lock is held, yet `pause` and `resume` write under the file's own lock; the pinned
    configuration, every meta row and every row of every other table stay byte for byte, the events only grow by the
    new pair after the earlier ones, and the dispatcher's next lease obeys each."""
    ledger, clock = make(tmp_path, **TWO)
    join(ledger, clock, "fed-1"), join(ledger, clock, "fed-2")
    leased(ledger, "fed-1")
    before = _tables(ledger)
    path = str(ledger_path(tmp_path))
    assert main(["pause", path, "--model", "m1", "--reason", WHY, "--actor", "op-1"]) == 0
    assert "paused model m1" in capsys.readouterr().out
    assert leased(ledger, "fed-2").model == "m2"
    middle = _tables(ledger)
    assert main(["resume", path, "--model", "m1", "--reason", "over", "--actor", "op-1"]) == 0
    assert "resumed model m1" in capsys.readouterr().out
    after = _tables(ledger)
    for name in before:
        if name not in ("events", "leases", "lease_sets", "runs"):  # the dispatcher's own lease is its own change
            assert after[name] == before[name], name
    assert after["meta"] == before["meta"] == middle["meta"]
    assert after["events"][: len(before["events"])] == before["events"]
    new = after["events"][len(before["events"]) :]
    assert [(row[2], row[3], row[4], row[5]) for row in new if row[2].startswith("operator")] == [
        ("operator_pause", None, None, None),
        ("operator_resume", None, None, None),
    ]
    assert [p.kind for p in ledger.pause_log()] == ["pause", "resume"]
    assert ledger.pause_log()[0].reason == WHY
    assert ledger.operator_pauses() == {}
    assert_invariants(ledger)


def test_c55_a_ledger_holding_pause_events_is_still_read_by_the_merge_by_run_and_the_controller(tmp_path):
    """C55: the readers that filter events by kind ignore the pause events: the merge's replay (which refuses a ledger
    whose tables disagree with its events) accepts the ledger, `ledger_view` reads it, and the file's schema is
    unchanged."""
    ledger, clock = make(tmp_path, **TWO)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.pause_model("m1", WHY, "op-1")
    assert finish(ledger, lease) == "counted"
    ledger.resume_model("m1", "over", "op-1")
    ledger.pause_model("m1", "again", "op-1")
    snapshot = tmp_path / "snapshot.sqlite"
    take_ledger_snapshot(ledger.path, snapshot)
    data = snapshot.read_bytes()
    facts = read_ledger(data, hashlib.sha256(data).hexdigest())
    assert facts.counted(lease.run_key) == lease.lease_id
    assert ledger_view(ledger).demand.pending == {"m1": 1, "m2": 2}
    raw = sqlite3.connect(ledger.path)
    try:
        assert raw.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == "expcore-ledger/1"
    finally:
        raw.close()


def test_c55_a_ledger_made_before_the_declaration_log_takes_a_pause_and_shows_it(tmp_path, capsys):
    """C55: the operator pause does not hang on the declaration tables: a file without them pauses and resumes a
    model, and `status` shows the pause (its declaration section stays the one line it was)."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)))
    ledger.close()
    raw = sqlite3.connect(ledger_path(tmp_path))
    for table in ("undeclared_seen", "lease_sets", "declarations"):
        raw.execute(f"DROP TABLE {table}")
    raw.commit()
    raw.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    join(ledger, clock, "fed-1")
    path = str(ledger_path(tmp_path))
    assert main(["pause", path, "--model", "m1", "--reason", WHY, "--actor", "op-1"]) == 0
    capsys.readouterr()
    held = ledger.issue("fed-1")
    assert isinstance(held, Wait)
    assert f"model m1 is paused by an operator: {WHY}" in held.reason
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    assert "declarations: none kept" in out
    assert "model m1: paused by an operator" in out
    assert main(["resume", path, "--model", "m1", "--reason", "over", "--actor", "op-1"]) == 0
    assert leased(ledger, "fed-1").model == "m1"
    ledger.close()
