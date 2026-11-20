"""C56: `node_room = "queue"` lets a non-exclusive site's room ignore its node counts (a federation's jobs start on
mixed nodes, so no count of idle nodes is a capacity), with one waiting up at a time as the guard; `"idle"`, the
default, is C43's free whole nodes."""

import pytest
from control_support import NOW, ledger, member, settings, site

from expcore.campaign.elastic.control import Add, SiteSettings, decide


def adds(decision) -> dict[str, int]:
    return {action.site: action.count for action in decision.actions if isinstance(action, Add)}


def limits_of(decision) -> dict:
    return decision.targets["site-a"]["limits"]


def waiting(label: str = "fed-1", age_s: float = 30.0):
    """An up in flight whose job still waits in the queue, submitted `age_s` ago."""
    return member(label, status="made", state="PENDING", submitted_at=NOW - age_s, started_at=None)


def test_c56_the_default_counts_idle_nodes_and_the_limits_name_the_mode():
    """C56: with no `node_room` the site is as before (0 idle beside 300 mixed holds no federation), and the limits
    line says which mode is in force."""
    assert SiteSettings("site-a", 7, 1e12, 47.0).node_room == "idle"
    decision = decide(NOW, ledger(pending={"m1": 9}), [(settings(), site(idle_nodes=0, mixed_nodes=300))])
    assert adds(decision) == {}
    assert (limits_of(decision)["node_room"], limits_of(decision)["nodes"]) == ("idle", 0)
    assert limits_of(decision)["queue_slot"] is None


def test_c56_queue_with_no_idle_node_gets_its_room_from_the_other_limits():
    """C56: 0 idle nodes beside 300 mixed: "queue" does not bind on nodes, so the site adds one federation (one waiting
    up at a time caps a tick at 1 even with 4 parallel ups); the node figures are still reported."""
    decision = decide(
        NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue"), site(idle_nodes=0, mixed_nodes=300))]
    )
    assert adds(decision) == {"site-a": 1}
    limits = limits_of(decision)
    assert (limits["node_room"], limits["nodes"], limits["queue_slot"]) == ("queue", None, 1)
    assert (limits["idle_nodes"], limits["mixed_nodes"]) == (0, 300)


def test_c56_queue_still_stops_at_the_other_limits():
    """C56: "queue" removes only the node limit: the maximum still binds."""
    full = [member(f"fed-{n}") for n in range(1, 4)]
    decision = decide(
        NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue", max_federations=3), site(members=full))]
    )
    assert adds(decision) == {}
    assert limits_of(decision)["maximum"] == 0


def test_c56_queue_stacks_no_second_up_behind_one_still_waiting():
    """C56: one up whose job waits (not yet past the stall bound) leaves no room under "queue": nothing queues behind
    it, whether or not this process started it (`busy` is empty, as after a restart)."""
    decision = decide(NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue"), site(members=[waiting()]))])
    assert adds(decision) == {}
    limits = limits_of(decision)
    assert (limits["queue_slot"], limits["stalled"]) == (0, False)


def test_c56_queue_stall_still_zeroes_the_room_once_an_up_waits_past_it():
    """C56: an up that has waited past `queue_stall_s` is stalled, and the site adds nothing."""
    decision = decide(
        NOW,
        ledger(pending={"m1": 9}),
        [(settings(node_room="queue", queue_stall_s=900), site(members=[waiting(age_s=1000)]))],
    )
    assert adds(decision) == {}
    assert limits_of(decision)["stalled"] is True


def test_c56_queue_waits_for_no_broken_member_to_leave_the_queue():
    """C56: a member whose jobs are neither waiting, running nor ended is not an up in the queue: it does not hold the
    site's one waiting slot (the controller drains and takes it down). Its retirement holds the add that tick (C62);
    once it is down the site adds."""
    broken = member("fed-1", state="SUSPENDED")
    assert broken.broken
    decision = decide(NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue"), site(members=[broken]))])
    assert decision.targets["site-a"]["limits"]["queue_slot"] == 1
    assert decision.targets["site-a"]["limits"]["retiring"] == ["fed-1"]
    assert adds(decision) == {}
    gone = member("fed-1", status="down", state=None)
    decision = decide(NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue"), site(members=[gone]))])
    assert adds(decision) == {"site-a": 1}


def test_c56_queue_adds_nothing_where_a_site_has_no_nodes_to_add():
    """C56: a Compose site reports no nodes for a federation (its members are static): "queue" never gives it room."""
    decision = decide(
        NOW, ledger(pending={"m1": 9}), [(settings(node_room="queue"), site(idle_nodes=0, nodes_per_federation=0))]
    )
    assert adds(decision) == {}


@pytest.mark.parametrize("value", ["free", "", "Queue", None, True, 1])
def test_c56_an_unknown_node_room_is_refused_by_name(value):
    """C56: only "idle" and "queue" are values of `node_room`; anything else is refused, naming the key."""
    with pytest.raises(ValueError, match="node_room"):
        settings(node_room=value)
