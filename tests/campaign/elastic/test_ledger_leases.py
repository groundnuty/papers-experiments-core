"""The ledger's lease state machine and its fencing: one live lease per run, the latest lease's completion counts."""

import dataclasses

import pytest
from elastic_support import (
    ENVIRONMENT,
    FINGERPRINT,
    SITE,
    archive,
    assert_invariants,
    completion,
    finish,
    join,
    leased,
    make,
    plan,
)

from expcore.campaign.elastic import CompletionConflict, JoinRefused, Lease, LedgerError, Return, SiteReading, Wait


def _expire(ledger, clock, lease, site=SITE):
    """Let the lease's heartbeat go stale and read its site."""
    clock.advance(301)
    ledger.observe_site(site, SiteReading())
    assert ledger.lease(lease.lease_id).state == "expired"


def test_c42_a_lease_names_its_run_fingerprint_attempt_base_grant_and_hard_term(tmp_path):
    """C42: an issued lease carries what its node needs and is `issued` until its file is out, then `sent`."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.5})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    first = plan()[0]
    assert (lease.run_key, lease.model, lease.condition) == (first.run_key, "m1", first.condition)
    assert (lease.fingerprint, lease.attempt_base, lease.grant_usd) == (FINGERPRINT, 0, 1.5)
    assert lease.hard_term_at == clock() + 3600.0
    assert ledger.lease(lease.lease_id).state == "issued"
    assert ledger.run(lease.run_key).state == "leased"
    ledger.mark_sent(lease.lease_id)
    assert ledger.lease(lease.lease_id).state == "sent"
    assert_invariants(ledger)


def test_c42_a_run_is_never_leased_twice_at_once(tmp_path):
    """C42: with one run and two idle federations, the second federation waits."""
    ledger, clock = make(tmp_path, runs=plan(conditions=("api-none",), tasks=1))
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    leased(ledger, "fed-1")
    assert isinstance(ledger.issue("fed-2"), Wait)
    assert_invariants(ledger)


def test_c42_a_federation_gets_its_next_lease_once_its_last_is_completed(tmp_path):
    """C42: a federation holding a lease waits; reading its completion frees it, before the archive is verified."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    first = leased(ledger, "fed-1")
    assert "holds lease" in ledger.issue("fed-1").reason
    ledger.observe_site(SITE, SiteReading(completions=(completion(first),)))
    second = leased(ledger, "fed-1")
    assert second.lease_id > first.lease_id
    assert_invariants(ledger)


@pytest.mark.parametrize("first_to_report", ["old", "new"])
def test_c42_a_duplicate_completion_counts_the_latest_lease_and_keeps_the_other_as_evidence(tmp_path, first_to_report):
    """C42: a run leased again after its lease expired has two completions; the latest lease's counts whatever order
    they arrive in, and the other is superseded, its spend counted."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)), max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    join(ledger, clock, "fed-2")
    old = leased(ledger, "fed-1")
    _expire(ledger, clock, old)
    ledger.prove_ended("fed-1", "cancelled")
    new = leased(ledger, "fed-2")
    assert new.run_key == old.run_key
    assert new.lease_id > old.lease_id
    order = [old, new] if first_to_report == "old" else [new, old]
    verdicts = {lease.lease_id: finish(ledger, lease, spend_usd=0.4) for lease in order}
    assert verdicts == {old.lease_id: "superseded", new.lease_id: "counted"}
    run = ledger.run(new.run_key)
    assert (run.state, run.lease_id) == ("done", new.lease_id)
    assert ledger.committed_micro()[0] == 800_000  # both leases' measured spend
    assert_invariants(ledger)


def test_c42_a_late_completion_of_the_latest_lease_still_counts_after_its_expiry(tmp_path):
    """C42: fencing reads lease order only: an expired lease that is still its run's latest counts when it completes."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)))
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    _expire(ledger, clock, lease)
    assert ledger.run(lease.run_key).state == "pending"
    assert finish(ledger, lease) == "counted"
    assert ledger.run(lease.run_key).state == "done"
    assert_invariants(ledger)


def test_c42_the_same_completion_read_twice_changes_nothing(tmp_path):
    """C42: a restart reads the results directories again; the same completion again adds no event and no spend."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    assert finish(ledger, lease) == "counted"
    events, committed = len(ledger.events()), ledger.committed_micro()
    assert finish(ledger, lease) == "counted"
    assert (len(ledger.events()), ledger.committed_micro()) == (events, committed)
    assert_invariants(ledger)


def test_c42_a_second_completion_of_one_lease_with_another_archive_is_refused(tmp_path):
    """C42: two different completions of one lease are refused, naming the lease, and the reading changes nothing."""
    ledger, clock = make(tmp_path)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease),)))
    events = len(ledger.events())
    other = dataclasses.replace(completion(lease), archive_sha256=archive(999))
    with pytest.raises(CompletionConflict, match=f"lease {lease.lease_id}"):
        ledger.observe_site(SITE, SiteReading(completions=(other,)))
    assert len(ledger.events()) == events
    assert_invariants(ledger)


def test_c42_a_returned_lease_puts_its_run_back_unstarted_and_uncharged(tmp_path):
    """C42: a lease returned unstarted frees its federation and slot, charges what it spent, and keeps its attempt
    base: the run's next lease starts from the same one."""
    ledger, clock = make(tmp_path, runs=plan(tasks=1, conditions=("api-none",)), max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(returns=(Return(lease.lease_id, spend_usd=0.01),)))
    assert ledger.lease(lease.lease_id).state == "returned"
    assert ledger.run(lease.run_key).state == "pending"
    assert ledger.committed_micro()[0] == 10_000
    again = leased(ledger, "fed-1")
    assert (again.run_key, again.attempt_base) == (lease.run_key, 0)
    assert_invariants(ledger)


