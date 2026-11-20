"""C58: a run whose consecutive infrastructure attempts, across its leases, reach `max_infra_streak` is lost by them.

The ledger is driven on a fake clock (`elastic_support`). A completion says the streak its store's attempts ended with
(`infra_streak`, and the last attempt's kind); every completion read adds it, whatever its verdict, and one whose last
attempt was done breaks it. A pending run whose streak reaches the bound becomes `infra_streak`, is never leased again,
and a late completion of its latest lease is superseded.
"""

import json

from elastic_support import (
    HARD_TERM_S,
    SITE,
    FakeClock,
    archive,
    assert_invariants,
    completion,
    config,
    finish,
    join,
    leased,
    make,
    plan,
)

from expcore.campaign.elastic import Ledger, LedgerConfig, SiteReading, Wait
from expcore.campaign.elastic.__main__ import format_status
from expcore.campaign.elastic.streaks import STREAK_ENDED

ONE = {"runs": plan(tasks=1, conditions=("api-none",))}
BOUND = {**ONE, "max_infra_streak": 3}


def infra(kind: str, charged: int = 0, streak: int = 1) -> dict:
    """What a lease store whose attempts ended as infrastructure reports: `streak` of them in a row, the last `kind`."""
    return {"state": "infra_fail", "charged": charged, "infra_streak": streak, "infra_kind": kind}


def streak_events(ledger) -> list[dict]:
    return [event for event in ledger.events() if event["kind"] == STREAK_ENDED]


def no_run_pending(ledger, clock, label: str) -> None:
    join(ledger, clock, label)
    waiting = ledger.issue(label)
    assert isinstance(waiting, Wait)
    assert "no run is pending" in waiting.reason


def test_c58_three_uncharged_infrastructure_attempts_across_leases_lose_the_run_and_it_is_never_leased_again(tmp_path):
    """C58: a kind that costs no attempt (a hold) never reaches `max_attempts`, so without the bound the run is leased
    for ever; with it the third attempt in a row loses the run, its charged attempts still 0, with the event."""
    ledger, clock = make(tmp_path, **BOUND)
    join(ledger, clock, "fed-1")
    for n in range(3):
        lease = leased(ledger, "fed-1")
        assert finish(ledger, lease, **infra("budget_hold")) == "counted"
        run = ledger.run(lease.run_key)
        assert run.state == ("pending" if n < 2 else STREAK_ENDED)
        assert run.charged == 0
    no_run_pending(ledger, clock, "fed-2")
    (event,) = streak_events(ledger)
    assert (event["run_key"], event["lease_id"]) == (lease.run_key, lease.lease_id)
    assert event["detail"]["streak"] == 3
    assert event["detail"]["infra_kind"] == "budget_hold"
    assert event["detail"]["max_infra_streak"] == 3
    assert_invariants(ledger)


def test_c58_charged_infrastructure_counts_too_and_ends_the_run_before_max_attempts(tmp_path):
    """C58: a charged kind is part of the streak as well: three in a row lose the run though `max_attempts` is 5."""
    ledger, clock = make(tmp_path, **BOUND, max_attempts=5)
    join(ledger, clock, "fed-1")
    for _ in range(3):
        lease = leased(ledger, "fed-1")
        finish(ledger, lease, **infra("cell_unkillable", charged=1))
    run = ledger.run(lease.run_key)
    assert (run.state, run.charged) == (STREAK_ENDED, 3)
    assert streak_events(ledger)[0]["detail"]["infra_kind"] == "cell_unkillable"
    no_run_pending(ledger, clock, "fed-2")
    assert_invariants(ledger)


def test_c58_one_lease_whose_store_ended_on_the_streak_loses_the_run_at_once(tmp_path):
    """C58: a lease store stops its run once the streak reaches the bound (the store's own rule) and says so; the
    ledger loses the run on that one completion."""
    ledger, clock = make(tmp_path, **BOUND)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    finish(ledger, lease, **infra("budget_hold", streak=3))
    assert ledger.run(lease.run_key).state == STREAK_ENDED
    no_run_pending(ledger, clock, "fed-2")


