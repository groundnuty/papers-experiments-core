"""The ledger's status: its ceiling and its history, what is committed and left, and the dollars locked in grants."""

from elastic_support import SITE, archive, assert_invariants, completion, finish, join, leased, lose, make, plan

from expcore.campaign.elastic import SiteReading

OPEN = {"runs": plan(tasks=4, conditions=("api-none",)), "max_usd": 10.0, "grants_usd": {"m1": 2.0}}


def test_c42_the_locked_total_is_right_through_issue_expiry_settle_and_completion(tmp_path):
    """C42: a grant is locked from its lease's issue until its spend is measured or settled, whatever its state (in
    flight, expired, its completion read but not verified, lost); a spend settled at exactly the grant is not locked."""
    ledger, clock = make(tmp_path, **OPEN)
    status = ledger.status()
    assert (status.locked_micro, status.committed_micro, status.remaining_micro) == (0, 0, 10_000_000)
    assert (status.ceiling_micro, status.ceilings, dict(status.locked_by_state)) == (10_000_000, (), {})
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    dying, reporting, finishing = (leased(ledger, label) for label in ("fed-1", "fed-2", "fed-3"))
    ledger.mark_sent(dying.lease_id)
    status = ledger.status()
    assert status.locked_micro == 6_000_000
    assert dict(status.locked_by_state) == {"issued": 4_000_000, "sent": 2_000_000}
    assert dict(status.locked_by_model) == {"m1": 6_000_000}
    ledger.prove_ended("fed-1", "terminal")  # the lease expires: its grant stays locked
    assert ledger.status().locked_micro == 6_000_000
    assert dict(ledger.status().locked_by_state) == {"expired": 2_000_000, "issued": 4_000_000}
    ledger.settle(dying.lease_id, 500_000, "root")
    status = ledger.status()
    assert (status.locked_micro, status.committed_micro, status.remaining_micro) == (4_000_000, 4_500_000, 5_500_000)
    ledger.observe_site(SITE, SiteReading(completions=(completion(reporting, spend_usd=0.6),)))
    assert dict(ledger.status().locked_by_state) == {"issued": 2_000_000, "reported": 2_000_000}  # not yet verified
    assert ledger.verify_archive(reporting.lease_id, archive(reporting.lease_id)) == "counted"
    assert ledger.status().locked_micro == 2_000_000
    assert finish(ledger, finishing, spend_usd=2.0) == "counted"
    status = ledger.status()
    assert (status.locked_micro, status.committed_micro, status.remaining_micro) == (0, 3_100_000, 6_900_000)
    full = lose(ledger, clock, "fed-4")
    assert ledger.status().locked_micro == 2_000_000
    ledger.settle(full.lease_id, 2_000_000, "pulled")  # charged its whole grant, yet settled: nothing is locked
    assert ledger.status().locked_micro == 0
    assert_invariants(ledger)


def test_c42_status_gives_the_remaining_budget_per_model_and_the_ceilings_history(tmp_path):
    """C42: remaining is the ceiling less every charge, in total and for each model with a ceiling; the history lists
    each raise in order with its reason and actor."""
    runs = plan(models=("m1", "m2"), tasks=2, conditions=("api-none",))
    ledger, clock = make(
        tmp_path, runs=runs, max_usd=10.0, per_model_usd={"m1": 4.0}, grants_usd={"m1": 1.0, "m2": 2.0}
    )
    for label in ("fed-1", "fed-2"):
        join(ledger, clock, label)
    leases = [leased(ledger, label) for label in ("fed-1", "fed-2")]
    status = ledger.status()
    assert status.committed_micro == 1_000_000 + 2_000_000
    assert status.remaining_micro == 7_000_000
    assert dict(status.model_ceiling_micro) == {"m1": 4_000_000}
    assert dict(status.model_remaining_micro) == {
        "m1": 4_000_000 - sum(lease.charge_micro for lease in leases if lease.model == "m1")
    }
    clock.advance(10)
    first = ledger.raise_ceiling(12_000_000, "one", "op-a")
    clock.advance(10)
    second = ledger.raise_model_ceiling("m1", 5_000_000, "two", "op-b")
    status = ledger.status()
    assert status.ceiling_micro == 12_000_000
    assert status.remaining_micro == 9_000_000
    assert status.ceilings == (first, second)
    assert dict(status.model_ceiling_micro) == {"m1": 5_000_000}
