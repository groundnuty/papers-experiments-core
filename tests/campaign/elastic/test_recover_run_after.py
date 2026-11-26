"""After a run is recovered: the ledger's own paths treat it as a pending run with the count the operator chose (it is
leased with that count as its attempt base, a sweep and a lost lease count from it, a lease ends it as they end any
run), and every reader of the ledger that walks its events (status, the views, the controller's view, the merge's
snapshot and replay) tolerates the new `operator_recover` kind, before the run is leased again and after it has ended.

A waiver leaves the run's count below the sum of what its leases were counted for, so `assert_invariants`, which holds
the two equal, is asked only of a recovery at the run's own count."""

import pytest
from control_support import member, settings, site
from elastic_support import FakeClock, assert_invariants, finish, join, leased, overdue
from recover_support import FED, Lost, event_detail, lost_by_losses, lost_run, open_ledger
from recover_support import recover_found as _recover

from expcore.campaign.elastic import Ledger, LedgerReader
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
    ledger = open_ledger(found)
    try:
        assert ledger.run(found.run_key).state == "infra_exhausted"
        assert ledger.demand().pending == {"m1": 1}  # the other run of the plan
        wanted_before = ledger_view(ledger).leases_wanted()
        _recover(found, charged=charged)
        run = ledger.run(found.run_key)
        assert (run.state, run.charged, run.lease_id) == ("pending", charged, found.leases[-1])
        assert ledger.demand().pending == {"m1": 2}
        assert ledger.holds() == {}
        assert ledger_view(ledger).leases_wanted() == wanted_before + 1
        fleet = site(members=[member("fed-1")])
        assert decide(found.clock(), ledger_view(ledger), [(settings(), fleet)]).wanted == wanted_before + 1
        again = leased(ledger, FED)
        assert (again.run_key, again.attempt_base) == (found.run_key, charged)
        assert again.lease_id == found.leases[-1] + 1
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
    assert facts.counted(found.run_key) is None  # a run that is not terminal has no counted lease
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted"] * 3
    later = _lease_again(found, end=True)
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "done"
    assert facts.counted(found.run_key) == later
    assert [lease.state for lease in facts.leases_of(found.run_key)] == ["counted"] * 4


def test_a_run_lost_by_its_leases_in_a_ledger_with_a_budget_is_held_until_their_settle_is_overdue(tmp_path):
    """A lost lease nobody settled holds its pending run (C68) until its settle is overdue. While the run was exhausted
    no overdue was logged for the last lease, so the recovered run waits `site_out_s` from the controller's open for
    it, and is then leased with the chosen count as its base."""
    found = lost_by_losses(tmp_path)
    _recover(found, charged=0)
    ledger = open_ledger(found)
    try:
        assert list(ledger.holds()) == [found.run_key]
        assert f"lease {found.leases[-1]} is lost and not settled" in ledger.holds()[found.run_key]
        join(ledger, found.clock, "fed-9")
        assert "is held" in ledger.issue("fed-9").reason
        overdue(ledger, found.clock)
        assert ledger.holds() == {}
        again = leased(ledger, "fed-9")
        assert (again.run_key, again.attempt_base) == (found.run_key, 0)
    finally:
        ledger.close()


def test_a_settle_after_a_waiver_moves_the_count_by_its_leases_own_difference(tmp_path):
    """The run's count is stored, and each lease adjusts it by the difference between what it was counted for and what
    its dead store's log gives (`attempts.set_charged`). A waiver sets the count below what the leases were counted for,
    so a settle that shows the last lease charged nothing takes one from the chosen count: 2 becomes 1."""
    found = lost_by_losses(tmp_path)
    _recover(found, charged=2)
    ledger = open_ledger(found)
    try:
        overdue(ledger, found.clock)
        ledger.settle(found.leases[-1], 100_000, "root", attempts_charged=0)
        assert ledger.run(found.run_key).charged == 1
    finally:
        ledger.close()
