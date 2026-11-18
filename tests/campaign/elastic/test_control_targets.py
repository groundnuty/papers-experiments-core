"""The elastic controller's targets (C43): how many federations each site should have, from the ledger's demand under
caps and budget, capped by the site's maximum, its free whole nodes, its inode quota (exact reservations for ups in
flight) and its declared billing allowance; adds go to the site with the most room."""

from control_support import HOUR, NOW, joined, ledger, member, settings, site

from expcore.campaign.elastic.control import Add, decide


def adds(decision) -> dict[str, int]:
    return {action.site: action.count for action in decision.actions if isinstance(action, Add)}


def test_c43_the_target_is_capped_by_demand_under_caps_and_counts_idle_federations():
    """C43: with 5 and 3 runs pending under caps of 2 each, 4 federations can work; two idle ones already up leave 2
    to add, and a model with no cap counts every pending run."""
    view = ledger(pending={"m1": 5, "m2": 3}, caps={"m1": 2, "m2": 2})
    assert adds(decide(NOW, view, [(settings(), site())])) == {"site-a": 4}
    fleet = site(members=[member("fed-1"), member("fed-2")])
    feds = [joined("fed-1"), joined("fed-2")]
    view = ledger(pending={"m1": 5, "m2": 3}, caps={"m1": 2, "m2": 2}, federations=feds)
    assert adds(decide(NOW, view, [(settings(), fleet)])) == {"site-a": 2}
    view = ledger(pending={"m1": 3}, caps={})
    assert adds(decide(NOW, view, [(settings(), site())])) == {"site-a": 3}


def test_c43_no_federation_comes_up_that_the_budget_could_never_lease():
    """C43: a model whose grant fits only twice in what max_usd leaves, and once in its own ceiling, asks for that many
    federations and no more."""
    view = ledger(pending={"m1": 5, "m2": 5}, grants={"m1": 10, "m2": 10}, left=25)
    assert adds(decide(NOW, view, [(settings(), site())])) == {"site-a": 2}
    view = ledger(pending={"m1": 5, "m2": 5}, grants={"m1": 10, "m2": 0}, left=100, left_per_model={"m1": 15})
    assert adds(decide(NOW, view, [(settings(), site())])) == {"site-a": 4}  # m1 once (ceiling), m2 free: 5 -> 4 a time


def test_c43_free_whole_nodes_cap_the_site_and_mixed_nodes_never_count():
    """C43: under --exclusive a federation takes 7 whole nodes, so only idle nodes are free: 20 idle nodes hold 2 more
    federations, an up in flight whose job still waits reserves its 7, and 6 idle nodes beside 300 mixed hold none."""
    view = ledger(pending={"m1": 9})
    assert adds(decide(NOW, view, [(settings(), site(idle_nodes=20))])) == {"site-a": 2}
    waiting = member("fed-1", state="PENDING", status="made", submitted_at=NOW - 30)
    assert adds(decide(NOW, view, [(settings(), site(idle_nodes=20, members=[waiting]))])) == {"site-a": 1}
    decision = decide(NOW, view, [(settings(), site(idle_nodes=6, mixed_nodes=300))])
    assert adds(decision) == {}
    assert decision.targets["site-a"]["limits"]["nodes"] == 0


def test_c43_inode_room_is_exact_with_a_reservation_for_each_up_in_flight():
    """C43: room is (95% line - used - reserved) // estimate: exactly k estimates fit k federations, one inode less fits
    k - 1, and each member not yet joined holds one whole estimate (a restart reads them from the fleet record)."""
    view = ledger(pending={"m1": 20})
    line = 950_000
    used = line - 3 * 17_650
    decision = decide(NOW, view, [(settings(), site(inodes_used=used))])
    assert decision.targets["site-a"]["limits"]["inodes"] == 3
    decision = decide(NOW, view, [(settings(), site(inodes_used=used + 1))])
    assert decision.targets["site-a"]["limits"]["inodes"] == 2
    coming = [member("fed-1", status="made", state=None), member("fed-2", state="RUNNING", verified=False)]
    decision = decide(NOW, view, [(settings(), site(inodes_used=used, members=coming))])
    assert decision.targets["site-a"]["limits"]["inodes"] == 1


