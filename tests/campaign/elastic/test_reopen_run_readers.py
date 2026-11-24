"""After a run is reopened: the controller's own planning treats it as pending and leases it normally, and every
reader of the ledger that walks its events (status, the views, the controller's view, a sweep, the merge's snapshot and
replay) tolerates the new `operator_reopen` kind, before the run is leased again and after it has ended."""

import pytest
from control_support import member, settings, site
from elastic_support import FakeClock, assert_invariants, finish, leased
from reopen_support import FED, Incident, incident, open_ledger, reopened

from expcore.campaign.elastic import Ledger, LedgerReader
from expcore.campaign.elastic.__main__ import format_status, main
from expcore.campaign.elastic.control import decide, ledger_view
from expcore.campaign.elastic.reopenrun import REOPEN, reopen_in_file
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot


def _lease_again(found: Incident, *, end: bool) -> int:
    """The reopened run's next lease, on a dispatcher's connection; with `end`, ended done (its charged attempt 1)."""
    ledger = open_ledger(found)
    try:
        lease = leased(ledger, FED)
        if end:
            assert finish(ledger, lease, state="done", charged=1) == "counted"
        return lease.lease_id
    finally:
        ledger.close()


def test_the_controllers_own_planning_sees_a_reopened_run_as_pending_and_leases_it_normally(tmp_path):
    """`demand`, `holds`, `ledger_view` and `decide` (what `control.py` reads) count the run among the pending ones, no
    hold keeps it back, and the next lease is its own, with its charged attempts as the attempt base."""
    found = incident(tmp_path)
    ledger = open_ledger(found)
    try:
        assert ledger.run(found.run_key).state == "harness_error"
        assert ledger.demand().pending == {"m1": 1}
        wanted_before = ledger_view(ledger).leases_wanted()
        reopen_in_file(found.path, found.lease_id, found.archive, "an invented reason", "op-1", clock=found.clock)
        run = ledger.run(found.run_key)
        assert (run.state, run.charged, run.lease_id) == ("pending", 1, found.lease_id)
        assert ledger.demand().pending == {"m1": 2}
        assert ledger.holds() == {}
        assert ledger_view(ledger).leases_wanted() == wanted_before + 1
        fleet = site(members=[member("fed-1")])
        assert decide(found.clock(), ledger_view(ledger), [(settings(), fleet)]).wanted == wanted_before + 1
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, run.charged)
        assert again.lease_id == found.lease_id + 1
        assert_invariants(ledger)
        found.clock.advance(60)
        assert finish(ledger, again, state="done", charged=1) == "counted"
        done = ledger.run(found.run_key)
        assert (done.state, done.charged) == ("done", 2)
        assert_invariants(ledger)
    finally:
        ledger.close()


@pytest.mark.parametrize("stage", ["reopened", "leased again", "ended again"])
def test_every_reader_of_the_event_log_tolerates_the_operator_reopen_kind(tmp_path, stage):
    found = reopened(tmp_path)
    if stage != "reopened":
        _lease_again(found, end=stage == "ended again")
    reader = LedgerReader(found.path)
    try:
        (event,) = [e for e in reader.events() if e["kind"] == REOPEN]
        assert (event["run_key"], event["lease_id"]) == (found.run_key, found.lease_id)
        assert event["detail"]["archive_sha256"] == found.sha256
        status = reader.status()
        assert isinstance(format_status(status), str)
        assert (status.pause_log, status.operator_pauses, status.grant_raises) == ((), {}, ())
        assert len(reader.runs()) == 2
        assert len(reader.leases()) >= 2
        assert reader.settlements() == []
        assert reader.holds() == {}
        view = ledger_view(reader)
        decision = decide(found.clock(), view, [(settings(), site(members=[member("fed-1")]))])
        assert decision.wanted == view.leases_wanted()
    finally:
        reader.close()
    assert main(["status", str(found.path)]) == 0


def test_a_sweep_and_an_issue_on_a_ledger_that_holds_the_event_work_as_before(tmp_path):
    found = reopened(tmp_path)
    ledger = Ledger.open(found.path, clock=FakeClock(found.clock() + 10))
    try:
        ledger.sweep()
        got = ledger.issue(FED)
        assert (got.run_key, got.attempt_base) == (found.run_key, 1)
        assert any(event["kind"] == REOPEN for event in ledger.events())
    finally:
        ledger.close()


def test_the_merges_snapshot_and_replay_accept_a_ledger_that_holds_the_event(tmp_path):
    """The replay reads the events in sequence and refuses a ledger whose tables are not what they give: the new kind
    changes no table the replay checks, so the reopened run (pending, its harness_error lease counted) and, later, the
    run it became (done on a later lease) both read."""
    found = reopened(tmp_path)
    snapshot = tmp_path / "ledger-snapshot.sqlite"
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "pending"
    assert facts.counted(found.run_key) is None  # a run that is not terminal has no counted lease
    assert facts.leases[found.lease_id].state == "counted"
    later = _lease_again(found, end=True)
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "done"
    assert facts.counted(found.run_key) == later
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted", "counted", "counted"]
