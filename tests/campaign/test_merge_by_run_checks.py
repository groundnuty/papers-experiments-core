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

from expcore.campaign.elastic import Return, SiteReading
from expcore.campaign.lease_facts import LeaseStore
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot
from expcore.campaign.merge import MergeRefused
from expcore.campaign.merge_by_run import Evidence, check_by_run, require_records, spend_micro
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
    with pytest.raises(MergeRefused, match="a record naming lease 3, where the store of lease 7"):
        require_records([store_of(7)], {RECORD: 3})


def test_c45_a_record_naming_its_own_lease_passes():
    """C45: the store's own lease."""
    require_records([store_of(7)], {RECORD: 7})


def test_c45_a_record_naming_no_lease_is_refused():
    """C45: a record that names no lease (one written before records named theirs) could be another lease's at the
    same path: refused."""
    with pytest.raises(MergeRefused, match="a record naming no lease"):
        require_records([store_of(7)], {RECORD: None})


def test_c45_a_settlement_of_the_spend_alone_leaves_the_exhausted_run_open(tmp_path):
    """C45: settled with no charged attempts, the loss's provisional attempt can still be taken back (a return showing
    the lease unstarted), so the run is still not final."""
    ledger, lease = exhausted_by_a_loss(tmp_path)
    ledger.settle(lease.lease_id, 0, "pulled")
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    with pytest.raises(MergeRefused, match=f"run {lease.run_key} is infra_exhausted"):
        check_by_run(facts, [], None, None)


def test_c45_a_ledger_that_recorded_a_settlement_below_the_spend_is_refused(tmp_path):
    """C45: a return reporting more than the lease was settled at is recorded as a contradiction and the lower charge
    kept: the merge refuses to count the campaign's spend from it."""
    ledger, lease = exhausted_by_a_loss(tmp_path)
    ledger.settle(lease.lease_id, 0, "pulled", attempts_charged=1)
    ledger.observe_site(SITE, SiteReading(returns=(Return(lease.lease_id, 0.5),)))
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    assert lease.lease_id in facts.contradicted
    with pytest.raises(MergeRefused, match=f"lease {lease.lease_id} spent more than it is charged"):
        check_by_run(facts, [], None, None)


def test_c45_without_a_budget_an_unsettled_lost_lease_makes_the_spend_unknown(tmp_path):
    """C45: with no budget a lease's grant is 0, no bound on what a lost one spent: the campaign's spend is unknown,
    never 0."""
    ledger, clock = make(tmp_path, plan(tasks=1), max_attempts=3)
    join(ledger, clock, "fed-a")
    lease = leased(ledger, "fed-a")
    ledger.mark_sent(lease.lease_id)
    clock.advance(400.0)
    ledger.observe_site(SITE, SiteReading())
    ledger.prove_ended("fed-a", "terminal")
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    row = facts.leases[lease.lease_id]
    totals = spend_micro(facts, [], [Evidence(row, "expired", None)])
    assert totals[None] is None
    assert totals[row.model] is None


# -- C50: with declared platforms, the environment is compared within each platform --------------------------------

APPTAINER, DOCKER = "a" * 64, "d" * 64
ENV_HPC, ENV_VM = {"layout": "1" * 64}, {"layout": "2" * 64}


def two_platforms(tmp_path: Path):
    """A ledger declaring two platforms, with an HPC federation and a VM one joined (their environments pinned) and a
    lease issued on each: the facts and the two leases."""
    from expcore.campaign.merge_by_run import environment_digest

    ledger, clock = make(tmp_path, plan(tasks=2), platforms=(APPTAINER, DOCKER))
    for label, platform, environment in (("fed-hpc", APPTAINER, ENV_HPC), ("fed-vm", DOCKER, ENV_VM)):
        ledger.join(label, site=SITE, job_ids=(f"job-{label}",), job_end_at=clock() + 86_400.0,
                    fingerprint=ledger.config.fingerprint, environment=environment_digest(environment),
                    kill_wait_s=180.0, platform=platform)  # fmt: skip
    on_hpc, on_vm = leased(ledger, "fed-hpc"), leased(ledger, "fed-vm")
    facts = facts_of(tmp_path, ledger.path)
    ledger.close()
    return facts, on_hpc, on_vm


def store_on(lease, environment) -> LeaseStore:
    run = RunLease(lease.run_key, lease.lease_id, 0, 0, "0" * 64, "2099-01-01T00:00:00Z")
    here = Path(".")
    return LeaseStore(here, here, "", run, "", environment, {}, {lease.run_key: {}}, {lease.run_key: "done"})


def test_c50_the_merge_accepts_two_platforms_stores_each_with_its_platforms_environment(tmp_path):
    from expcore.campaign.merge_by_run import check_platform_environments

    facts, on_hpc, on_vm = two_platforms(tmp_path)
    check_platform_environments([store_on(on_hpc, ENV_HPC), store_on(on_vm, ENV_VM)], facts)


@pytest.mark.parametrize("vm_environment", [ENV_HPC, {"layout": "3" * 64}])
def test_c50_the_merge_refuses_a_store_whose_environment_is_not_its_platforms_pin(tmp_path, vm_environment):
    """C50: a VM store that ran against the HPC environment, or another one, is not its platform's: refused."""
    from expcore.campaign.merge_by_run import check_platform_environments

    facts, on_hpc, on_vm = two_platforms(tmp_path)
    with pytest.raises(MergeRefused, match="first join on its platform"):
        check_platform_environments([store_on(on_hpc, ENV_HPC), store_on(on_vm, vm_environment)], facts)


def test_c50_the_merge_refuses_two_stores_of_one_platform_whose_environments_differ(tmp_path):
    from expcore.campaign.merge_by_run import check_platform_environments

    facts, on_hpc, _ = two_platforms(tmp_path)
    other = store_on(on_hpc, {"layout": "4" * 64})
    with pytest.raises(MergeRefused, match="another environment than"):
        check_platform_environments([store_on(on_hpc, ENV_HPC), other], facts)
