"""Settling a lost lease: its held grant becomes the spend read from its dead federation's call log (C42)."""

import pytest
from elastic_support import (
    SITE,
    archive,
    assert_invariants,
    completion,
    finish,
    join,
    leased,
    ledger_path,
    lose,
    make,
    plan,
)

from expcore.campaign.elastic import Ledger, LedgerError, Return, SettleRefused, SiteReading, Wait

PRICED = {"runs": plan(tasks=4, conditions=("api-none",)), "max_usd": 2.0, "grants_usd": {"m1": 1.0}}
LOOSE = {"runs": plan(tasks=4, conditions=("api-none",)), "max_usd": 10.0, "grants_usd": {"m1": 1.0}}
WIDE = {"runs": plan(tasks=6, conditions=("api-none",)), "max_usd": 20.0, "grants_usd": {"m1": 1.0}}


def test_c42_settling_lost_leases_releases_their_grants_and_a_priced_dispatch_they_blocked_goes_out(tmp_path):
    """C42: two federations die holding their grants, so the budget is full and a third waits; each settle replaces
    its lease's grant with the spend read from the dead federation, and once enough is released the third is leased."""
    ledger, clock = make(tmp_path, **PRICED)
    first, second = lose(ledger, clock, "fed-1"), lose(ledger, clock, "fed-2")
    join(ledger, clock, "fed-3")
    assert ledger.committed_micro() == (2_000_000, {"m1": 2_000_000})
    blocked = ledger.issue("fed-3")
    assert isinstance(blocked, Wait)
    assert "budget" in blocked.reason
    clock.advance(60)
    settlement = ledger.settle(first.lease_id, 250_000, "root")
    assert (settlement.lease_id, settlement.at, settlement.source) == (first.lease_id, clock(), "root")
    assert (settlement.spend_micro, settlement.grant_micro, settlement.released_micro) == (250_000, 1_000_000, 750_000)
    assert ledger.committed_micro() == (1_250_000, {"m1": 1_250_000})
    assert isinstance(ledger.issue("fed-3"), Wait)  # 0.75 USD left, and a grant is 1
    ledger.settle(second.lease_id, 400_000, "pulled")
    assert ledger.committed_micro() == (650_000, {"m1": 650_000})
    assert leased(ledger, "fed-3").grant_micro == 1_000_000
    assert ledger.committed_micro()[0] == 1_650_000
    assert [lease.charge_micro for lease in ledger.leases()[:2]] == [250_000, 400_000]
    assert_invariants(ledger)


def test_c42_a_settlement_is_recorded_with_its_time_and_source_and_survives_a_restart(tmp_path):
    """C42: the table keeps each settlement's lease, time, source, spend and grant, and the event log keeps the step."""
    ledger, clock = make(tmp_path, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    clock.advance(90)
    ledger.settle(lost.lease_id, 125_000, "pulled")
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    (kept,) = ledger.settlements()
    assert (kept.lease_id, kept.at, kept.source, kept.spend_micro, kept.grant_micro) == (
        lost.lease_id,
        clock(),
        "pulled",
        125_000,
        1_000_000,
    )
    event = next(e for e in ledger.events() if e["kind"] == "settle")
    assert (event["lease_id"], event["at"]) == (lost.lease_id, clock())
    assert event["detail"] == {"released_micro": 875_000, "source": "pulled", "spend_micro": 125_000}
    assert ledger.committed_micro()[0] == 125_000


def _refused(ledger, lease, match):
    """Settling the lease is refused with `match` in the reason, and the lease is as it was."""
    before = ledger.lease(lease.lease_id)
    with pytest.raises(SettleRefused, match=match):
        ledger.settle(lease.lease_id, 0, "root")
    assert ledger.lease(lease.lease_id) == before


def test_c42_a_lease_that_is_not_lost_is_not_settled(tmp_path):
    """C42: a lease in flight, one expired whose holder may still be spending, and any lease with an outcome are
    refused, and nothing changes: only a lease expired with its holder proven ended, or one whose archive never
    verified, is lost."""
    ledger, clock = make(tmp_path, **WIDE)
    for label in ("fed-1", "fed-2", "fed-3", "fed-4", "fed-5", "fed-6"):
        join(ledger, clock, label)
    issued, sent, heard = (leased(ledger, label) for label in ("fed-1", "fed-2", "fed-3"))
    ledger.mark_sent(sent.lease_id)
    ledger.observe_site(SITE, SiteReading(heartbeats={heard.lease_id: 1}))
    counted, returned, cancelled = (leased(ledger, label) for label in ("fed-4", "fed-5", "fed-6"))
    ledger.cancel(cancelled.lease_id)
    ledger.observe_site(SITE, SiteReading(returns=(Return(returned.lease_id, 0.0),)))
    assert finish(ledger, counted) == "counted"
    for lease, state in ((issued, "issued"), (sent, "sent"), (heard, "sent")):
        _refused(ledger, lease, state)
    for lease, state in ((counted, "counted"), (returned, "returned"), (cancelled, "cancelled")):
        _refused(ledger, lease, state)
    clock.advance(301)
    ledger.observe_site(SITE, SiteReading())
    for lease in (issued, sent, heard):
        assert ledger.lease(lease.lease_id).state == "expired"
        assert not ledger.lease(lease.lease_id).proven
        _refused(ledger, lease, "proven")  # its holder may still be spending
    assert ledger.settlements() == []
    assert not any(e["kind"] in ("settle", "cap_failed") for e in ledger.events())
    with pytest.raises(LedgerError, match="999"):
        ledger.settle(999, 0, "root")


def test_c42_a_lease_whose_job_was_cancelled_is_settled_only_once_killwait_has_passed(tmp_path):
    """C42: a cancelled job may run on until KillWait ends, still spending, so its lease is settled only after that
    (the grant stays held until then); a job seen ended, or past its walltime, is settled at once."""
    ledger, clock = make(tmp_path, **PRICED)
    join(ledger, clock, "fed-1")  # KillWait 180 s
    lease = leased(ledger, "fed-1")
    ledger.prove_ended("fed-1", "cancelled")
    clock.advance(179)
    _refused(ledger, lease, "KillWait")
    assert ledger.committed_micro()[0] == 1_000_000
    clock.advance(1)
    ledger.settle(lease.lease_id, 100_000, "root")
    assert ledger.committed_micro()[0] == 100_000


def test_c42_a_lease_whose_archive_never_verified_can_be_settled(tmp_path):
    """C42: a completion was read and its archive never arrived, so the lease is lost and charged its grant; its spend
    read elsewhere settles it."""
    ledger, clock = make(tmp_path, **PRICED)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(lease, spend_usd=0.4),)))
    assert ledger.verify_archive(lease.lease_id, None) == "lost"
    assert ledger.committed_micro()[0] == 1_000_000
    ledger.settle(lease.lease_id, 400_000, "pulled")
    assert ledger.committed_micro()[0] == 400_000
    assert ledger.lease(lease.lease_id).state == "lost"
    assert_invariants(ledger)


