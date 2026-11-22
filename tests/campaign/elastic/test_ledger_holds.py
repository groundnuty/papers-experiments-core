"""C68: a run is leased again only once its earlier leases' charged attempts are final, so no lease lets a run pass
`max_attempts` across its leases; a settle that never comes holds it only until its settle is overdue."""

import pytest
from elastic_support import (
    SITE,
    assert_invariants,
    finish,
    join,
    leased,
    ledger_path,
    lose,
    make,
    plan,
)

from expcore.campaign.elastic import Ledger, Return, SiteReading, Wait
from expcore.campaign.elastic.control import ledger_view
from expcore.campaign.elastic.ledger import HELD_SHOWN

ONE = {"runs": plan(tasks=1, conditions=("api-none",))}
TWO = {"runs": plan(tasks=2, conditions=("api-none",))}
PRICED = {"max_usd": 10.0, "grants_usd": {"m1": 1.0}}


def _stale(ledger, clock, lease):
    """The lease's worker beats once and stops: its counter stands still for `stale_s` of reads and it expires."""
    ledger.observe_site(SITE, SiteReading(heartbeats={lease.lease_id: 1}))
    clock.advance(ledger.config.stale_s + 1)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(lease.lease_id).state == "expired"


def _waits(ledger, label, *words):
    got = ledger.issue(label)
    assert isinstance(got, Wait), f"{label} was leased {got}"
    for word in words:
        assert word in got.reason, got.reason
    return got


def test_c68_a_stale_lease_whose_store_charged_two_holds_its_run_until_settled_and_then_leaves_one_try(tmp_path):
    """C68, the shape seen live: a lease whose store charged two attempts expires stale. Its run is not leased again
    at base 0, neither before its holder is proven ended nor after (its settle may still name more than the loss's
    one). The settle names two, and the next lease follows at base 2 with exactly one try of 3 left, so the run's
    charged attempts across its leases stop at 3."""
    ledger, clock = make(tmp_path, **ONE, **PRICED, max_attempts=3)
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    _stale(ledger, clock, first)
    assert ledger.run(first.run_key).state == "pending"
    _waits(ledger, "fed-2", f"run {first.run_key} is held", "not proven ended")
    ledger.prove_ended("fed-1", "terminal")
    assert ledger.run(first.run_key).charged == 1  # the loss's one, not yet final
    _waits(ledger, "fed-2", f"run {first.run_key} is held", "not settled")
    assert_invariants(ledger)
    ledger.settle(first.lease_id, 400_000, "root", attempts_charged=2)
    second = leased(ledger, "fed-2")
    assert (second.run_key, second.attempt_base) == (first.run_key, 2)
    assert ledger.config.max_attempts - second.attempt_base == 1  # what the lease's store allows
    assert finish(ledger, second, state="infra_fail", charged=1) == "counted"
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (3, "infra_exhausted")
    assert_invariants(ledger)


def test_c68_a_held_run_keeps_its_set_and_the_sets_other_runs_still_lease(tmp_path):
    """C68: a held run is skipped, not waited on: the next run of the set is leased, and a federation with nothing
    left to lease is told which run is held and why."""
    ledger, clock = make(tmp_path, **TWO)
    for label in ("fed-1", "fed-2", "fed-3"):
        join(ledger, clock, label)
    first = leased(ledger, "fed-1")
    _stale(ledger, clock, first)
    other = leased(ledger, "fed-2")
    assert other.run_key != first.run_key
    waiting = _waits(ledger, "fed-3", f"run {first.run_key} is held: lease {first.lease_id} expired")
    assert waiting.reason.startswith("set 0 waits: ")
    assert ledger.demand().pending == {}
    assert ledger.demand().set_index == 0
    assert_invariants(ledger)


def test_c68_a_wait_names_a_few_held_runs_and_counts_the_rest(tmp_path):
    """C68: a site that goes out holds every run it had, so a wait names the first five held runs and counts the
    others, and stays one readable line."""
    ledger, clock = make(tmp_path, runs=plan(tasks=7, conditions=("api-none",)))
    labels = [f"fed-{n}" for n in range(8)]
    for label in labels:
        join(ledger, clock, label)
    leases = [leased(ledger, label) for label in labels[:7]]
    clock.advance(ledger.config.stale_s + 1)
    ledger.observe_site(SITE, SiteReading())
    assert {ledger.lease(lease.lease_id).state for lease in leases} == {"expired"}
    waiting = _waits(ledger, labels[7], "2 more run(s) held")
    assert waiting.reason.count(" is held: ") == HELD_SHOWN
    assert f"run {leases[0].run_key} is held" in waiting.reason
    assert f"run {leases[6].run_key} is held" not in waiting.reason


def test_c68_without_a_budget_a_loss_is_final_once_its_holder_is_proven_ended(tmp_path):
    """C68: a ledger with no budget settles nothing, so the loss's one attempt is final at the proof and the run is
    leased again at once, at base 1."""
    ledger, clock = make(tmp_path, **ONE)
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    _stale(ledger, clock, first)
    _waits(ledger, "fed-2", "not proven ended")
    ledger.prove_ended("fed-1", "terminal")
    assert leased(ledger, "fed-2").attempt_base == 1
    assert_invariants(ledger)


