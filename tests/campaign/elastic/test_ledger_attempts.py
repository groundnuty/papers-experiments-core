"""A run's charged attempts across its leases: one count, a lost lease costing one, and settle giving the real one."""

import pytest
from elastic_support import SITE, assert_invariants, completion, finish, join, leased, lose, make, plan

from expcore.campaign.elastic import Return, SettleRefused, SiteReading, Wait

ONE = {"runs": plan(tasks=1, conditions=("api-none",))}
PRICED = {**ONE, "max_usd": 10.0, "grants_usd": {"m1": 1.0}}


def test_c42_a_lost_lease_nobody_can_read_costs_its_run_one_charged_attempt_and_a_run_that_keeps_losing_ends(tmp_path):
    """C42: the loss is itself one infrastructure attempt: the next lease's attempt base counts it, and a run that
    keeps losing its federation reaches `max_attempts` and is exhausted instead of being leased for ever."""
    ledger, clock = make(tmp_path, **ONE, max_attempts=2)
    first = lose(ledger, clock, "fed-1")
    assert ledger.run(first.run_key).charged == 1
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    assert second.attempt_base == 1
    ledger.prove_ended("fed-2", "terminal")
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (2, "infra_exhausted")
    join(ledger, clock, "fed-3")
    waiting = ledger.issue("fed-3")
    assert isinstance(waiting, Wait)
    assert "no run is pending" in waiting.reason
    assert_invariants(ledger)


def test_c42_a_lease_whose_archive_never_verified_costs_at_least_one_attempt_and_what_its_completion_named(tmp_path):
    """C42: a completion read whose archive never arrived is lost: the attempts it named, or one."""
    ledger, clock = make(tmp_path / "named", **ONE)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease, state="infra_fail", charged=2),)))
    assert ledger.verify_archive(lease.lease_id, None) == "lost"
    assert ledger.run(lease.run_key).charged == 2
    other, other_clock = make(tmp_path / "none", **ONE)
    join(other, other_clock, "fed-1")
    unnamed = leased(other, "fed-1")
    other.observe_site(SITE, SiteReading(completions=(completion(unnamed, state="harness_error", charged=0),)))
    assert other.verify_archive(unnamed.lease_id, None) == "lost"
    assert other.run(unnamed.run_key).charged == 1
    assert_invariants(ledger)
    assert_invariants(other)


@pytest.mark.parametrize(
    ("state", "charged", "expected"), [("infra_fail", 1, 1), ("infra_fail", 2, 2), ("harness_error", 0, 0)]
)
def test_c42_a_completion_read_after_the_loss_was_counted_replaces_it_with_its_own_charged_attempts(
    tmp_path, state, charged, expected
):
    """C42: the late completion's charged attempts, not the loss's one and its own on top."""
    ledger, clock = make(tmp_path, **ONE)
    lost = lose(ledger, clock, "fed-1")
    assert ledger.run(lost.run_key).charged == 1
    assert finish(ledger, lost, state=state, charged=charged) == "counted"
    assert ledger.run(lost.run_key).charged == expected
    assert_invariants(ledger)


def test_c42_settling_with_the_attempts_charged_replaces_the_losss_one(tmp_path):
    """C42: a settle that reads the dead store's log gives the lease's charged attempts, which replace the one the
    loss was charged; without them the one stays."""
    ledger, clock = make(tmp_path, **PRICED)
    unread, two, none = (lose(ledger, clock, f"fed-{n}") for n in (1, 2, 3))
    ledger.settle(unread.lease_id, 100_000, "root")
    assert ledger.run(unread.run_key).charged == 3  # one for each of the three losses
    first = ledger.settle(two.lease_id, 100_000, "root", attempts_charged=2)
    assert first.charged == 2
    assert ledger.run(two.run_key).charged == 4
    ledger.settle(none.lease_id, 100_000, "pulled", attempts_charged=0)
    assert ledger.run(none.run_key).charged == 3
    assert ledger.settle(two.lease_id, 100_000, "root", attempts_charged=2) == first
    assert ledger.run(two.run_key).charged == 3 + 0  # the same call again adds nothing
    assert_invariants(ledger)


