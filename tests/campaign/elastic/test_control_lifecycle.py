"""The elastic controller's federation lifecycle (C43): join once up and verified, drain before the recorded job end,
retire what is drained or idle with no cap to let it work (never with a lease in flight), prove an end only from what
a read site shows, and replace what ended or died."""

from control_support import HOUR, NOW, joined, ledger, member, settings, site

from expcore.campaign.elastic.control import Add, Drain, Join, ProveEnded, Retire, Settle, decide

CUTOFF = 1800.0 + 60.0 + 180.0 + 300.0  # the lease bound, collect, KillWait and the margin


def of(decision, kind) -> list:
    return [action for action in decision.actions if isinstance(action, kind)]


def test_c43_a_federation_up_linked_and_verified_joins_with_its_earliest_job_end_and_queue_wait():
    """C43: a member whose up finished, whose ingested data is verified and whose jobs run is joined under the site's
    label, with the earlier end of its held jobs and its queue wait; an unverified one waits and joins nothing."""
    ready = member("fed-3", jobs=("41", "42"), ends_at=NOW + 5 * HOUR)
    waiting = member("fed-4", verified=False)
    decision = decide(NOW, ledger(pending={"m1": 1}), [(settings(), site(members=[ready, waiting]))])
    (join,) = of(decision, Join)
    assert (join.site, join.member, join.label) == ("site-a", "fed-3", "site-a-fed-3")
    assert join.job_ids == ("41", "42")
    assert join.job_end_at == NOW + 5 * HOUR
    assert join.kill_wait_s == 180.0
    assert join.queue_wait_s == 40.0


def test_c43_a_federation_too_close_to_its_end_is_never_joined_and_is_taken_down():
    """C43: a member whose end is already inside the drain cut-off would never get a lease: it is retired, not
    joined, and it was never in the ledger."""
    late = member("fed-3", ends_at=NOW + CUTOFF - 1)
    decision = decide(NOW, ledger(pending={"m1": 1}), [(settings(), site(members=[late]))])
    assert of(decision, Join) == []
    (retire,) = of(decision, Retire)
    assert (retire.member, retire.label, retire.collect) == ("fed-3", None, True)


def test_c43_drain_comes_before_the_recorded_job_end_and_never_after_it():
    """C43: a federation is drained once its end is closer than the lease bound, collect, KillWait and 5 minutes; not
    before, and never once its end has passed (its job is gone or going: that is proven, not drained)."""
    for end_in, drained in ((CUTOFF + 1, False), (CUTOFF, True), (60.0, True), (0.0, False), (-30.0, False)):
        fleet = site(members=[member("fed-1", ends_at=NOW + end_in)])
        fed = joined("fed-1", end_in_s=end_in)
        view = ledger(pending={"m1": 1}, federations=[fed], holding={"site-a-fed-1": "sent"})
        found = of(decide(NOW, view, [(settings(), fleet)]), Drain)
        assert bool(found) is drained, end_in


def test_c43_the_drain_bound_defaults_to_the_ledgers_hard_term_which_fails_closed():
    """C43: with no declared lease bound, the drain reads the ledger's hard term (7200 s here), the longest a lease may
    hold, so no lease is issued that the walltime could cut."""
    cutoff = 7200.0 + 60.0 + 180.0 + 300.0
    fleet = site(members=[member("fed-1", ends_at=NOW + cutoff)])
    view = ledger(pending={"m1": 1}, federations=[joined("fed-1", end_in_s=cutoff)])
    assert of(decide(NOW, view, [(settings(lease_bound_s=None), fleet)]), Drain) != []


def test_c43_a_drained_federation_is_retired_only_with_no_lease_in_flight():
    """C43: drained and holding a lease that is issued or sent, it stays; with nothing held, or only an expired lease
    whose run went back (its holder is cancelled by the down, which proves its end), it is retired with its evidence
    collected first."""
    fleet = site(members=[member("fed-1")])
    drained = joined("fed-1", drained_at=NOW - 60)
    for held, retired in (("issued", False), ("sent", False), ("expired", True), (None, True)):
        holding = {} if held is None else {"site-a-fed-1": held}
        view = ledger(pending={"m1": 1}, federations=[drained], holding=holding)
        found = of(decide(NOW, view, [(settings(), fleet)]), Retire)
        assert bool(found) is retired, held
        if found:
            assert (found[0].label, found[0].collect) == ("site-a-fed-1", True)


