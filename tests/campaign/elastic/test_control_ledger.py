"""What the elastic controller reads of a ledger (C45): the demand, which lease holds each federation's slot, the
budget left, since when each federation is idle; and a dry run's read-only view, which takes no lock and writes
nothing."""

from elastic_support import finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, SiteReading
from expcore.campaign.elastic.control import ledger_view, read_only


def test_c45_the_ledger_view_gives_slots_held_leases_in_flight_budget_left_and_idle_since(tmp_path):
    """C45: one lease in flight and one completed: the first federation holds a sent lease, the second is idle since its
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


def test_c45_a_dry_runs_read_only_view_reads_beside_the_driver_and_never_writes(tmp_path):
    """C45: while the driving process holds the ledger's lock, the read-only view reads the same demand and slots, and
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
