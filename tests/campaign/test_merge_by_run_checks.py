"""C45: the checks of a merge by run that need no lease store run for real: a run the ledger may still reopen, and a
record of another lease where a counted one belongs.

The ledger is driven on a fake clock (`elastic_support`). A lost lease costs its run one charged attempt before its
real count is known, so a run can be `infra_exhausted` and reopen when the lease is settled with fewer: the merge
must not take such a run as final. Attempt numbers can repeat across a run's leases, so the record a merged batch holds
where a counted store's attempt belongs must name that store's lease.
"""

import hashlib
from pathlib import Path

import pytest
from elastic_support import SITE, join, leased, make, plan

from expcore.campaign.elastic import SiteReading
from expcore.campaign.lease_facts import LeaseStore
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot
from expcore.campaign.merge import MergeRefused
from expcore.campaign.merge_by_run import check_by_run, require_records
from expcore.campaign.run_lease import RunLease

RECORD = "d1/try1/r1.1.json"


def facts_of(tmp_path: Path, ledger_file: Path):
    snapshot = tmp_path / "snapshot.sqlite"
    take_ledger_snapshot(ledger_file, snapshot)
    data = snapshot.read_bytes()
    return read_ledger(data, hashlib.sha256(data).hexdigest())


def exhausted_by_a_loss(tmp_path: Path):
    """A ledger with a budget allowing one attempt, whose first lease expired with its holder proven ended: the loss
    charged the run its one attempt, so the run is infra_exhausted, with that lease unsettled."""
    ledger, clock = make(tmp_path, plan(tasks=1), max_attempts=1, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-a")
    lease = leased(ledger, "fed-a")
    ledger.mark_sent(lease.lease_id)
    clock.advance(400.0)
    ledger.observe_site(SITE, SiteReading())  # no heartbeat: it expires
    ledger.prove_ended("fed-a", "terminal")  # no completion: the loss counts one attempt
    assert ledger.run(lease.run_key).state == "infra_exhausted"
    return ledger, lease


def test_c45_a_run_exhausted_while_a_lease_of_it_is_unsettled_is_not_final(tmp_path):
    """C45: the merge refuses, naming the run and the lease that can still reopen it."""
    ledger, lease = exhausted_by_a_loss(tmp_path)
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    with pytest.raises(MergeRefused, match=f"run {lease.run_key} is infra_exhausted .* lease {lease.lease_id} is"):
        check_by_run(facts, [], None, None)


def test_c45_a_run_exhausted_once_its_lost_lease_is_settled_is_final(tmp_path):
    """C45: settled with the attempt it charged, the lease no longer holds the run open (the merge then asks for
    stores, as for any ledger)."""
    ledger, lease = exhausted_by_a_loss(tmp_path)
    ledger.settle(lease.lease_id, 0, "pulled", attempts_charged=1)
    assert ledger.run(lease.run_key).state == "infra_exhausted"
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert facts.unsettled(lease.run_key) == []
    with pytest.raises(MergeRefused, match="none was given"):
        check_by_run(facts, [], None, None)


def store_of(lease_id: int) -> LeaseStore:
    """A counted store holding one done attempt whose record lies at RECORD."""
    lease = RunLease("r1", lease_id, 0, 0, "0" * 64, "2099-01-01T00:00:00Z")
    here = Path(".")
    return LeaseStore(here, here, "", lease, "", {}, {}, {"r1": {}}, {"r1": "done"}, None, {RECORD: "done"})


def test_c45_a_record_of_another_lease_where_a_counted_one_belongs_is_refused():
    """C45: the record lies where the counted store's attempt does, and names a superseded lease: refused."""
    with pytest.raises(MergeRefused, match="a record of lease 3, where the store of lease 7"):
        require_records([store_of(7)], {RECORD: 3})


def test_c45_a_record_naming_its_own_lease_or_none_passes():
    """C45: the store's own lease, or none (the store's pinned spec then binds it)."""
    require_records([store_of(7)], {RECORD: 7})
    require_records([store_of(7)], {RECORD: None})
