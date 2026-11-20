"""C60: a site's allowance counts a member whose down the ledger shows ended at what its jobs billed (from their start,
the job end recorded at its join less one whole --time, to the proven end), not at its whole --time; every other member
made, and a downed one whose times the ledger does not hold, keeps its whole --time."""

from control_support import HOUR, NOW, joined, ledger, member, settings, site

from expcore.campaign.elastic.control import Add, decide, made_units

RATE, TIME_H = 47.0, 12.0  # the helpers' site: 47 units an hour, a 12 h --time
WHOLE = RATE * TIME_H


def downed_row(started_h_ago: float, ended_h_ago: float | None):
    """The ledger's row of fed-2, whose jobs started `started_h_ago` hours ago and whose end was proven
    `ended_h_ago` hours ago (None: not proven yet)."""
    end_in = (TIME_H - started_h_ago) * HOUR  # its recorded job end is its start plus one whole --time
    ended = None if ended_h_ago is None else NOW - ended_h_ago * HOUR
    return joined("fed-2", end_in_s=end_in, ended_at=ended, end_proof=None if ended is None else "cancelled")


def adds(federations, allowance_units: float) -> int:
    """How many the site adds with fed-1 up and fed-2 down, under the given allowance."""
    members = [member("fed-1"), member("fed-2", status="down", state=None)]
    view = ledger(pending={"m1": 5}, federations=[joined("fed-1"), *federations])
    decision = decide(NOW, view, [(settings(allowance_units=allowance_units), site(members=members))])
    return sum(action.count for action in decision.actions if isinstance(action, Add))


def test_c60_a_downed_member_the_ledger_shows_ended_counts_what_its_jobs_billed():
    """C60: fed-2 ran from 3 h ago to its proven end 1 h ago: it counts 2 h at the rate, so an allowance of two whole
    --times plus those 2 h leaves room for one more federation (with its whole --time it would leave none)."""
    rows = [downed_row(started_h_ago=3.0, ended_h_ago=1.0)]
    members = [member("fed-1"), member("fed-2", status="down", state=None)]
    federations = {f.label: f for f in (joined("fed-1"), *rows)}
    assert made_units(settings(), site(members=members), federations) == WHOLE + 2 * RATE
    assert adds(rows, allowance_units=2 * WHOLE + 2 * RATE) == 1


def test_c60_a_downed_member_whose_times_the_ledger_does_not_hold_keeps_its_whole_time():
    """C60: fed-2 never joined (no ledger row), or its end is not proven yet: it counts its whole --time, and the same
    allowance leaves no room."""
    for rows in ([], [downed_row(started_h_ago=3.0, ended_h_ago=None)]):
        assert adds(rows, allowance_units=2 * WHOLE + 2 * RATE) == 0, rows


def test_c60_a_member_not_down_keeps_its_whole_time_whatever_the_ledger_shows():
    """C60: a member that may still hold a job (up, failed, made) counts its whole --time, even one whose end the
    ledger has proven: only a verified down ends what it reserves."""
    for status in ("up", "failed", "made"):
        members = [member("fed-2", status=status)]
        federations = {"site-a-fed-2": downed_row(started_h_ago=3.0, ended_h_ago=1.0)}
        assert made_units(settings(), site(members=members), federations) == WHOLE, status


def test_c60_a_downed_member_counts_nothing_for_a_job_that_never_ran_and_never_more_than_its_whole_time():
    """C60: an end proven before its start counts 0 (Slurm bills a job that never started nothing); an end past its
    recorded job end counts one whole --time at most."""
    members = [member("fed-2", status="down", state=None)]
    never_ran = {"site-a-fed-2": downed_row(started_h_ago=-1.0, ended_h_ago=0.0)}
    assert made_units(settings(), site(members=members), never_ran) == 0.0
    overran = {"site-a-fed-2": downed_row(started_h_ago=TIME_H + 2.0, ended_h_ago=0.0)}
    assert made_units(settings(), site(members=members), overran) == WHOLE