def test_c43_a_federation_whose_lease_went_stale_is_drained_then_retired_and_replaced():
    """C43: a lease that expired on a running job (a stale heartbeat or its hard term) leaves its federation
    suspect: it is drained, then taken down (the cancel proves its end), and a replacement comes up."""
    fleet = site(members=[member("fed-1")])
    holding = {"site-a-fed-1": "expired"}
    view = ledger(pending={"m1": 1}, slots={"m1": 1}, federations=[joined("fed-1")], holding=holding)
    decision = decide(NOW, view, [(settings(), fleet)])
    assert [d.label for d in of(decision, Drain)] == ["site-a-fed-1"]
    assert {a.site: a.count for a in of(decision, Add)} == {"site-a": 1}


def test_c43_a_node_failure_or_a_walltime_end_is_proven_from_the_site_read_then_collected_and_replaced():
    """C43: a joined federation whose job the site shows ended (a node failure, its walltime) is proven ended as
    terminal; once the ledger shows it ended it is collected and taken down; either way a replacement comes up."""
    for state in ("NODE_FAIL", "TIMEOUT", "GONE"):
        fleet = site(members=[member("fed-1", state=state)])
        view = ledger(pending={"m1": 1}, federations=[joined("fed-1")])
        decision = decide(NOW, view, [(settings(), fleet)])
        assert [(p.label, p.proof) for p in of(decision, ProveEnded)] == [("site-a-fed-1", "terminal")], state
        assert of(decision, Retire) == []
        assert {a.site: a.count for a in of(decision, Add)} == {"site-a": 1}
        ended = joined("fed-1", ended_at=NOW - 5, end_proof="terminal")
        decision = decide(NOW, ledger(pending={"m1": 1}, federations=[ended]), [(settings(), fleet)])
        (retire,) = of(decision, Retire)
        assert (retire.label, retire.collect) == ("site-a-fed-1", True)


def test_c43_a_down_verified_by_the_fleet_is_proven_cancelled_and_nothing_else_is():
    """C43: the end is proven `cancelled` only once the fleet's record shows the member down (its down verified the job
    gone and the root removed); a member still up proves nothing."""
    view = ledger(federations=[joined("fed-1", drained_at=NOW - 600), joined("fed-2")])
    fleet = site(members=[member("fed-1", status="down", state=None), member("fed-2")])
    decision = decide(NOW, view, [(settings(), fleet)])
    assert [(p.label, p.proof) for p in of(decision, ProveEnded)] == [("site-a-fed-1", "cancelled")]


def test_c43_when_every_model_is_at_its_cap_idle_federations_retire_after_their_re_up_cost():
    """C43: no cap lets more work, so federations idle past the re-up cost are drained, the least walltime left first,
    keeping a declared warm pool; one idle for less than that is kept."""
    feds = [joined("fed-1", end_in_s=9 * HOUR), joined("fed-2", end_in_s=3 * HOUR), joined("fed-3", end_in_s=6 * HOUR)]
    fleet = site(members=[member("fed-1"), member("fed-2"), member("fed-3")])
    idle = {f.label: NOW - 1000 for f in feds}
    view = ledger(pending={"m1": 4}, caps={"m1": 1}, slots={"m1": 1}, federations=feds, idle_since=idle)
    decision = decide(NOW, view, [(settings(), fleet)])
    assert sorted(d.label for d in of(decision, Drain)) == ["site-a-fed-1", "site-a-fed-2", "site-a-fed-3"]
    decision = decide(NOW, view, [(settings(warm_pool=1), fleet)])
    assert [d.label for d in of(decision, Drain)] == ["site-a-fed-2", "site-a-fed-3"]
    fresh = {f.label: NOW - 100 for f in feds}
    view = ledger(pending={"m1": 4}, caps={"m1": 1}, slots={"m1": 1}, federations=feds, idle_since=fresh)
    assert of(decide(NOW, view, [(settings(), fleet)]), Drain) == []


