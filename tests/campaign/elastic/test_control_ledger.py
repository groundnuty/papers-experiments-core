"""What the elastic controller reads of a ledger (C43): the demand, which lease holds each federation's slot, the
budget left, since when each federation is idle; and a dry run's read-only view, which takes no lock and writes
nothing."""

import pytest
from control_support import member, settings, site
from elastic_support import SITE, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, SiteReading
from expcore.campaign.elastic.control import Add, Drain, Retire, decide, environment_digest, ledger_view, read_only


def test_c43_the_ledger_view_gives_slots_held_leases_in_flight_budget_left_and_idle_since(tmp_path):
    """C43: one lease in flight and one completed: the first federation holds a sent lease, the second is idle since its
    report; the budget left is max_usd less every lease's charge; the hard term is the ledger's."""
    runs = plan(models=("m1",), tasks=3)
    ledger, clock = make(tmp_path, runs=runs, max_usd=100.0, grants_usd={"m1": 10.0}, per_model_usd={"m1": 50.0})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    ledger.mark_sent(first.lease_id)
    second = leased(ledger, "fed-2")
    clock.advance(120)
    assert finish(ledger, second, spend_usd=0.5) == "counted"
    view = ledger_view(ledger)
    assert view.holding == {"fed-1": "sent"}
    assert view.in_flight == 1
    assert view.left_micro == 100_000_000 - 10_000_000 - 500_000
    assert view.left_per_model_micro == {"m1": 50_000_000 - 10_000_000 - 500_000}
    assert view.grants_micro == {"m1": 10_000_000}
    assert view.idle_since["fed-2"] == clock()
    assert view.idle_since["fed-1"] == ledger.federations()[0].joined_at
    assert view.hard_term_s == ledger.config.hard_term_s
    assert view.leases_wanted() == 3  # 4 runs pending; max_usd leaves 8 grants of USD 10, the ceiling 3
    ledger.close()


def test_c43_a_dry_runs_read_only_view_reads_beside_the_driver_and_never_writes(tmp_path):
    """C43: while the driving process holds the ledger's lock, the read-only view reads the same demand and slots, and
    the ledger's file is byte for byte what it was."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    leased(ledger, "fed-1")
    ledger.observe_site("site-a", SiteReading())
    before = ledger_path(tmp_path).read_bytes()
    view = read_only(ledger_path(tmp_path))
    try:
        assert ledger_view(view).holding == ledger_view(ledger).holding
        assert view.demand() == ledger.demand()
    finally:
        view.close()
    assert ledger_path(tmp_path).read_bytes() == before
    ledger.close()
    reopened = Ledger.open(ledger_path(tmp_path), clock=clock)
    reopened.close()


def test_c43_an_exhausted_run_with_an_unsettled_lost_lease_is_counted_as_one_that_may_reopen(tmp_path):
    """C43: with one attempt allowed, a lease lost with its holder proven ended exhausts its run by the loss's charge;
    the view counts it as reopenable until a settle showing no attempt charged reopens it."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)), max_attempts=1, max_usd=30.0,
                         grants_usd={"m1": 10.0})  # fmt: skip
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.mark_sent(lease.lease_id)
    ledger.prove_ended("fed-1", "terminal")
    assert ledger.run(lease.run_key).state == "infra_exhausted"
    assert ledger_view(ledger).reopenable == 1
    ledger.settle(lease.lease_id, 100_000, "root", attempts_charged=0)
    assert ledger.run(lease.run_key).state == "pending"
    assert ledger_view(ledger).reopenable == 0
    ledger.close()


def test_c43_the_environment_a_federation_joins_with_is_the_digest_step_4_checks():
    """C43: the SHA-256 of the identity's canonical JSON (keys sorted, no spaces), whatever the keys' order; an empty
    or non-mapping identity is refused."""
    digest = "78bd403ced04f551ce0683afa7aedfd2d6d5df93e9375054cef49f4b54a94ffe"
    assert environment_digest({"core_commit": "0123abcd", "python": "3.12.7"}) == digest
    assert environment_digest({"python": "3.12.7", "core_commit": "0123abcd"}) == digest
    for bad in ({}, ["python"]):
        with pytest.raises(ValueError, match="non-empty mapping"):
            environment_digest(bad)


def test_c68_a_held_run_last_in_its_set_leaves_demand_at_zero_and_the_controller_drains_only_idle_members(tmp_path):
    """C68: the set's last pending run is held for its expired lease, so demand is 0 until the hold ends and nothing
    goes to the later set. The controller's tick plans nothing worse than at any demand of 0: the member whose lease
    expired on a running job is drained to be replaced, as before, the idle one is drained past its re-up cost, the
    one holding a live lease is left alone, and nothing is retired or added."""
    runs = plan(models=("m1", "m2"), tasks=1, sets={"mount-read": 1})  # set 0: each model's api-none run
    ledger, clock = make(tmp_path, runs=runs)
    labels = ("site-a-fed-1", "site-a-fed-2", "site-a-fed-3")
    for label in labels:
        join(ledger, clock, label, site=SITE)
    held, live = leased(ledger, labels[0]), leased(ledger, labels[1])
    ledger.observe_site(SITE, SiteReading(heartbeats={held.lease_id: 1, live.lease_id: 1}))
    clock.advance(ledger.config.stale_s + 1)
    ledger.observe_site(SITE, SiteReading(heartbeats={live.lease_id: 2}))
    assert ledger.lease(held.lease_id).state == "expired"
    assert held.run_key in ledger.holds()
    view = ledger_view(ledger)
    assert (view.demand.set_index, view.demand.pending, view.leases_wanted()) == (0, {}, 0)
    fleet = site(members=[member("fed-1"), member("fed-2"), member("fed-3")])
    decision = decide(clock(), view, [(settings(retire_idle_s=60.0), fleet)])
    drained = sorted(action.label for action in decision.actions if isinstance(action, Drain))
    assert drained == ["site-a-fed-1", "site-a-fed-3"]
    assert not [action for action in decision.actions if isinstance(action, Retire | Add)]
    assert all(getattr(action, "label", None) != labels[1] for action in decision.actions)
