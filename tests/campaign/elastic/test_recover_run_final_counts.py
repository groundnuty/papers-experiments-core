"""`recover-run` refuses a run one of whose earlier leases has a count that is not final (condition 2b), by the rule the
ledger holds a pending run back by (`attempts.holds`, C68): a settle or a return of such a lease would move the run's
stored count by that lease's own difference after the recovery, and the ledger would hold the recovered run until then.
A lease's count is final when it is counted, superseded, returned or cancelled; or, lost or expired with its holder
proven ended, when it was settled with its charged attempts, or its settle is overdue, or the ledger has no budget."""

import shutil

import pytest
from recover_support import (
    add_overdue,
    lost_and_settled,
    lost_by_losses,
    lost_run,
    settle_spend_only,
)
from recover_support import assert_refused as _refused
from recover_support import recover_found as _recover
from reopen_support import sql

from expcore.campaign.elastic import attempts, schema
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import RecoverRefused

BUDGET = {"max_usd": 10.0, "grants_usd": {"m1": 1.0}}


@pytest.mark.parametrize("state", ["lost", "expired"])
def test_a_lease_lost_with_no_settle_in_a_ledger_with_a_budget_refuses_the_recovery(tmp_path, state):
    """The last of three lost leases was never swept for its overdue (its run was exhausted, not pending), and nobody
    settled it. The earlier two were swept while their run was pending, so only it is named."""
    found = lost_by_losses(tmp_path)
    sql(found.path, "UPDATE leases SET state=? WHERE id=?", state, found.leases[-1])
    message = _refused(found, "2b", f"lease {found.leases[-1]} ({state})")
    assert "an earlier lease's count is not final" in message
    assert f"lease {found.leases[0]} " not in message


def test_an_expired_lease_nobody_settled_whose_holder_is_proven_ended_refuses_the_recovery(tmp_path):
    found = lost_run(tmp_path, **BUDGET)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    _refused(found, "2b", "lease 1 (expired)")


def test_a_settle_of_the_spend_alone_does_not_make_the_count_final(tmp_path):
    """A settlement with no charged attempts gives no count (a lease is settled once), so only an overdue ends it."""
    found = lost_by_losses(tmp_path)
    settle_spend_only(found, found.leases[-1])
    _refused(found, "2b", f"lease {found.leases[-1]} ")


def test_a_lost_lease_whose_settle_gave_its_charged_attempts_is_final(tmp_path):
    assert _recover(lost_and_settled(tmp_path)).event_seq is not None


def test_a_lost_lease_whose_settle_is_overdue_is_final(tmp_path):
    found = lost_by_losses(tmp_path)
    add_overdue(found.path, found.leases[-1], found.run_key)
    assert _recover(found).event_seq is not None


def test_a_ledger_without_a_budget_counts_a_loss_at_once(tmp_path):
    """Nothing is settled without a budget, so a lost lease, or an expired one whose holder is proven ended, is final
    at once."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost' WHERE id=1")
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=2")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    assert _recover(found).event_seq is not None


@pytest.mark.parametrize("states", [("counted", "counted", "counted"), ("superseded", "returned", "cancelled")])
def test_a_lease_that_is_counted_superseded_returned_or_cancelled_is_final_in_a_ledger_with_a_budget(tmp_path, states):
    found = lost_run(tmp_path, **BUDGET)
    for lease, state in zip(found.leases, states, strict=True):
        sql(found.path, "UPDATE leases SET state=? WHERE id=?", state, lease)
    assert _recover(found).event_seq is not None


def _unsettled(tmp_path):
    return lost_by_losses(tmp_path)


def _spend_only(tmp_path):
    found = lost_by_losses(tmp_path)
    settle_spend_only(found, found.leases[-1])
    return found


def _overdue(tmp_path):
    found = lost_by_losses(tmp_path)
    add_overdue(found.path, found.leases[-1], found.run_key)
    return found


def _without_a_budget(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost' WHERE id=1")
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=2")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    return found


@pytest.mark.parametrize(
    ("build", "held"),
    [
        (_unsettled, True),
        (_spend_only, True),
        (lost_and_settled, False),
        (_overdue, False),
        (_without_a_budget, False),
    ],
)
def test_the_recovery_refuses_what_the_ledger_would_hold_a_pending_run_back_for(tmp_path, build, held):
    """The rule is `attempts.holds`' on a run of any state: the same ledger with its run set to pending is held back by
    the ledger exactly when the recovery refuses it for the count of an earlier lease."""
    found = build(tmp_path)
    twin = found.root / "twin.sqlite"
    shutil.copy2(found.path, twin)
    sql(twin, "UPDATE runs SET state='pending' WHERE run_key=?", found.run_key)
    db = schema.connect(twin)
    try:
        config = LedgerConfig.from_json(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        ledger_holds = found.run_key in attempts.holds(db, config)
    finally:
        db.close()
    refusal = None
    try:
        _recover(found, dry_run=True)
    except RecoverRefused as error:
        refusal = str(error)
    assert refusal is None or refusal.startswith("condition 2b ("), refusal
    assert ledger_holds is (refusal is not None) is held