def test_c68_a_lost_lease_nobody_can_settle_releases_its_run_once_its_settle_is_overdue(tmp_path):
    """C68: a dead store nobody reads never settles, so it holds its run only `site_out_s` from its loss: the sweep
    then logs `settle_overdue` once, and the run is leased at the loss's one attempt."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    clock.advance(ledger.config.site_out_s - 1)
    ledger.observe_site(SITE, SiteReading())
    _waits(ledger, "fed-2", "not settled")
    clock.advance(1)
    ledger.observe_site(SITE, SiteReading())
    ledger.observe_site(SITE, SiteReading())  # read again: logged once
    (event,) = [event for event in ledger.events() if event["kind"] == "settle_overdue"]
    assert (event["lease_id"], event["run_key"]) == (lost.lease_id, lost.run_key)
    assert (event["detail"]["charged"], event["detail"]["waited_s"]) == (1, ledger.config.site_out_s)
    again = leased(ledger, "fed-2")
    assert (again.run_key, again.attempt_base) == (lost.run_key, 1)
    assert_invariants(ledger)


def test_c68_a_restart_gives_the_controller_a_whole_site_out_to_settle_before_a_loss_is_overdue(tmp_path):
    """C68: only the controller holding the ledger settles, so a loss whose wait ran out while it was down is not
    overdue at the first sweep after the reopen: the wait counts from the reopen, as expiry's does."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    ledger.close()
    clock.advance(ledger.config.site_out_s * 3)
    reopened = Ledger.open(ledger_path(tmp_path), clock)
    reopened.observe_site(SITE, SiteReading())
    assert not [event for event in reopened.events() if event["kind"] == "settle_overdue"]
    _waits(reopened, "fed-2", "not settled")
    clock.advance(reopened.config.site_out_s)
    reopened.observe_site(SITE, SiteReading())
    again = leased(reopened, "fed-2")
    assert (again.run_key, again.attempt_base) == (lost.run_key, 1)
    assert_invariants(reopened)
    reopened.close()


def test_c68_a_holder_the_controller_cancelled_is_overdue_only_after_its_killwait_too(tmp_path):
    """C68: a cancelled job may run on until KillWait passes, and its settle is refused until then, so its overdue
    counts from the proven end plus KillWait."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    join(ledger, clock, "fed-1", kill_wait_s=180.0)
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    ledger.prove_ended("fed-1", "cancelled")
    clock.advance(ledger.config.site_out_s)
    ledger.observe_site(SITE, SiteReading())
    _waits(ledger, "fed-2", "not settled")
    clock.advance(180.0)
    ledger.observe_site(SITE, SiteReading())
    assert leased(ledger, "fed-2").run_key == first.run_key
    assert_invariants(ledger)


@pytest.mark.parametrize("proven", [False, True])
def test_c68_an_unstarted_return_still_gives_the_attempt_back_and_releases_the_run(tmp_path, proven):
    """C68: a return shows the lease never started, so it cost its run nothing and its count is final: the run is
    leased again at base 0, whether the return comes before its holder's proof or after the loss counted one."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    _stale(ledger, clock, first)
    if proven:
        ledger.prove_ended("fed-1", "terminal")
        assert ledger.run(first.run_key).charged == 1
    _waits(ledger, "fed-2", "is held")
    ledger.observe_site(SITE, SiteReading(returns=(Return(first.lease_id, 0.0),)))
    assert ledger.run(first.run_key).charged == 0
    again = leased(ledger, "fed-2")
    assert (again.run_key, again.attempt_base) == (first.run_key, 0)
    assert_invariants(ledger)


def test_c68_a_counted_completion_leases_its_run_again_at_once_as_before(tmp_path):
    """C68 changes nothing on the normal path: a counted infra_fail is final, so its run is leased again at once at
    its charged attempts, with no settle and no wait, and a done run is done."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    first = leased(ledger, "fed-1")
    assert finish(ledger, first, state="infra_fail", charged=1) == "counted"
    second = leased(ledger, "fed-2")
    assert (second.run_key, second.attempt_base) == (first.run_key, 1)
    assert finish(ledger, second, state="done", charged=1) == "counted"
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (2, "done")
    assert not ledger.holds()
    assert not [event for event in ledger.events() if event["kind"] == "settle_overdue"]
    assert_invariants(ledger)


def test_c68_the_controller_keeps_a_federation_for_a_run_held_until_its_settle(tmp_path):
    """C68: a run held for a lost lease's settle is leased the moment the settle lands, so the controller's view
    counts it as one a settle may let be leased (not as demand) until then."""
    ledger, clock = make(tmp_path, **ONE, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    view = ledger_view(ledger)
    assert (view.demand.pending, view.reopenable) == ({}, 1)
    ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=1)
    view = ledger_view(ledger)
    assert (view.demand.pending, view.reopenable) == ({"m1": 1}, 0)
