"""The merge's replay (`ledger_read.read_ledger`, entered through `take_ledger_snapshot`) recomputes each run's latest
lease from the ledger's events and refuses a ledger whose run row says otherwise. A recovery clears the run's latest
lease, so the replay reads `operator_recover` as clearing it, checks the lease it names as the one it held, and
accepts the recovered ledger right after the recovery, after the run's next issue, and after a late completion of an
earlier lease that the ledger read as superseded; it still refuses a pointer cleared by hand."""

import json

import pytest
from elastic_support import finish, join, leased
from recover_support import FED, Lost, event_detail, lost_and_settled, lost_run, open_ledger
from recover_support import recover_found as _recover
from reopen_support import sql

from expcore.campaign.ledger_read import LedgerFacts, LedgerRefused, read_ledger, take_ledger_snapshot


def _facts(found: Lost) -> LedgerFacts:
    snapshot = found.root / "ledger-snapshot.sqlite"
    take_ledger_snapshot(found.path, snapshot)
    return read_ledger(snapshot.read_bytes(), "0" * 64)


def test_the_replay_accepts_a_recovered_ledger_before_the_run_is_issued_again(tmp_path):
    found = lost_run(tmp_path)
    before = _facts(found)
    assert before.runs[found.run_key].lease_id == found.leases[-1]
    _recover(found, charged=0)
    facts = _facts(found)
    run = facts.runs[found.run_key]
    assert (run.state, run.lease_id, run.charged) == ("pending", None, 0)
    assert facts.counted(found.run_key) is None
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted"] * 3


def test_the_replay_accepts_a_recovered_ledger_after_the_runs_next_issue(tmp_path):
    found = lost_run(tmp_path)
    _recover(found, charged=1)
    ledger = open_ledger(found)
    try:
        again = leased(ledger, FED)
    finally:
        ledger.close()
    facts = _facts(found)
    assert (facts.runs[found.run_key].state, facts.runs[found.run_key].lease_id) == ("leased", again.lease_id)


def test_the_replay_accepts_a_recovered_ledger_after_a_late_completion_the_ledger_read_as_superseded(tmp_path):
    found = lost_and_settled(tmp_path)
    _recover(found, charged=0)
    ledger = open_ledger(found)
    try:
        assert finish(ledger, ledger.lease(found.leases[-1]), state="done", charged=1) == "superseded"
    finally:
        ledger.close()
    facts = _facts(found)
    run = facts.runs[found.run_key]
    assert (run.state, run.lease_id, run.charged) == ("pending", None, 0)
    assert facts.leases[found.leases[-1]].state == "superseded"
    ledger = open_ledger(found)
    try:
        join(ledger, found.clock, "fed-next")
        again = leased(ledger, "fed-next")
    finally:
        ledger.close()
    assert _facts(found).runs[found.run_key].lease_id == again.lease_id


def test_the_replay_still_refuses_a_run_whose_latest_lease_was_cleared_with_no_recovery_event(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE runs SET lease_id=NULL WHERE run_key=?", found.run_key)
    with pytest.raises(LedgerRefused, match="the ledger's tables are not what its events give"):
        _facts(found)


def test_the_replay_refuses_a_recovered_run_whose_latest_lease_was_set_back_by_hand(tmp_path):
    found = lost_run(tmp_path)
    _recover(found)
    sql(found.path, "UPDATE runs SET lease_id=? WHERE run_key=?", found.leases[-1], found.run_key)
    with pytest.raises(LedgerRefused, match="its events give lease None"):
        _facts(found)


def test_the_replay_refuses_a_recovery_event_that_names_another_lease_as_the_one_the_run_held(tmp_path):
    found = lost_run(tmp_path)
    _recover(found)
    detail = event_detail(found.path)
    assert detail["prior_lease_id"] == found.leases[-1]
    sql(
        found.path,
        "UPDATE events SET detail=? WHERE kind='operator_recover'",
        json.dumps({**detail, "prior_lease_id": found.leases[0]}),
    )
    with pytest.raises(LedgerRefused, match=r"records prior lease 1 for run .*, where its events give 3"):
        _facts(found)