def test_c42_settling_twice_with_the_same_values_changes_nothing_and_with_other_values_is_refused(tmp_path):
    """C42: the same lease, spend and source again returns the first record (its time kept) and releases nothing more;
    any other spend or source is refused as already settled, and the charge stays what the first settle made it."""
    ledger, clock = make(tmp_path, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    first = ledger.settle(lost.lease_id, 300_000, "root")
    clock.advance(500)
    assert ledger.settle(lost.lease_id, 300_000, "root") == first
    assert len([e for e in ledger.events() if e["kind"] == "settle"]) == 1
    for spend, source in ((200_000, "root"), (300_000, "pulled"), (1_000_000, "root"), (400_000, "pulled")):
        with pytest.raises(SettleRefused, match="already settled"):
            ledger.settle(lost.lease_id, spend, source)
    assert ledger.settlements() == [first]
    assert ledger.lease(lost.lease_id).charge_micro == 300_000
    assert ledger.committed_micro()[0] == 300_000
    assert_invariants(ledger)


def test_c42_a_spend_above_the_grant_is_recorded_as_a_failed_cap_and_refused(tmp_path):
    """C42: a spend above the grant means the cap failed: it is refused, the lease stays charged its grant, and the
    failure is kept in the log, once for each distinct claim however often it is repeated, and across a restart."""
    ledger, clock = make(tmp_path, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    for _ in range(3):
        with pytest.raises(SettleRefused, match="cap"):
            ledger.settle(lost.lease_id, 1_000_001, "root")
    assert ledger.lease(lost.lease_id).charge_micro == 1_000_000
    assert ledger.settlements() == []
    (failed,) = [e for e in ledger.events() if e["kind"] == "cap_failed"]
    assert failed["lease_id"] == lost.lease_id
    assert failed["detail"] == {"grant_micro": 1_000_000, "source": "root", "spend_micro": 1_000_001}
    with pytest.raises(SettleRefused, match="cap"):
        ledger.settle(lost.lease_id, 1_500_000, "pulled")
    ledger.close()
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert len([e for e in ledger.events() if e["kind"] == "cap_failed"]) == 2
    assert ledger.committed_micro()[0] == 1_000_000
    settlement = ledger.settle(lost.lease_id, 1_000_000, "root")  # exactly the grant is within the cap
    assert settlement.released_micro == 0
    assert ledger.status().locked_micro == 0
    assert_invariants(ledger)


def test_c42_a_lost_lease_nobody_can_read_stays_charged_its_grant_across_a_restart(tmp_path):
    """C42: with no spend read for a lost lease its grant, the bound of what it can have spent, stays held."""
    ledger, clock = make(tmp_path, **PRICED)
    unread, read = lose(ledger, clock, "fed-1"), lose(ledger, clock, "fed-2")
    ledger.settle(read.lease_id, 100_000, "root")
    for _ in range(2):
        assert ledger.lease(unread.lease_id).charge_micro == 1_000_000
        assert ledger.committed_micro()[0] == 1_100_000
        assert ledger.status().locked_micro == 1_000_000
        ledger.close()
        ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert_invariants(ledger)


def test_c42_evidence_read_after_a_settle_never_raises_the_settled_charge_and_may_lower_it(tmp_path):
    """C42: a completion verified or a return read after a settle replaces the settled spend only with a lower one: a
    higher one would pass a budget the released money was leased out of, so the charge stays and the contradiction
    is logged. An archive that never verifies leaves the settled spend, and the same return read again changes
    nothing."""
    ledger, clock = make(tmp_path, **LOOSE)
    higher, lower, never, return_high, return_low = (lose(ledger, clock, f"fed-{n}") for n in range(1, 6))
    for lease in (higher, lower, never, return_high, return_low):
        ledger.settle(lease.lease_id, 300_000, "root")
    both = (completion(higher, spend_usd=0.5), completion(lower, spend_usd=0.1))
    ledger.observe_site(SITE, SiteReading(completions=both))
    assert ledger.verify_archive(higher.lease_id, archive(higher.lease_id)) == "superseded"
    assert ledger.lease(higher.lease_id).charge_micro == 300_000
    assert ledger.verify_archive(lower.lease_id, archive(lower.lease_id)) == "superseded"
    assert ledger.lease(lower.lease_id).charge_micro == 100_000
    ledger.observe_site(SITE, SiteReading(completions=(completion(never, spend_usd=0.3),)))
    assert ledger.verify_archive(never.lease_id, None) == "lost"
    assert ledger.lease(never.lease_id).charge_micro == 300_000
    assert ledger.settle(never.lease_id, 300_000, "root").spend_micro == 300_000
    for lease, spend, charge in ((return_high, 5.0, 300_000), (return_low, 0.2, 200_000)):
        for _ in range(2):  # the same return read twice
            ledger.observe_site(SITE, SiteReading(returns=(Return(lease.lease_id, spend),)))
            assert ledger.lease(lease.lease_id).charge_micro == charge
            assert ledger.lease(lease.lease_id).state == "returned"
    contradictions = [e["detail"] for e in ledger.events() if e["kind"] == "settle_contradicted"]
    assert sorted(c["measured_micro"] for c in contradictions) == [500_000, 5_000_000]
    assert all(c["settled_micro"] == 300_000 for c in contradictions)
    assert ledger.committed_micro()[0] == 300_000 + 100_000 + 300_000 + 300_000 + 200_000
    assert ledger.status().locked_micro == 0
    assert_invariants(ledger)


@pytest.mark.parametrize("spend", [-1, 1.5, True, "300", None])
def test_c42_a_spend_that_is_not_a_whole_number_of_micro_dollars_is_refused(tmp_path, spend):
    """C42: the spend is a count of micro-dollars, not below zero."""
    ledger, clock = make(tmp_path, **PRICED)
    lost = lose(ledger, clock, "fed-1")
    with pytest.raises(ValueError, match="spend_micro_usd"):
        ledger.settle(lost.lease_id, spend, "root")
    assert ledger.lease(lost.lease_id).charge_micro == 1_000_000


@pytest.mark.parametrize("lease_id", [True, 0, -1, "1", None, 1.0])
def test_c42_a_lease_id_that_is_not_a_whole_number_from_one_is_refused(tmp_path, lease_id):
    """C42: the fencing token is a whole number from 1; a bool is not lease 1, and releases nothing."""
    ledger, clock = make(tmp_path, **PRICED)
    lose(ledger, clock, "fed-1")
    with pytest.raises(ValueError, match="lease id"):
        ledger.settle(lease_id, 0, "root")
    assert ledger.committed_micro()[0] == 1_000_000


def test_c42_a_source_other_than_root_or_pulled_and_a_ledger_without_a_budget_are_refused(tmp_path):
    """C42: the evidence comes from the surviving root or pulled evidence; with no budget no lease holds a charge."""
    ledger, clock = make(tmp_path / "budget", **PRICED)
    lost = lose(ledger, clock, "fed-1")
    with pytest.raises(ValueError, match="source"):
        ledger.settle(lost.lease_id, 0, "guess")
    free, free_clock = make(tmp_path / "free")
    free_lost = lose(free, free_clock, "fed-1")
    with pytest.raises(SettleRefused, match="budget"):
        free.settle(free_lost.lease_id, 0, "root")
