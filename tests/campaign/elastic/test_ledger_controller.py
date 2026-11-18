"""What a controller asks of the ledger: draining a federation, and the current set's demand per model."""

import pytest
from elastic_support import SITE, assert_invariants, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Demand, Ledger, LedgerError, SiteReading, Wait


def test_c42_a_draining_federation_gets_no_new_lease_and_its_lease_in_flight_still_counts(tmp_path):
    """C42: a federation drained near its walltime finishes its lease, which counts, and is then given nothing; the
    drain survives a restart."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.drain("fed-1")
    assert finish(ledger, lease) == "counted"
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    waiting = ledger.issue("fed-1")
    assert isinstance(waiting, Wait)
    assert "draining" in waiting.reason
    assert ledger.federations()[0].drained_at is not None
    with pytest.raises(LedgerError, match="fed-9"):
        ledger.drain("fed-9")
    assert_invariants(ledger)


def test_c42_demand_gives_the_current_set_its_pending_runs_and_the_slots_held_per_model(tmp_path):
    """C42: the controller's target reads the current set, its pending runs per model and the cap slots held per
    model (in flight and held back); an expired lease's run counts as pending again while its slot stays held."""
    runs = plan(models=("m1", "m2"), tasks=2, sets={"mount-read": 1})
    ledger, clock = make(tmp_path, runs=runs, caps={"m1": 1})
    assert ledger.demand() == Demand(set_index=0, pending={"m1": 2, "m2": 2}, slots={})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    leased(ledger, "fed-2")
    assert ledger.demand() == Demand(set_index=0, pending={"m1": 1, "m2": 1}, slots={"m1": 1, "m2": 1})
    clock.advance(301)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(first.lease_id).state == "expired"
    assert ledger.demand() == Demand(set_index=0, pending={"m1": 2, "m2": 2}, slots={"m1": 1, "m2": 1})
    ledger.prove_ended("fed-1", "cancelled")
    ledger.prove_ended("fed-2", "cancelled")
    assert ledger.demand() == Demand(set_index=0, pending={"m1": 2, "m2": 2}, slots={})


def test_c42_demand_is_empty_once_no_run_is_pending(tmp_path):
    """C42: with every run leased or ended, there is no current set and no pending run."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)))
    join(ledger, clock, "fed-1")
    leased(ledger, "fed-1")
    assert ledger.demand() == Demand(set_index=None, pending={}, slots={"m1": 1})