def test_c42_a_lease_whose_file_never_landed_is_cancelled_and_only_while_issued(tmp_path):
    """C42: an issued lease is cancelled with nothing charged and its run pending; a sent one cannot be cancelled."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.cancel(lease.lease_id)
    assert ledger.lease(lease.lease_id).state == "cancelled"
    assert (ledger.run(lease.run_key).state, ledger.committed_micro()[0]) == ("pending", 0)
    sent = leased(ledger, "fed-1")
    ledger.mark_sent(sent.lease_id)
    with pytest.raises(LedgerError, match="sent"):
        ledger.cancel(sent.lease_id)
    assert_invariants(ledger)


def test_c42_an_archive_that_never_verifies_is_a_lost_lease_charged_at_its_bound(tmp_path):
    """C42: a completion whose archive does not verify is lost: the run goes back to pending and the grant stays
    charged, never the completion's measured spend."""
    ledger, clock = make(tmp_path, max_usd=10.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease, spend_usd=0.1),)))
    assert ledger.committed_micro()[0] == 1_000_000  # the grant, until the archive verifies
    assert ledger.verify_archive(lease.lease_id, None) == "lost"
    assert ledger.run(lease.run_key).state == "pending"
    assert ledger.committed_micro()[0] == 1_000_000
    assert_invariants(ledger)


def test_c42_the_status_rule_runs_across_leases(tmp_path):
    """C42: a counted infrastructure failure under the attempt limit returns the run to pending with its charged
    attempts as the next lease's base; at the limit it is exhausted; a harness error ends its run."""
    runs = plan(tasks=1)
    ledger, clock = make(tmp_path, runs=runs, max_attempts=2)
    join(ledger, clock, "fed-1")
    first = leased(ledger, "fed-1")
    assert finish(ledger, first, state="infra_fail", charged=1) == "counted"
    assert ledger.run(first.run_key).state == "pending"
    second = leased(ledger, "fed-1")
    assert (second.run_key, second.attempt_base) == (first.run_key, 1)
    finish(ledger, second, state="infra_fail", charged=1)
    assert ledger.run(first.run_key).state == "infra_exhausted"
    third = leased(ledger, "fed-1")
    finish(ledger, third, state="harness_error", charged=0)
    assert ledger.run(third.run_key).state == "harness_error"
    assert isinstance(ledger.issue("fed-1"), Wait)
    assert_invariants(ledger)


def test_c42_a_federation_with_another_fingerprint_or_environment_is_refused_at_join(tmp_path):
    """C42: the campaign fingerprint is pinned at creation and the environment at the first join; a federation
    with either different is refused before any lease, and nothing of it is kept."""
    ledger, clock = make(tmp_path)
    common = {"site": SITE, "job_ids": ("1",), "job_end_at": clock() + 3600, "kill_wait_s": 180.0}
    with pytest.raises(JoinRefused, match="fingerprint"):
        ledger.join("fed-1", fingerprint="0" * 64, environment=ENVIRONMENT, **common)
    ledger.join("fed-1", fingerprint=FINGERPRINT, environment=ENVIRONMENT, **common)
    with pytest.raises(JoinRefused, match="environment"):
        ledger.join("fed-2", fingerprint=FINGERPRINT, environment="1" * 64, **common)
    with pytest.raises(JoinRefused, match="already"):
        ledger.join("fed-1", fingerprint=FINGERPRINT, environment=ENVIRONMENT, **common)
    with pytest.raises(LedgerError, match="fed-2"):
        ledger.issue("fed-2")
    assert [f.label for f in ledger.federations()] == ["fed-1"]
    assert isinstance(ledger.issue("fed-1"), Lease)


def test_c42_a_heartbeat_shows_the_lease_file_landed_so_the_lease_can_no_longer_be_cancelled(tmp_path):
    """C42: a dispatcher that crashed before marking a lease sent finds its heartbeat at the next read: the lease is
    then sent, and cancelling it (which would free a grant its worker is spending) is refused."""
    ledger, clock = make(tmp_path, max_usd=1.0, grants_usd={"m1": 1.0})
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    clock.advance(30)
    ledger.observe_site(SITE, SiteReading(heartbeats={lease.lease_id: 1}))
    assert ledger.lease(lease.lease_id).state == "sent"
    with pytest.raises(LedgerError, match="sent"):
        ledger.cancel(lease.lease_id)
    assert ledger.committed_micro()[0] == 1_000_000
    assert_invariants(ledger)


def test_c42_the_pinned_configuration_cannot_be_changed_through_the_open_ledger(tmp_path):
    """C42: the caps, ceilings and grants a ledger was created with are read-only on the ledger that holds them."""
    ledger, _ = make(tmp_path, caps={"m1": 1}, max_usd=10.0, per_model_usd={"m1": 5.0}, grants_usd={"m1": 1.0})
    for name in ("caps", "per_model_usd", "grants_usd"):
        with pytest.raises(TypeError):
            getattr(ledger.config, name)["m1"] = 99
    assert ledger.config.caps == {"m1": 1}