def test_c58_superseded_completions_count_and_a_late_completion_of_the_lost_runs_lease_is_superseded(tmp_path):
    """C58: the live loop: each lease outlives its hard term, so its run is leased again and its store's completion,
    read later, is superseded, and its charged attempts never reach the run's. The streak counts it; the run, leased
    again meanwhile, is lost once that lease comes back too, and its completion read after is superseded."""
    ledger, clock = make(tmp_path, **BOUND)
    join(ledger, clock, "fed-1")
    first = leased(ledger, "fed-1")
    clock.advance(HARD_TERM_S + 1)
    ledger.observe_site(SITE, SiteReading(heartbeats={first.lease_id: 1}))  # past its hard term: it expires
    join(ledger, clock, "fed-2")
    second = leased(ledger, "fed-2")
    late = completion(first, state="infra_fail", charged=3, infra_streak=3, infra_kind="cell_unkillable")
    ledger.observe_site(SITE, SiteReading(completions=(late,)))
    assert ledger.verify_archive(first.lease_id, archive(first.lease_id)) == "superseded"
    assert ledger.run(first.run_key).charged == 0  # a superseded lease's attempts never reach the run's
    assert ledger.run(first.run_key).state == "leased"  # its lease out runs on
    clock.advance(HARD_TERM_S + 1)
    ledger.observe_site(SITE, SiteReading(heartbeats={second.lease_id: 1}))  # it expires: back, and lost at once
    assert ledger.run(first.run_key).state == STREAK_ENDED
    no_run_pending(ledger, clock, "fed-3")
    assert finish(ledger, second, **infra("cell_unkillable", charged=3, streak=3)) == "superseded"
    assert ledger.run(first.run_key).state == STREAK_ENDED
    assert_invariants(ledger)


def test_c58_a_done_attempt_in_between_breaks_the_streak(tmp_path):
    """C58: a lease whose store ended done (here its archive never verified, so its run is pending again) breaks the
    streak: two infrastructure attempts before it and two after are not three in a row; a third after is."""
    ledger, clock = make(tmp_path, **BOUND)
    join(ledger, clock, "fed-1")
    before = leased(ledger, "fed-1")
    finish(ledger, before, **infra("budget_hold", streak=2))
    done = leased(ledger, "fed-1")
    ledger.observe_site(SITE, SiteReading(completions=(completion(done, state="done", charged=0, infra_streak=0),)))
    assert ledger.verify_archive(done.lease_id, None) == "lost"
    after = leased(ledger, "fed-1")
    finish(ledger, after, **infra("budget_hold", streak=2))
    assert ledger.run(after.run_key).state == "pending"
    last = leased(ledger, "fed-1")
    finish(ledger, last, **infra("budget_hold"))
    assert ledger.run(last.run_key).state == STREAK_ENDED
    assert streak_events(ledger)[0]["detail"]["streak"] == 3
    assert_invariants(ledger)


def test_c58_unset_a_long_uncharged_loop_keeps_leasing_the_run_as_today(tmp_path):
    """C58: with no bound the streak is never read: ten uncharged infrastructure completions leave the run pending and
    leased again, as before the setting, whatever the completions say of their streak."""
    ledger, clock = make(tmp_path, **ONE)
    join(ledger, clock, "fed-1")
    for _ in range(10):
        lease = leased(ledger, "fed-1")
        assert finish(ledger, lease, **infra("budget_hold", streak=5)) == "counted"
        assert ledger.run(lease.run_key).state == "pending"
    assert streak_events(ledger) == []
    assert ledger.status().streak_ended == ()
    assert_invariants(ledger)


def test_c58_the_configurations_text_is_the_one_of_before_and_the_bound_has_a_meta_row_of_its_own(tmp_path):
    """C58: the bound is never in the configuration's text, so a core without the field still parses a bounded
    ledger's (a controller reading a sibling's to defer to it, C53, would otherwise read it as unreadable and add
    nothing); it is pinned in its own meta row, read back when the ledger is opened again, and absent when unset."""
    unset, bound = config(), config(max_infra_streak=3)
    assert bound.to_json() == unset.to_json()
    assert "max_infra_streak" not in json.loads(bound.to_json())
    assert unset.meta_rows() == {"config": unset.to_json()}
    assert bound.meta_rows() == {"config": unset.to_json(), "max_infra_streak": "3"}
    assert LedgerConfig.from_json(unset.to_json()).max_infra_streak is None
    assert LedgerConfig.from_json(bound.to_json(), "3") == bound
    ledger, _ = make(tmp_path, **BOUND)
    path = ledger.path
    ledger.close()
    reopened = Ledger.open(path, clock=FakeClock())
    assert reopened.config.max_infra_streak == 3
    assert reopened._meta("config") == unset.to_json()
    reopened.close()


def test_c58_status_shows_each_run_lost_by_its_streak(tmp_path):
    """C58: `status` names the run, the streak, its last kind, the bound and the lease; nothing while none is lost."""
    ledger, clock = make(tmp_path, **BOUND)
    join(ledger, clock, "fed-1")
    lease = leased(ledger, "fed-1")
    assert "infrastructure streak" not in format_status(ledger.status())
    finish(ledger, lease, **infra("budget_hold", streak=3))
    (ended,) = ledger.status().streak_ended
    assert (ended.run_key, ended.lease_id, ended.streak, ended.infra_kind) == (
        lease.run_key,
        lease.lease_id,
        3,
        "budget_hold",
    )
    shown = format_status(ledger.status())
    assert f"run {lease.run_key}: lost by its infrastructure streak" in shown
    assert "3 consecutive infrastructure attempts, the last of kind budget_hold, reached max_infra_streak 3" in shown
