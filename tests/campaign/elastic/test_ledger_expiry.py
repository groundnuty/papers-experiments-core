"""Expiry and the proof of a holder's end: a run is requeued at once, its grant and cap slot wait for the proof."""

import pytest
from elastic_support import (
    SITE,
    assert_invariants,
    completion,
    finish,
    join,
    leased,
    ledger_path,
    make,
    plan,
)

from expcore.campaign.elastic import Ledger, SiteReading, Wait
from expcore.campaign.lock import CampaignLocked

ONE_RUN = {"runs": plan(tasks=1, conditions=("api-none",))}


def _beat(ledger, lease, counter, site=SITE):
    ledger.observe_site(site, SiteReading(heartbeats={lease.lease_id: counter}))


def test_c42_expiry_before_proof_requeues_the_run_at_once_but_holds_its_grant_and_cap_slot(tmp_path):
    """C42: a stale heartbeat on a readable site expires the lease and puts its run back to pending at once; the cap
    slot and the grant stay held, so the model at cap 1 waits until the holder is proven ended, and then the same
    run goes out again with the expired lease charged at its bound."""
    ledger, clock = make(tmp_path, **ONE_RUN, caps={"m1": 1}, max_usd=10.0, grants_usd={"m1": 2.0})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    _beat(ledger, first, 1)
    clock.advance(301)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(first.lease_id).state == "expired"
    assert ledger.run(first.run_key).state == "pending"
    assert ledger.slots() == {"m1": 1}
    assert ledger.committed_micro()[0] == 2_000_000
    waiting = ledger.issue("fed-2")
    assert isinstance(waiting, Wait)
    assert "cap" in waiting.reason
    assert_invariants(ledger)
    ledger.prove_ended("fed-1", "cancelled")
    assert ledger.slots() == {}
    assert ledger.committed_micro()[0] == 2_000_000  # proven ended with no completion: charged at its bound
    second = leased(ledger, "fed-2")
    assert (second.run_key, second.attempt_base) == (first.run_key, 0)
    assert_invariants(ledger)


def test_c42_an_advancing_counter_renews_the_lease(tmp_path):
    """C42: a counter that advances at every read keeps its lease, however long the run takes within its term."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    for counter in range(1, 100):
        clock.advance(30)
        _beat(ledger, lease, counter)
    assert ledger.lease(lease.lease_id).state == "issued"


def test_c42_a_counter_that_stops_expires_its_lease_only_after_five_minutes_of_reads(tmp_path):
    """C42: a counter read unchanged for less than 5 minutes keeps the lease; at 5 minutes it expires."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    _beat(ledger, lease, 7)
    clock.advance(299)
    _beat(ledger, lease, 7)
    assert ledger.lease(lease.lease_id).state == "issued"
    clock.advance(1)
    _beat(ledger, lease, 7)
    assert ledger.lease(lease.lease_id).state == "expired"


def test_c42_a_lease_past_its_hard_term_expires_though_it_still_beats(tmp_path):
    """C42: a hung kernel can keep its heartbeat thread alive, so the hard term expires a lease that still beats."""
    ledger, clock = make(tmp_path, hard_term_s=600.0)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    for counter in range(1, 21):
        clock.advance(30)
        _beat(ledger, lease, counter)
    assert ledger.lease(lease.lease_id).state == "expired"


def test_c42_a_completion_in_the_same_reading_is_collected_before_the_lease_can_expire(tmp_path):
    """C42: collect before requeue: a reading that carries the completion of a lease gone stale counts it."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    clock.advance(1200)
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease),)))
    assert ledger.lease(lease.lease_id).state == "reported"
    assert ledger.run(lease.run_key).state == "leased"
    assert_invariants(ledger)


def test_c42_the_recorded_job_end_plus_kill_wait_proves_the_holder_ended(tmp_path):
    """C42: an expired lease is freed once its job's recorded end plus KillWait has passed and its site was read
    since then (so a completion written before the end is collected first); not before."""
    ledger, clock = make(tmp_path, caps={"m1": 1})
    join(ledger, clock, "fed-1", job_end_in_s=1000.0, kill_wait_s=180.0)
    lease = leased(ledger, "fed-1")
    clock.advance(400)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(lease.lease_id).state == "expired"
    clock.advance(779)  # 1179 s: one second short of the end plus KillWait
    ledger.observe_site(SITE, SiteReading())
    assert ledger.slots() == {"m1": 1}
    clock.advance(1)
    ledger.sweep()
    assert ledger.slots() == {"m1": 1}  # the site was last read before the end plus KillWait
    ledger.observe_site(SITE, SiteReading())
    assert ledger.slots() == {}
    assert ledger.lease(lease.lease_id).proven
    assert_invariants(ledger)


def test_c42_a_job_that_ended_while_the_orchestrator_was_down_is_read_before_its_lease_is_given_up(tmp_path):
    """C42: a job whose end plus KillWait passed during a restart keeps its live lease until its site is read: the
    completion it left is collected and counts, rather than the run being leased again and its result superseded."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1", job_end_in_s=600.0, kill_wait_s=180.0)
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(heartbeats={lease.lease_id: 1}))
    ledger.close()
    clock.advance(1200)
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    ledger.sweep()
    assert ledger.lease(lease.lease_id).state == "issued"
    assert ledger.run(lease.run_key).state == "leased"
    assert finish(ledger, lease) == "counted"
    assert ledger.federations()[0].end_proof == "walltime"
    assert_invariants(ledger)