def test_c43_quota_exhaustion_adds_nothing_at_that_site_and_moves_the_adds_elsewhere():
    """C43: a site whose usage is past its line adds nothing; the other site takes the demand. A site whose quota could
    not be read (on scratch) adds nothing either: unknown is never room."""
    view = ledger(pending={"m1": 3})
    full = site(inodes_used=949_999)
    decision = decide(NOW, view, [(settings(), full), (settings("site-b"), site("site-b"))])
    assert adds(decision) == {"site-b": 3}
    unread = site(inodes_used=None, inodes_quota=None, inodes_error="the quota command failed")
    assert adds(decide(NOW, view, [(settings(), unread)])) == {}
    off_scratch = site(inodes_used=None, inodes_quota=None, inode_estimate=0)
    assert adds(decide(NOW, view, [(settings(), off_scratch)])) == {"site-a": 3}
    no_estimate = site(inodes_used=950_001, inode_estimate=0)  # past the line: an estimate of 0 adds nothing
    assert adds(decide(NOW, view, [(settings(), no_estimate)])) == {}


def test_c43_the_allowance_must_pay_a_new_federations_whole_time():
    """C43: each federation made is charged its whole --time at the declared rate; a new one is admitted only while the
    allowance still pays its whole --time. No code reads a grant."""
    view = ledger(pending={"m1": 5})
    whole = 47.0 * 12  # one federation's 12 h at 47 units an hour
    made = [member("fed-1"), member("fed-2", status="down", state=None)]
    feds = [joined("fed-1")]
    view = ledger(pending={"m1": 5}, federations=feds)
    exact = settings(allowance_units=3 * whole)
    assert adds(decide(NOW, view, [(exact, site(members=made))])) == {"site-a": 1}
    short = settings(allowance_units=3 * whole - 0.01)
    decision = decide(NOW, view, [(short, site(members=made))])
    assert adds(decision) == {}
    assert decision.targets["site-a"]["limits"]["allowance"] == 0


def test_c43_the_sites_maximum_counts_every_member_that_may_hold_a_job():
    """C43: a failed member still holds its job until down, so it counts against the site's maximum."""
    view = ledger(pending={"m1": 9})
    members = [member("fed-1"), member("fed-2", status="failed", state="RUNNING", linked=False)]
    decision = decide(NOW, view, [(settings(max_federations=3), site(members=members))])
    assert decision.targets["site-a"]["limits"]["maximum"] == 1


def test_c43_adds_go_to_the_site_with_the_most_room_each_within_its_parallel_ups():
    """C43: five federations wanted, site-a with room for 2 and site-b for 7 bringing 4 up at a time: site-b takes 4,
    site-a 1."""
    view = ledger(pending={"m1": 5})
    sites = [(settings(max_federations=2), site()), (settings("site-b"), site("site-b"))]
    assert adds(decide(NOW, view, sites)) == {"site-a": 1, "site-b": 4}


def test_c43_a_stalled_queue_stops_adds_at_that_site_and_its_queue_waits_are_reported():
    """C43: an up whose job has waited past the site's stall bound stops further adds there (they would only queue
    behind it); the demand goes elsewhere, and each up's queue wait, submit to running, is reported per site."""
    view = ledger(pending={"m1": 2}, federations=[joined("fed-2")], holding={"site-a-fed-2": "sent"})
    stalled = member("fed-1", status="made", state="PENDING", submitted_at=NOW - 1000, started_at=None)
    done = member("fed-2", submitted_at=NOW - HOUR - 40, started_at=NOW - HOUR)
    sites = [(settings(queue_stall_s=900), site(members=[stalled, done])), (settings("site-b"), site("site-b"))]
    decision = decide(NOW, view, sites)
    assert adds(decision) == {"site-b": 1}  # 2 more leases wanted; fed-1 is on its way, so 1 more
    assert decision.targets["site-a"]["limits"]["stalled"] is True
    waits = {w["member"]: w for w in decision.waits["site-a"]}
    assert waits["fed-1"] == {"member": "fed-1", "queue_wait_s": 1000.0, "waiting": True}
    assert waits["fed-2"] == {"member": "fed-2", "queue_wait_s": 40.0, "waiting": False}