def test_c42_a_settle_that_reaches_max_attempts_exhausts_a_pending_run(tmp_path):
    """C42: a run whose charged attempts reach `max_attempts` with no lease in flight has none left."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=3)
    lost = lose(ledger, clock, "fed-1")
    assert ledger.run(lost.run_key).state == "pending"
    ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=3)
    run = ledger.run(lost.run_key)
    assert (run.charged, run.state) == (3, "infra_exhausted")
    assert_invariants(ledger)


def test_c42_a_settle_refuses_more_attempts_than_the_lease_could_have_charged_and_values_that_are_not_counts(tmp_path):
    """C42: a lease at base 0 of a run allowed 3 attempts charged at most 3; more means the log is wrong, and nothing
    is settled. A count is a whole number, not below zero; other counts for a settled lease are refused."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=3)
    lost = lose(ledger, clock, "fed-1")
    with pytest.raises(SettleRefused, match="attempts"):
        ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=4)
    assert ledger.settlements() == []
    assert ledger.lease(lost.lease_id).charge_micro == 1_000_000
    for bad in (-1, True, 1.5, "2"):
        with pytest.raises(ValueError, match="attempts_charged"):
            ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=bad)
    ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=1)
    for other in (0, 2, None):
        with pytest.raises(SettleRefused, match="already settled"):
            ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=other)
    assert ledger.run(lost.run_key).charged == 1


@pytest.mark.parametrize("how", ["cancel", "return", "expire"])
def test_c42_a_run_put_back_with_no_attempt_left_is_exhausted_however_it_goes_back(tmp_path, how):
    """C42: a settle names two attempts for lease A while B is in flight, so when B is cancelled, returned unstarted or
    expired the run is pending with every attempt of 2 charged: it is exhausted, not leased again."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    lost = lose(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    assert second.attempt_base == 1
    ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=2)
    assert ledger.run(lost.run_key).state == "leased"
    if how == "cancel":
        ledger.cancel(second.lease_id)
    elif how == "return":
        ledger.observe_site(SITE, SiteReading(returns=(Return(second.lease_id, 0.0),)))
    else:
        clock.advance(301)
        ledger.observe_site(SITE, SiteReading())
        assert ledger.lease(second.lease_id).state == "expired"
    run = ledger.run(lost.run_key)
    assert (run.charged, run.state) == (2, "infra_exhausted")
    join(ledger, clock, "fed-3")
    assert isinstance(ledger.issue("fed-3"), Wait)
    assert_invariants(ledger)


def test_c42_a_superseded_lease_whose_archive_never_arrives_costs_only_the_one_its_loss_did(tmp_path):
    """C42: a late completion of A that is not its run's latest lease counts nothing it names (as a verified one
    counts nothing), so its missing archive leaves A at the one attempt its loss cost."""
    ledger, clock = make(tmp_path, **ONE, max_attempts=3)
    lost = lose(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lost, state="infra_fail", charged=2),)))
    assert ledger.lease(lost.lease_id).verdict == "superseded"
    assert ledger.verify_archive(lost.lease_id, None) == "lost"
    assert ledger.run(lost.run_key).charged == 1
    ledger.observe_site(SITE, SiteReading(returns=(Return(second.lease_id, 0.0),)))
    assert leased(ledger, "fed-2").attempt_base == 1
    assert_invariants(ledger)


def test_c42_a_lease_settled_with_its_attempts_keeps_them_when_its_late_archive_never_arrives(tmp_path):
    """C42: the settle said zero charged attempts: a late completion of the lease that is then lost does not put the
    one-attempt minimum of an unread loss back."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    lost = lose(ledger, clock, "fed-1")
    ledger.settle(lost.lease_id, 100_000, "root", attempts_charged=0)
    assert ledger.run(lost.run_key).charged == 0
    ledger.observe_site(SITE, SiteReading(completions=(completion(lost, state="harness_error", charged=0),)))
    assert ledger.verify_archive(lost.lease_id, None) == "lost"
    assert ledger.run(lost.run_key).charged == 0
    join(ledger, clock, "fed-2")
    assert leased(ledger, "fed-2").attempt_base == 0
    assert_invariants(ledger)


def test_c42_a_lost_lease_whose_return_is_read_later_costs_nothing(tmp_path):
    """C42: the return shows the lease never started, so the attempt its loss was counted for is taken back."""
    ledger, clock = make(tmp_path, **ONE, max_attempts=2)
    lost = lose(ledger, clock, "fed-1")
    assert ledger.run(lost.run_key).charged == 1
    ledger.observe_site(SITE, SiteReading(returns=(Return(lost.lease_id, 0.0),)))
    assert ledger.lease(lost.lease_id).state == "returned"
    assert ledger.run(lost.run_key).charged == 0
    join(ledger, clock, "fed-2")
    assert leased(ledger, "fed-2").attempt_base == 0
    assert_invariants(ledger)