def test_c42_a_job_that_ended_unread_for_a_whole_site_outage_is_proven_by_its_time(tmp_path):
    """C42: once a site is out, its jobs past their end plus KillWait are proven ended without a read."""
    ledger, clock = make(tmp_path, caps={"m1": 1})
    join(ledger, clock, "fed-1", job_end_in_s=100.0, kill_wait_s=180.0)
    leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading())
    clock.advance(899)
    ledger.sweep()
    assert ledger.slots() == {"m1": 1}
    clock.advance(1)
    ledger.sweep()
    assert ledger.slots() == {}
    assert_invariants(ledger)


def test_c42_a_federation_holding_an_expired_unproven_lease_gets_no_new_lease(tmp_path):
    """C42: a federation whose last lease expired may be hung, so it waits until its job is proven ended."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    clock.advance(301)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(lease.lease_id).state == "expired"
    assert "holds lease" in ledger.issue("fed-1").reason
    ledger.prove_ended("fed-1", "terminal")
    assert "ended" in ledger.issue("fed-1").reason


def test_c42_a_proof_of_end_settles_a_lease_still_live_on_its_federation(tmp_path):
    """C42: a job proven ended while its lease is live expires that lease, frees its slot and charges its bound."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.mark_sent(lease.lease_id)
    ledger.observe_site(SITE, SiteReading(ended={"fed-1": "terminal"}))
    view = ledger.lease(lease.lease_id)
    assert (view.state, view.slot, view.proven, view.charge_micro) == ("expired", False, True, 1_000_000)
    assert ledger.run(lease.run_key).state == "pending"
    assert_invariants(ledger)


def test_c42_an_unread_site_expires_nothing_until_it_is_marked_out(tmp_path):
    """C42: leases on a site that cannot be read are unobserved; after 15 minutes unread the site is out and its
    leases expire, their grants and slots held until each job is proven ended."""
    ledger, clock = make(tmp_path, caps={"m1": 2})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading())
    clock.advance(899)
    ledger.sweep()
    assert ledger.lease(lease.lease_id).state == "issued"
    clock.advance(1)
    ledger.sweep()
    assert ledger.lease(lease.lease_id).state == "expired"
    assert ledger.run(lease.run_key).state == "pending"
    assert ledger.slots() == {"m1": 1}
    assert_invariants(ledger)


def test_c42_a_restart_reloads_every_lease_and_expires_none_until_its_site_is_read(tmp_path):
    """C42: the ledger is reopened from its file after the orchestrator was down 20 minutes. Leases, grants and slots
    come back; nothing expires on a site not yet read since the reopen, and the site's 15 minutes start then. A read
    expires the lease whose counter stood still and renews the one that advanced."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    still, moving = leased(ledger, "fed-1"), leased(ledger, "fed-2")
    ledger.observe_site(SITE, SiteReading(heartbeats={still.lease_id: 1, moving.lease_id: 1}))
    before = (ledger.leases(), ledger.runs(), ledger.committed_micro(), ledger.slots())
    ledger.close()
    clock.advance(1200)
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert (ledger.leases(), ledger.runs(), ledger.committed_micro(), ledger.slots()) == before
    clock.advance(899)
    ledger.sweep()
    assert {lease.state for lease in ledger.leases()} == {"issued"}
    ledger.observe_site(SITE, SiteReading(heartbeats={still.lease_id: 1, moving.lease_id: 40}))
    assert ledger.lease(still.lease_id).state == "expired"
    assert ledger.lease(moving.lease_id).state == "issued"
    assert ledger.run(still.run_key).state == "pending"
    assert_invariants(ledger)


def test_c42_a_second_process_cannot_open_the_ledger(tmp_path):
    """C42: the ledger holds one dispatcher lock; a second open while it is held is refused."""
    ledger, clock = make(tmp_path)
    with pytest.raises(CampaignLocked):
        Ledger.open(ledger_path(tmp_path), clock=clock)
    ledger.close()
    Ledger.open(ledger_path(tmp_path), clock=clock).close()
