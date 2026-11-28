"""`recover-run` refuses a run one of whose leases has a count that is not final (condition 2b): a first settle, or a
return of an expired lease, would move the run's stored count by that lease's own difference after the recovery. A
lease's count is final when it is counted, superseded, returned or cancelled; when it is lost and a settle gave its
charged attempts, or the ledger has no budget (nothing is settled, and a lost lease is not returned); or when it is
expired with its holder proven ended and a settle gave its charged attempts. A settle that is overdue (`settle_overdue`)
is not enough, though the ledger lets it release a pending run (`attempts.holds`, C68): a first settle can arrive after
it. The verb is deliberately stricter than the hold."""

import shutil

import pytest
from recover_support import (
    add_overdue,
    lost_and_settled,
    lost_by_losses,
    lost_run,
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
    """None of the three lost leases was settled with its charged attempts, so each is named."""
    found = lost_by_losses(tmp_path)
    sql(found.path, "UPDATE leases SET state=?", state)
    message = _refused(found, "2b", f"lease {found.leases[0]} ({state})")
    assert "an earlier lease's count is not final" in message
    assert f"lease {found.leases[-1]} ({state})" in message


def test_an_expired_lease_nobody_settled_whose_holder_is_proven_ended_refuses_the_recovery(tmp_path):
    found = lost_run(tmp_path, **BUDGET)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=1")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    _refused(found, "2b", "lease 1 (expired)")


def test_an_expired_lease_in_a_ledger_without_a_budget_is_not_final_either(tmp_path):
    """A return of an expired lease takes its attempt back, and without a budget nothing can be settled to prevent it:
    such a run is not recovered, though a lost lease of the same ledger is final."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost' WHERE id=1")
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=2")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    message = _refused(found, "2b", "lease 2 (expired)")
    assert "lease 1" not in message


def test_a_settle_of_the_spend_alone_does_not_make_the_count_final(tmp_path):
    """A settlement with no charged attempts gives no count, and a lease is settled once: the leases the earlier
    settles gave their attempts are final, and the last one, settled with its spend alone, is not."""
    found = lost_and_settled(tmp_path, spend_only_last=True)
    message = _refused(found, "2b", f"lease {found.leases[-1]} ")
    assert f"lease {found.leases[0]} " not in message


def test_an_overdue_settle_does_not_make_the_count_final(tmp_path):
    """A first settle can still arrive after the overdue, as `attempts.py` says, and would move the count."""
    found = lost_by_losses(tmp_path)
    for lease in found.leases:
        add_overdue(found.path, lease, found.run_key)  # the earlier two were swept while their run was pending
    _refused(found, "2b", f"lease {found.leases[-1]} ")


def test_a_lost_lease_whose_settle_gave_its_charged_attempts_is_final(tmp_path):
    assert _recover(lost_and_settled(tmp_path)).event_seq is not None


def test_a_lost_lease_in_a_ledger_without_a_budget_is_final_at_once(tmp_path):
    """Nothing is settled without a budget, and a lost lease is not returned (`Ledger._return`)."""
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost'")
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
    return lost_and_settled(tmp_path, spend_only_last=True)


def _overdue(tmp_path):
    found = lost_by_losses(tmp_path)
    for lease in found.leases:
        add_overdue(found.path, lease, found.run_key)
    return found


def _lost_without_a_budget(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='lost'")
    return found


def _expired_without_a_budget(tmp_path):
    found = lost_run(tmp_path)
    sql(found.path, "UPDATE leases SET state='expired' WHERE id=2")
    sql(found.path, "UPDATE federations SET ended_at=?, end_proof='terminal'", found.clock() + 60)
    return found


@pytest.mark.parametrize(
    ("build", "held", "refused"),
    [
        (_unsettled, True, True),
        (_spend_only, True, True),
        (lost_and_settled, False, False),
        (_lost_without_a_budget, False, False),
        (_overdue, False, True),  # the verb is stricter: a first settle can still arrive after an overdue
        (
            _expired_without_a_budget,
            False,
            True,
        ),  # stricter: nothing can be settled, and a return takes the attempt back
    ],
)
def test_the_recovery_refuses_everything_the_ledger_would_hold_a_pending_run_back_for_and_a_little_more(
    tmp_path, build, held, refused
):
    """The rule is `attempts.holds`' on a run of any state, made stricter in the two places listed: the same ledger with
    its run set to pending is held back by the ledger in a subset of the cases where the recovery refuses it for the
    count of a lease."""
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
    assert (ledger_holds, refusal is not None) == (held, refused)
    assert refusal is not None or not ledger_holds  # what the ledger holds back, the recovery refuses
