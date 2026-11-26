"""After a run is recovered: the ledger's own paths treat it as a pending run with the count the operator chose (it is
leased with that count as its attempt base, a sweep and a lost lease count from it, a lease ends it as they end any
run), and every reader of the ledger that walks its events (status, the views, the controller's view, the merge's
snapshot and replay) tolerates the new `operator_recover` kind, before the run is leased again and after it has ended.

A waiver leaves the run's count below the sum of what its leases were counted for, so `assert_invariants`, which holds
the two equal, is asked only of a recovery at the run's own count."""

import pytest
from control_support import member, settings, site
from elastic_support import FakeClock, assert_invariants, finish, leased
from recover_support import FED, Lost, event_detail, lost_and_settled, lost_run, open_ledger
from recover_support import recover_found as _recover

from expcore.campaign.elastic import Ledger, LedgerReader, SettleRefused
from expcore.campaign.elastic.__main__ import format_status, main
from expcore.campaign.elastic.control import decide, ledger_view
from expcore.campaign.elastic.recoverrun import RECOVER
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot


def _recovered(tmp_path, charged: int = 0) -> Lost:
    found = lost_run(tmp_path)
    _recover(found, charged=charged)
    return found


def _lease_again(found: Lost, *, end: bool) -> int:
    """The recovered run's next lease, on a dispatcher's connection; with `end`, ended done (its charged attempt 1)."""
    ledger = open_ledger(found)
    try:
        lease = leased(ledger, FED)
        if end:
            assert finish(ledger, lease, state="done", charged=1) == "counted"
        return lease.lease_id
    finally:
        ledger.close()


@pytest.mark.parametrize("charged", [0, 1, 2])
def test_the_controllers_own_planning_sees_a_recovered_run_as_pending_and_leases_it_with_the_chosen_base(
    tmp_path, charged
):
    """`demand`, `holds`, `ledger_view` and `decide` (what `control.py` reads) count the run among the pending ones, no
    hold keeps it back, and the next lease is its own, with the chosen count as the attempt base."""
    found = lost_run(tmp_path)
    reader = LedgerReader(found.path)  # before the recovery: the controller has not started
    try:
        assert {run.run_key: run.state for run in reader.runs()}[found.run_key] == "infra_exhausted"
        assert reader.demand().pending == {"m1": 1}  # the other run of the plan
        wanted_before = ledger_view(reader).leases_wanted()
    finally:
        reader.close()
    _recover(found, charged=charged)
    ledger = open_ledger(found)
    try:
        run = ledger.run(found.run_key)
        assert (run.state, run.charged, run.lease_id) == ("pending", charged, None)
        assert ledger.demand().pending == {"m1": 2}
        assert ledger.holds() == {}
        assert ledger_view(ledger).leases_wanted() == wanted_before + 1
        fleet = site(members=[member("fed-1")])
        assert decide(found.clock(), ledger_view(ledger), [(settings(), fleet)]).wanted == wanted_before + 1
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, charged)
        assert again.lease_id == found.leases[-1] + 1
        assert ledger.run(found.run_key).lease_id == again.lease_id  # the next issue sets the run's latest lease again
        found.clock.advance(60)
        assert finish(ledger, again, state="done", charged=1) == "counted"
        done = ledger.run(found.run_key)
        assert (done.state, done.charged) == ("done", charged + 1)
    finally:
        ledger.close()


def test_a_recovery_at_the_runs_own_count_keeps_every_invariant_of_the_ledger(tmp_path):
    found = lost_run(tmp_path, state="harness_error", charges=(1,))
    _recover(found, charged=1)
    ledger = open_ledger(found)
    try:
        assert_invariants(ledger)
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, 1)
        assert_invariants(ledger)
        found.clock.advance(60)
        assert finish(ledger, again, state="done", charged=1) == "counted"
        assert_invariants(ledger)
    finally:
        ledger.close()


