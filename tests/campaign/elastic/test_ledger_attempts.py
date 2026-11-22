"""A run's charged attempts across its leases: one count, a lost lease costing one, and settle giving the real one."""

import pytest
from elastic_support import SITE, assert_invariants, completion, finish, join, leased, lose, make, overdue, plan

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
    ("state", "charged", "expected"),
    [("infra_fail", 1, 1), ("infra_fail", 2, 2), ("harness_error", 0, 0), ("outage_discarded", 0, 0)],
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
    loss was charged; without them the one stays. Each loss's settle goes overdue (C68) before the run is leased
    again, and a settle read after that still counts."""
    ledger, clock = make(tmp_path, **PRICED)
    losses = []
    for n in (1, 2, 3):
        losses.append(lose(ledger, clock, f"fed-{n}"))
        overdue(ledger, clock)
    unread, two, none = losses
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
    """C42: a settle names two attempts for lease A while B is in flight (A's settle went overdue first, C68), so when
    B is cancelled, returned unstarted or expired the run is pending with every attempt of 2 charged: it is
    exhausted, not leased again."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    lost = lose(ledger, clock, "fed-1")
    overdue(ledger, clock)
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


def _reopens(ledger, run_key):
    return [event for event in ledger.events() if event["kind"] == "reopen" and event["run_key"] == run_key]


def test_c42_a_lost_lease_exhausts_its_run_at_max_attempts(tmp_path):
    """C42: the loss counts one attempt, so with one attempt allowed the run is exhausted at once and not leased."""
    ledger, clock = make(tmp_path, **ONE, max_attempts=1)
    lost = lose(ledger, clock, "fed-1")
    run = ledger.run(lost.run_key)
    assert (run.charged, run.state) == (1, "infra_exhausted")
    join(ledger, clock, "fed-2")
    assert isinstance(ledger.issue("fed-2"), Wait)
    assert not _reopens(ledger, lost.run_key)
    assert_invariants(ledger)


@pytest.mark.parametrize(("how", "word"), [("settle", "settle"), ("return", "return"), ("completion", "completion")])
def test_c42_a_run_its_losses_exhausted_reopens_when_the_real_count_leaves_it_below_max_attempts(tmp_path, how, word):
    """C42: two lost leases exhaust a run of two attempts, the second one conservatively. When the second lease's real
    count (a settle, a return that shows it unstarted, a late completion) is lower, the run is below `max_attempts`
    again: it goes back to pending, the log names the lease and the old and new counts, and the next lease leases it.
    The first loss's settle went overdue (C68) before the second lease."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    first = lose(ledger, clock, "fed-1")
    overdue(ledger, clock)
    second = lose(ledger, clock, "fed-2")
    assert second.run_key == first.run_key
    assert (ledger.run(first.run_key).charged, ledger.run(first.run_key).state) == (2, "infra_exhausted")
    if how == "settle":
        ledger.settle(second.lease_id, 100_000, "root", attempts_charged=0)
    elif how == "return":
        ledger.observe_site(SITE, SiteReading(returns=(Return(second.lease_id, 0.0),)))
    else:
        assert finish(ledger, second, state="infra_fail", charged=0) == "counted"
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (1, "pending")
    (event,) = _reopens(ledger, first.run_key)
    assert event["lease_id"] == second.lease_id
    detail = event["detail"]
    assert (detail["lease_charged_before"], detail["lease_charged_after"]) == (1, 0)
    assert (detail["run_charged_before"], detail["run_charged_after"], detail["max_attempts"]) == (2, 1, 2)
    assert word in detail["reason"]
    assert_invariants(ledger)
    join(ledger, clock, "fed-3")
    again = leased(ledger, "fed-3")
    assert (again.run_key, again.attempt_base) == (first.run_key, 1)


def test_c42_a_run_exhausted_by_its_real_attempts_stays_exhausted_when_a_loss_is_taken_back(tmp_path):
    """C42: a settle names two real attempts of two allowed while the next lease is in flight; that lease expires, so
    the run is exhausted by real attempts. Its own loss, counted one on top and then settled at none, leaves the run at
    the two real ones: still no attempt left, so no reopen. The first loss's settle went overdue (C68) before the
    next lease."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    first = lose(ledger, clock, "fed-1")
    overdue(ledger, clock)
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    ledger.settle(first.lease_id, 100_000, "root", attempts_charged=2)
    clock.advance(301)
    ledger.observe_site(SITE, SiteReading())
    assert ledger.lease(second.lease_id).state == "expired"
    ledger.prove_ended("fed-2", "terminal")
    assert (ledger.run(first.run_key).charged, ledger.run(first.run_key).state) == (3, "infra_exhausted")
    ledger.settle(second.lease_id, 100_000, "root", attempts_charged=0)
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (2, "infra_exhausted")
    assert not _reopens(ledger, first.run_key)
    join(ledger, clock, "fed-3")
    assert isinstance(ledger.issue("fed-3"), Wait)
    assert_invariants(ledger)


def test_c42_a_done_run_does_not_reopen_when_an_earlier_loss_is_settled_at_fewer_attempts(tmp_path):
    """C42: a run is done by its second lease's counted completion; settling the first lease's loss at none lowers the
    run's charged attempts below `max_attempts` and leaves it done. The first loss's settle went overdue (C68) before
    the second lease."""
    ledger, clock = make(tmp_path, **PRICED, max_attempts=2)
    first = lose(ledger, clock, "fed-1")
    overdue(ledger, clock)
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    assert finish(ledger, second, state="done", charged=1) == "counted"
    assert (ledger.run(first.run_key).charged, ledger.run(first.run_key).state) == (2, "done")
    ledger.settle(first.lease_id, 100_000, "root", attempts_charged=0)
    run = ledger.run(first.run_key)
    assert (run.charged, run.state) == (1, "done")
    assert not _reopens(ledger, first.run_key)
    assert_invariants(ledger)