def test_c43_a_failed_up_is_taken_down_without_a_collect_and_a_down_failed_one_again():
    """C43: a failed up may still hold a job: it is taken down (its root holds nothing a run left; down's own gate
    refuses one that does); a member whose down failed is taken down again."""
    failed = member("fed-1", status="failed", state="RUNNING", linked=False, verified=False)
    again = member("fed-2", status="down-failed", state=None, linked=False, verified=False)
    decision = decide(NOW, ledger(), [(settings(), site(members=[failed, again]))])
    assert sorted((r.member, r.collect) for r in of(decision, Retire)) == [("fed-1", False), ("fed-2", False)]


def test_c43_a_member_busy_retiring_is_not_retired_twice():
    """C43: a retire still running (its collect or down) is not started again on the next tick."""
    drained = joined("fed-1", drained_at=NOW - 60)
    view = ledger(federations=[drained])
    fleet = site(members=[member("fed-1")])
    assert of(decide(NOW, view, [(settings(), fleet)], busy=frozenset({"site-a/fed-1"})), Retire) == []


def test_c43_every_action_names_its_reason_and_the_order_proves_and_drains_before_it_adds():
    """C43: the decision lists proven ends and drains before joins, retirements and adds, each with a reason."""
    fleet = site(members=[member("fed-1", state="NODE_FAIL"), member("fed-2", ends_at=NOW + 60)])
    feds = [joined("fed-1"), joined("fed-2", end_in_s=60)]
    view = ledger(pending={"m1": 2}, federations=feds, holding={"site-a-fed-2": "sent"})
    decision = decide(NOW, view, [(settings(), fleet)])
    kinds = [type(action).__name__ for action in decision.actions]
    assert kinds == ["ProveEnded", "Drain", "Add"]
    assert all(action.reason for action in decision.actions)


def test_c43_a_dead_relay_is_recorded_with_its_last_heartbeat_and_its_federation_drained():
    """C43: a relay its up recorded serving and that no longer beats is recorded as gone with no exit status and its
    last heartbeat, and its federation drained (no outage can be injected there); one not yet joined is taken down."""
    joined_gone = member("fed-1", relay="gone", relay_beat_at=NOW - 300)
    fresh_gone = member("fed-2", relay="gone", relay_beat_at=None)
    fleet = site(members=[joined_gone, fresh_gone, member("fed-3", relay="here")])
    view = ledger(pending={"m1": 1}, federations=[joined("fed-1"), joined("fed-3")])
    decision = decide(NOW, view, [(settings(), fleet)])
    (drain,) = of(decision, Drain)
    assert drain.label == "site-a-fed-1"
    assert drain.reason.startswith("relay gone, no exit status (its last heartbeat at")
    assert [(r.member, r.label) for r in of(decision, Retire)] == [("fed-2", None)]
    assert decision.relays["site-a"] == [
        {"member": "fed-1", "relay": "gone, no exit status", "last_beat_at": NOW - 300},
        {"member": "fed-2", "relay": "gone, no exit status", "last_beat_at": None},
    ]


def test_c43_every_lost_lease_not_yet_settled_is_settled_but_one_whose_cancelled_job_may_still_spend():
    """C43: a lease expired with its holder proven ended, or lost, gets a Settle naming its federation's root while
    the site shows it; one whose job was cancelled less than KillWait ago waits, as the ledger would refuse it."""
    ended = joined("fed-1", ended_at=NOW - 10, end_proof="terminal")
    cancelled = joined("fed-2", ended_at=NOW - 10, end_proof="cancelled")
    long_cancelled = joined("fed-3", ended_at=NOW - 200, end_proof="cancelled")
    lost = [(11, "site-a-fed-1", "expired"), (12, "site-a-fed-2", "expired"), (13, "site-a-fed-3", "expired"),
            (14, "site-a-fed-1", "lost")]  # fmt: skip
    fleet = site(members=[member("fed-1", state="NODE_FAIL"), member("fed-2", status="down", state=None)])
    view = ledger(federations=[ended, cancelled, long_cancelled], lost=lost, left=10)
    assert of(decide(NOW, ledger(federations=[ended], lost=lost), [(settings(), fleet)]), Settle) == []  # no budget
    settles = of(decide(NOW, view, [(settings(), fleet)]), Settle)
    assert [(a.lease_id, a.member, a.root) for a in settles] == [
        (11, "fed-1", "/fleet/fed-1/root"),
        (13, "fed-3", None),
        (14, "fed-1", "/fleet/fed-1/root"),
    ]
    assert all(a.reason for a in settles)