def test_a_sweep_does_not_end_a_recovered_run_again_and_an_issue_after_it_leases_the_run(tmp_path):
    """The run has an attempt left (N is below `max_attempts`): a sweep, as on any pending run, leaves it pending."""
    found = _recovered(tmp_path, charged=2)
    ledger = Ledger.open(found.path, clock=FakeClock(found.clock() + 10))
    try:
        ledger.sweep()
        run = ledger.run(found.run_key)
        assert (run.state, run.charged) == ("pending", 2)
        got = leased(ledger, FED)
        assert (got.run_key, got.attempt_base) == (found.run_key, 2)
        assert any(event["kind"] == RECOVER for event in ledger.events())
    finally:
        ledger.close()


@pytest.mark.parametrize(("charged", "state"), [(0, "pending"), (1, "pending"), (2, "infra_exhausted")])
def test_a_lease_lost_after_the_recovery_counts_one_on_the_chosen_count(tmp_path, charged, state):
    """The lost lease's one conservative attempt is added to N, and a run that reaches `max_attempts` ends again."""
    found = _recovered(tmp_path, charged=charged)
    ledger = open_ledger(found)
    try:
        lease = leased(ledger, FED)
        assert lease.attempt_base == charged
        ledger.prove_ended(FED, "terminal")
        run = ledger.run(found.run_key)
        assert (run.state, run.charged) == (state, charged + 1)
    finally:
        ledger.close()


@pytest.mark.parametrize("stage", ["recovered", "leased again", "ended again"])
def test_every_reader_of_the_event_log_tolerates_the_operator_recover_kind(tmp_path, stage):
    found = _recovered(tmp_path)
    if stage != "recovered":
        _lease_again(found, end=stage == "ended again")
    reader = LedgerReader(found.path)
    try:
        (event,) = [e for e in reader.events() if e["kind"] == RECOVER]
        assert (event["run_key"], event["lease_id"]) == (found.run_key, None)
        assert event["detail"] == event_detail(found.path)
        status = reader.status()
        assert isinstance(format_status(status), str)
        assert (status.pause_log, status.operator_pauses, status.grant_raises) == ((), {}, ())
        assert len(reader.runs()) == 2
        assert len(reader.leases()) >= 3
        assert reader.settlements() == []
        assert reader.holds() == {}
        view = ledger_view(reader)
        decision = decide(found.clock(), view, [(settings(), site(members=[member("fed-1")]))])
        assert decision.wanted == view.leases_wanted()
    finally:
        reader.close()
    assert main(["status", str(found.path)]) == 0


def test_the_merges_snapshot_and_replay_accept_a_ledger_that_holds_the_event(tmp_path):
    """The replay reads the events in sequence and refuses a ledger whose tables are not what they give: the new kind
    changes no table the replay checks, so the recovered run (pending, its lease counted as it was) and, later, the run
    it became (done on a later lease) both read."""
    found = _recovered(tmp_path)
    snapshot = tmp_path / "ledger-snapshot.sqlite"
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "pending"
    assert facts.runs[found.run_key].lease_id is None
    assert facts.counted(found.run_key) is None  # a run that is not terminal has no counted lease
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted"] * 3
    later = _lease_again(found, end=True)
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "done"
    assert facts.counted(found.run_key) == later
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted"] * 4


def test_a_settle_read_again_after_a_waiver_changes_nothing_or_is_refused(tmp_path):
    """Every earlier lease's count is final (condition 2b), so the run's stored count is not moved by one of them
    afterwards: a settle with the values it was settled with changes nothing, and any other is refused. The count the
    waiver chose, 2 where the leases were counted for 3, stays."""
    found = lost_and_settled(tmp_path)
    _recover(found, charged=2)
    ledger = open_ledger(found)
    try:
        last = found.leases[-1]
        ledger.settle(last, 100_000, "root", attempts_charged=1)
        assert ledger.run(found.run_key).charged == 2
        with pytest.raises(SettleRefused):
            ledger.settle(last, 100_000, "root", attempts_charged=0)
        assert ledger.run(found.run_key).charged == 2
    finally:
        ledger.close()
