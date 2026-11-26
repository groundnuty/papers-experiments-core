"""After a recovery the run has no latest lease, so what an earlier lease's site says later cannot move it: a completion
of any earlier lease is read as superseded (the ledger counts a completion only for the run's latest lease), one of a
counted lease is the file read again or a conflict, and a return of an expired lease whose settle gave its attempts
takes nothing back. The run stays pending with the count the operator chose, and its next lease starts from it."""

import pytest
from elastic_support import SITE, finish, join, leased
from recover_support import lost_and_settled, lost_run, open_ledger
from recover_support import recover_found as _recover
from reopen_support import sql

from expcore.campaign.elastic import CompletionConflict, Return, SiteReading


def _run_of(ledger, found):
    run = ledger.run(found.run_key)
    return run.state, run.charged, run.lease_id


@pytest.mark.parametrize("charged", [0, 2])
@pytest.mark.parametrize("which", ["latest", "earlier"])
@pytest.mark.parametrize(("state", "attempts"), [("done", 1), ("harness_error", 0), ("infra_fail", 3)])
def test_a_late_completion_of_an_expired_lease_is_superseded_and_the_run_stays_as_recovered(
    tmp_path, charged, which, state, attempts
):
    """The leases of this run were lost, settled with their attempts, and its latest was lease 3. A completion of it,
    or of lease 1, read after the recovery, is not counted: the run is not put back to leased, not ended, and not
    recounted, and its next lease is issued with the chosen count."""
    found = lost_and_settled(tmp_path)
    _recover(found, charged=charged)
    ledger = open_ledger(found)
    try:
        old = ledger.lease(found.leases[-1] if which == "latest" else found.leases[0])
        assert finish(ledger, old, state=state, charged=attempts) == "superseded"
        assert _run_of(ledger, found) == ("pending", charged, None)
        join(ledger, found.clock, "fed-next")
        again = leased(ledger, "fed-next")
        assert (again.run_key, again.attempt_base) == (found.run_key, charged)
        assert _run_of(ledger, found) == ("leased", charged, again.lease_id)
    finally:
        ledger.close()


def test_a_completion_of_a_lost_lease_read_after_the_recovery_is_superseded_too(tmp_path):
    """A ledger without a budget counts a loss at once, and its lost lease is final: its completion read later is not
    counted either."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost', completion=NULL, verdict=NULL WHERE id=3")
    _recover(found, charged=1)
    ledger = open_ledger(found)
    try:
        assert finish(ledger, ledger.lease(3), state="done", charged=1) == "superseded"
        assert _run_of(ledger, found) == ("pending", 1, None)
    finally:
        ledger.close()


def test_a_completion_of_a_counted_lease_is_the_same_file_read_again_or_a_conflict(tmp_path):
    found = lost_run(tmp_path)
    _recover(found, charged=0)
    ledger = open_ledger(found)
    try:
        events = len(ledger.events())
        old = ledger.lease(found.leases[-1])
        assert finish(ledger, old, state="infra_fail", charged=1) == "counted"  # the completion it was counted by
        assert (_run_of(ledger, found), len(ledger.events())) == (("pending", 0, None), events)
        with pytest.raises(CompletionConflict):
            finish(ledger, old, state="done", charged=1)
        assert _run_of(ledger, found) == ("pending", 0, None)
    finally:
        ledger.close()


@pytest.mark.parametrize("charged", [0, 2])
def test_a_return_of_an_expired_lease_whose_settle_gave_its_attempts_takes_nothing_back(tmp_path, charged):
    found = lost_and_settled(tmp_path)
    _recover(found, charged=charged)
    ledger = open_ledger(found)
    try:
        ledger.observe_site(SITE, SiteReading(returns=(Return(lease_id=found.leases[-1], spend_usd=0.0),)))
        assert ledger.lease(found.leases[-1]).state == "returned"
        assert _run_of(ledger, found) == ("pending", charged, None)
    finally:
        ledger.close()


def test_a_lost_lease_cannot_be_returned_so_its_count_is_final_without_a_budget(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost'")
    _recover(found, charged=1)
    ledger = open_ledger(found)
    try:
        with pytest.raises(CompletionConflict, match="is lost, so it cannot be returned"):
            ledger.observe_site(SITE, SiteReading(returns=(Return(lease_id=3, spend_usd=0.0),)))
        assert _run_of(ledger, found) == ("pending", 1, None)
    finally:
        ledger.close()


def test_a_return_takes_back_the_attempt_of_an_expired_lease_nobody_settled_which_is_why_the_recovery_refuses_it(
    tmp_path,
):
    """What the ledger does without the verb: the run is exhausted on three charged attempts and its last lease is
    expired with its holder proven ended and no settle. A return of that lease takes its attempt back and reopens the
    run. The recovery would set the count and then see it lowered again below it, so it refuses such a run (2b)."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=3")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    ledger = open_ledger(found)
    try:
        assert _run_of(ledger, found)[:2] == ("infra_exhausted", 3)
        ledger.observe_site(SITE, SiteReading(returns=(Return(lease_id=3, spend_usd=0.0),)))
        assert _run_of(ledger, found)[:2] == ("pending", 2)
    finally:
        ledger.close()