def test_c43_no_path_drains_a_federation_at_or_past_its_recorded_end():
    """C43: past its recorded end (Slurm may still show the job running), a federation whose lease expired, whose fleet
    status is not up, or whose relay is gone is not drained, and the idle surplus never picks it: it is proven ended,
    never drained."""
    for changes, holding in (({}, "expired"), ({"status": "down-failed"}, None), ({"relay": "gone"}, None)):
        fleet = site(members=[member("fed-1", **changes)])
        hold = {} if holding is None else {"site-a-fed-1": holding}
        view = ledger(pending={"m1": 1}, federations=[joined("fed-1", end_in_s=-30.0)], holding=hold)
        assert of(decide(NOW, view, [(settings(), fleet)]), Drain) == [], changes
    feds = [joined("fed-1", end_in_s=-30.0), joined("fed-2", end_in_s=3 * HOUR)]
    fleet = site(members=[member("fed-1"), member("fed-2")])
    view = ledger(federations=feds, idle_since={f.label: NOW - 2000 for f in feds})
    assert [d.label for d in of(decide(NOW, view, [(settings(), fleet)]), Drain)] == ["site-a-fed-2"]


# ------------------------------------------------------------------------------------------------ review round 2
def test_c43_a_member_with_one_job_ended_beside_one_running_is_not_proven_ended_but_drained():
    """C43: services ended while the agent's job still runs (and may spend): not proven ended, never settled; drained
    and then taken down, its down proving the end; one not joined is taken down, never joined."""
    view = ledger(pending={"m1": 1}, federations=[joined("fed-1")], holding={"site-a-fed-1": "sent"})
    decision = decide(NOW, view, [(settings(), site(members=[member("fed-1", state="BROKEN")]))])
    assert of(decision, ProveEnded) == []
    assert [d.label for d in of(decision, Drain)] == ["site-a-fed-1"]
    decision = decide(NOW, ledger(pending={"m1": 1}), [(settings(), site(members=[member("fed-2", state="BROKEN")]))])
    assert of(decision, Join) == []
    assert [r.member for r in of(decision, Retire)] == ["fed-2"]


def test_c43_a_relay_gone_before_the_data_is_verified_takes_the_member_down():
    """C43: a linked member whose relay died while its ingest is still unverified is not left up for ever."""
    fleet = site(members=[member("fed-1", relay="gone", verified=False)])
    assert [r.member for r in of(decide(NOW, ledger(pending={"m1": 1}), [(settings(), fleet)]), Retire)] == ["fed-1"]


def test_c43_a_settle_names_a_root_only_of_the_member_holding_the_jobs_the_federation_joined_with():
    """C43: a site's fleet label now names another federation (other jobs): its root is not the lost lease's, so the
    Settle names none and the reader must find the dead federation's evidence elsewhere."""
    ended = joined("fed-1", ended_at=NOW - 10, end_proof="terminal")
    view = ledger(federations=[ended], lost=[(11, "site-a-fed-1", "expired")], left=10)
    other = member("fed-1", jobs=("j-replacement",))
    (settle,) = of(decide(NOW, view, [(settings(), site(members=[other]))]), Settle)
    assert settle.root is None


def test_c43_idle_federations_are_kept_for_exhausted_runs_a_pending_settle_may_reopen():
    """C43: two runs exhausted by a loss's conservative charge, their lost leases not yet settled, may come back: of
    three idle federations no cap lets work, only one is drained until they are settled."""
    feds = [joined(f"fed-{n}", end_in_s=(4 + n) * HOUR) for n in (1, 2, 3)]
    fleet = site(members=[member(f"fed-{n}") for n in (1, 2, 3)])
    idle = {f.label: NOW - 2000 for f in feds}
    view = ledger(federations=feds, idle_since=idle, reopenable=2)
    assert [d.label for d in of(decide(NOW, view, [(settings(), fleet)]), Drain)] == ["site-a-fed-1"]
