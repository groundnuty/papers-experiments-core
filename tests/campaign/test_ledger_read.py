"""C45: the ledger as a merge by run reads it: a snapshot of the file, and the counting rule recomputed from its events.

These run on a ledger driven on a fake clock (`elastic_support`), with no store: what a run's counted lease is, a lease
that counted before its run was leased again, a ledger whose tables disagree with its events, and a live ledger whose
latest transactions are still in its -wal.
"""

import hashlib
import sqlite3

import pytest
from elastic_support import SITE, completion, finish, join, leased, lose, make, overdue

from expcore.campaign.elastic import SiteReading
from expcore.campaign.ledger_read import LedgerRefused, read_ledger, take_ledger_snapshot


def facts_of(tmp_path, ledger_file):
    """The facts a merge reads: a snapshot of the file, then its bytes."""
    snapshot = tmp_path / "snapshot.sqlite"
    take_ledger_snapshot(ledger_file, snapshot)
    data = snapshot.read_bytes()
    return read_ledger(data, hashlib.sha256(data).hexdigest())


def test_c45_a_lease_that_counted_before_its_run_was_leased_again_is_not_the_runs_counted_lease(tmp_path):
    """C45: an infrastructure failure below the attempt limit counts and puts the run back to pending; the lease that
    then ends it is the run's counted lease, and the earlier one stays counted, as evidence."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-a")
    first = leased(ledger, "fed-a")
    assert finish(ledger, first, state="infra_fail", charged=1) == "counted"
    second = leased(ledger, "fed-a")
    assert second.run_key == first.run_key
    assert finish(ledger, second) == "counted"
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert facts.counted(first.run_key) == second.lease_id
    assert facts.leases[first.lease_id].state == "counted"
    assert facts.runs[first.run_key].charged == 2


def test_c45_a_run_still_pending_has_no_counted_lease(tmp_path):
    """C45: a run that is not terminal in the ledger has none, whatever its leases did."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-a")
    lease = leased(ledger, "fed-a")
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert facts.counted(lease.run_key) is None


def test_c45_a_snapshot_of_a_live_ledger_holds_what_its_wal_holds(tmp_path):
    """C45: the ledger open on the orchestrator keeps its latest transactions in its -wal; the snapshot replays them."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-a")
    lease = leased(ledger, "fed-a")
    finish(ledger, lease)
    assert ledger.path.with_name(ledger.path.name + "-wal").exists()
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert facts.counted(lease.run_key) == lease.lease_id


def test_c45_a_ledger_whose_recorded_verdict_is_not_the_rules_is_refused(tmp_path):
    """C45: a completion recorded as counting when its lease was no longer its run's latest is refused."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-a")
    join(ledger, clock, "fed-b")
    lost = leased(ledger, "fed-a")
    ledger.mark_sent(lost.lease_id)
    clock.advance(400.0)
    ledger.observe_site(SITE, SiteReading())  # no heartbeat: it expires and its run goes back to pending
    ledger.prove_ended("fed-a", "cancelled")  # C68: the run is leased again once its holder is proven ended
    again = leased(ledger, "fed-b")
    assert again.run_key == lost.run_key
    assert finish(ledger, again) == "counted"
    ledger.observe_site(SITE, SiteReading(completions=(completion(lost),)))
    ledger.close()
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "UPDATE events SET detail=replace(detail, 'superseded', 'counts') WHERE kind='report' AND lease_id=?",
            (lost.lease_id,),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(LedgerRefused, match="where the rule gives 'superseded'"):
        facts_of(tmp_path, ledger.path)


def test_c45_a_lease_settled_twice_is_refused(tmp_path):
    """C45: a lease whose archive never verified is lost for good; a later event counting it is refused."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-a")
    lease = leased(ledger, "fed-a")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease),)))
    assert ledger.verify_archive(lease.lease_id, None) == "lost"
    ledger.close()
    db = sqlite3.connect(ledger.path)
    try:
        db.execute(
            "INSERT INTO events(at, kind, run_key, lease_id, detail) VALUES(?, 'counted', ?, ?, ?)",
            (clock(), lease.run_key, lease.lease_id, '{"outcome": "done"}'),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(LedgerRefused, match="settles a lease already settled as lost"):
        facts_of(tmp_path, ledger.path)


def test_c45_a_file_that_is_not_a_ledger_is_refused(tmp_path):
    """C45: anything but a ledger's file is refused, naming it."""
    other = tmp_path / "other.sqlite"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    db.commit()
    db.close()
    with pytest.raises(LedgerRefused, match="not a ledger"):
        facts_of(tmp_path, other)


def test_c68_a_ledger_whose_lost_lease_went_overdue_reads_and_its_run_is_on_the_later_lease(tmp_path):
    """C68: `settle_overdue` is a step of the log the read keeps and does not refuse: the run's latest lease is the
    one issued after the loss's settle went overdue, and the lost lease is still unsettled."""
    ledger, clock = make(tmp_path, runs=None, max_usd=10.0, grants_usd={"m1": 1.0})
    lost = lose(ledger, clock, "fed-a")
    overdue(ledger, clock)
    join(ledger, clock, "fed-b")
    again = leased(ledger, "fed-b")
    assert again.run_key == lost.run_key
    assert [event["lease_id"] for event in ledger.events() if event["kind"] == "settle_overdue"] == [lost.lease_id]
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert facts.runs[lost.run_key].lease_id == again.lease_id
    assert lost.lease_id not in facts.settlements
