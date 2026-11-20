"""C62: a retirement's down takes its fleet's lifecycle lock, which an add holds through its whole up (queue wait
included), so a site with a retirement planned this tick or still running plans no add: the down gets the lock. Adds
resume there once no retirement is pending; a join there and every other site are untouched."""

from control_support import NOW, joined, ledger, member, settings, site

from expcore.campaign.elastic.control import Add, Join, Retire, decide


def of(decision, kind) -> list:
    return [action for action in decision.actions if isinstance(action, kind)]


def adds(decision) -> dict[str, int]:
    return {action.site: action.count for action in of(decision, Add)}


def drained_fleet():
    """Site A: fed-1 drained with no lease in flight (its retirement is planned) and demand for more."""
    view = ledger(pending={"m1": 3}, federations=[joined("fed-1", drained_at=NOW - 60)])
    return view, site(members=[member("fed-1")])


def test_c62_a_retirement_planned_at_a_site_holds_its_adds_that_tick():
    """C62: fed-1 is retired this tick and the site plans no add (its room is 0, the limit names fed-1); without the
    retirement the same demand adds there."""
    view, fleet = drained_fleet()
    decision = decide(NOW, view, [(settings(), fleet)])
    assert [action.member for action in of(decision, Retire)] == ["fed-1"]
    assert adds(decision) == {}
    assert decision.targets["site-a"]["limits"]["retiring"] == ["fed-1"]
    assert decision.targets["site-a"]["room"] == 0
    assert adds(decide(NOW, ledger(pending={"m1": 3}), [(settings(), site())])) == {"site-a": 3}


def test_c62_a_retirement_still_running_holds_the_adds_and_a_prepare_does_not():
    """C62: a retirement the executor still runs (its busy key `site-a/fed-2`) holds the site's adds; a prepare in
    flight there (`prepare:site-a/fed-2`) does not."""
    fleet = site(members=[member("fed-2")])
    view = ledger(pending={"m1": 3})
    held = decide(NOW, view, [(settings(), fleet)], frozenset({"site-a/fed-2"}))
    assert adds(held) == {}
    assert held.targets["site-a"]["limits"]["retiring"] == ["fed-2"]
    preparing = decide(NOW, view, [(settings(), fleet)], frozenset({"prepare:site-a/fed-2"}))
    assert adds(preparing) == {"site-a": 2}  # fed-2, its data on its way, counts as one of the three
    assert preparing.targets["site-a"]["limits"]["retiring"] == []


def test_c62_adds_resume_once_the_retirement_has_ended_with_its_down():
    """C62: the tick after fed-1's down is verified (the fleet's record says down, the end is proven), nothing is
    pending at the site and the add comes back; so a down refused for a while never holds the adds for ever."""
    gone = joined("fed-1", drained_at=NOW - 600, ended_at=NOW - 60, end_proof="cancelled")
    view = ledger(pending={"m1": 3}, federations=[gone])
    decision = decide(NOW, view, [(settings(), site(members=[member("fed-1", status="down", state=None)]))])
    assert of(decision, Retire) == []
    assert adds(decision) == {"site-a": 3}
    assert decision.targets["site-a"]["limits"]["retiring"] == []


def test_c62_a_failed_up_being_retired_holds_its_sites_adds():
    """C62: an up that failed (never joined) is retired with no collect; the add waits for its down there too."""
    fleet = site(members=[member("fed-3", status="failed", state="RUNNING")])
    decision = decide(NOW, ledger(pending={"m1": 3}), [(settings(), fleet)])
    assert [action.member for action in of(decision, Retire)] == ["fed-3"]
    assert adds(decision) == {}


def test_c62_another_site_still_adds_and_a_join_at_the_retiring_site_is_not_delayed():
    """C62: site A retires fed-1 and has fed-4 ready to join: fed-4 joins this tick, and site B, with no retirement,
    takes the add."""
    view, _ = drained_fleet()
    fleet = site(members=[member("fed-1"), member("fed-4")])
    decision = decide(NOW, view, [(settings(), fleet), (settings("site-b"), site("site-b"))])
    assert [action.member for action in of(decision, Retire)] == ["fed-1"]
    assert [action.member for action in of(decision, Join)] == ["fed-4"]
    assert "site-a" not in adds(decision)
    assert adds(decision).get("site-b", 0) > 0