def test_c43_a_site_going_dark_gets_no_action_and_its_federations_are_replaced_elsewhere():
    """C43: a site that cannot be read is target 0 with no action, no proven end and no retirement there; its joined
    federations do not count as free, so the demand comes up at the other site."""
    feds = [joined("fed-1"), joined("fed-2")]
    view = ledger(pending={"m1": 2}, federations=feds)
    dark = site(error="the site could not be read", members=())
    decision = decide(NOW, view, [(settings(), dark), (settings("site-b"), site("site-b"))])
    assert [a for a in decision.actions if a.site == "site-a"] == []
    assert adds(decision) == {"site-b": 2}
    assert decision.targets["site-a"]["target"] == 0
    assert decision.targets["site-a"]["dark"] == "the site could not be read"


def test_c43_a_site_busy_with_an_add_gets_no_second_one():
    """C43: an add still running at a site (the fleet refuses a second beside it) means no new add there this tick."""
    view = ledger(pending={"m1": 3})
    decision = decide(NOW, view, [(settings(), site())], busy=frozenset({"site-a"}))
    assert adds(decision) == {}


def test_c43_an_add_is_refused_at_the_login_nodes_memory_cap():
    """C43: the login node caps the account's memory; with a 512 MB reserve, three relays beating there (40 MB each),
    an up in flight (256 MB) and a verify-data in flight (1024 MB), a new federation (its up, then its relay: 296 MB)
    fits only while that much is left under the cap; relays beating on another login node take none of it."""
    members = [member(f"fed-{n}", relay="here") for n in (1, 2, 3)]
    members += [member("fed-4", status="made", state="PENDING", submitted_at=NOW - 30), member("fed-5", verified=False)]
    members += [member("fed-6", relay="elsewhere")]
    used = 512 + 3 * 40 + 256 + 1024
    view = ledger(pending={"m1": 9})
    tight = decide(NOW, view, [(settings(login_memory_mb=used + 295.0), site(members=members))])
    assert adds(tight) == {}
    limits = tight.targets["site-a"]["limits"]
    assert (limits["login_memory"], limits["login_memory_mb"]["used_mb"]) == (0, used)
    fits = decide(NOW, view, [(settings(login_memory_mb=used + 296.0), site(members=members))])
    assert fits.targets["site-a"]["limits"]["login_memory"] == 1
    assert decide(NOW, view, [(settings(), site(members=members))]).targets["site-a"]["limits"]["login_memory"] is None


def test_c43_a_failed_member_still_holding_a_waiting_job_reserves_its_nodes():
    """C43: a failed up whose job still waits (its cancel failed) may still be given its 7 nodes: they stay reserved."""
    view = ledger(pending={"m1": 3})
    failed = member("fed-1", status="failed", state="PENDING", linked=False, verified=False, submitted_at=NOW - 60)
    decision = decide(NOW, view, [(settings(), site(idle_nodes=14, members=[failed]))])
    assert decision.targets["site-a"]["limits"]["nodes"] == 1


def test_c43_sites_on_one_login_node_share_its_memory_budget():
    """C43: two fleets whose commands run on one login node share its cgroup: with room there for one more federation,
    the two sites together get one add, not one each; a site there that could not be read leaves none."""
    view = ledger(pending={"m1": 6})
    cap = 512 + 296.0
    a, b = settings(login_memory_mb=cap, login_node="ln-1"), settings("site-b", login_memory_mb=cap, login_node="ln-1")
    decision = decide(NOW, view, [(a, site()), (b, site("site-b"))])
    assert sum(action.count for action in decision.actions if isinstance(action, Add)) == 1
    dark = decide(NOW, view, [(a, site()), (b, site("site-b", error="unread"))])
    assert [action for action in dark.actions if isinstance(action, Add)] == []
