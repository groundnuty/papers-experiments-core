"""The mount's I/O mode over the generations of a restored mount: each oneclient's log is judged and the worst stands
(O97), so a second oneclient is never unjudged."""

from expcore_onedata.sandbox import iomode
from expcore_onedata.sandbox.iomode import agrees, judge, worst

HEADER = "Log file created at: 2026/11/20 10:00:00\n"
START = (
    HEADER
    + "I1120 10:00:00.100300     1 logging.cc:84] Forced direct IO: {direct}\n"
    + "I1120 10:00:00.100400     1 logging.cc:85] Forced proxy IO: {proxy}\n"
)
PROXY, DIRECT, NEITHER, BOTH = (
    START.format(direct=d, proxy=p) for d, p in (("0", "1"), ("1", "0"), ("0", "0"), ("1", "1"))
)
FALLBACK = "W1120 10:00:03.000000    16 helpersCache.h:645] Storage st1 is not accessible in direct IO mode\n"


def snapshot_of(reading):
    """What the instrument records: the reading with the snapshot's version and its mount marker."""
    return {"version": 1, "mount": True, **reading}


def test_o97_a_single_generation_is_its_own_reading():
    """O97: one reading is returned as it is (a copy: changing it never changes the generation's own)."""
    reading = judge("proxy", PROXY)
    found = worst([reading])
    assert found == reading
    assert found is not reading


def test_o97_no_reading_is_none():
    """O97: with no generation's reading there is nothing to take: the caller reads it as one never taken."""
    assert worst([]) is None


def test_o97_generations_that_all_agree_give_the_first():
    """O97: two agreeing generations give an agreeing reading."""
    found = worst([judge("proxy", PROXY), judge("proxy", PROXY)])
    assert agrees(snapshot_of(found))


def test_o97_a_second_generation_that_disagrees_stands_over_a_first_that_agrees():
    """O97: oneclient of generation 2 ran in no forced mode: the run is not shown to be the declared mode."""
    found = worst([judge("proxy", PROXY), judge("proxy", NEITHER)])
    assert found["effective"] == iomode.AUTO
    assert not found["agree"]
    assert not agrees(snapshot_of(found))


def test_o97_a_first_generation_that_disagrees_stands_over_a_second_that_agrees():
    """O97: the order of the generations does not matter: a generation that disagrees stands."""
    found = worst([judge("proxy", NEITHER), judge("proxy", PROXY)])
    assert found["effective"] == iomode.AUTO
    assert not agrees(snapshot_of(found))


def test_o97_a_generation_whose_log_cannot_be_read_is_not_agreement():
    """O97: a second oneclient whose log was never read leaves the run unproven, never agreeing."""
    found = worst([judge("proxy", PROXY), judge("proxy", None)])
    assert found["effective"] == iomode.UNKNOWN
    assert not agrees(snapshot_of(found))


def test_o97_a_contradicting_log_is_worse_than_an_unreadable_one_which_is_worse_than_no_forced_mode():
    """O97: among the generations that disagree, mixed, then unknown, then auto, then the other forced mode."""
    mixed, unknown = judge("proxy", BOTH), judge("proxy", None)
    auto, other = judge("proxy", NEITHER), judge("proxy", DIRECT)
    assert worst([other, auto, unknown, mixed])["effective"] == iomode.MIXED
    assert worst([other, auto, unknown])["effective"] == iomode.UNKNOWN
    assert worst([other, auto])["effective"] == iomode.AUTO
    assert worst([auto, other])["effective"] == iomode.AUTO


def test_o97_of_two_equally_bad_generations_the_earlier_stands():
    """O97: equal readings give the first; the worst is never a merge."""
    first, second = judge("proxy", NEITHER), judge("proxy", NEITHER + FALLBACK)
    assert first != second
    assert first["effective"] == second["effective"] == iomode.AUTO
    assert worst([first, second]) == first
    assert worst([second, first]) == second


def test_o97_the_worst_keeps_the_shape_the_instrument_reads():
    """O97: the answer has the keys of `judge`, so the instrument's `agrees` and refusal read it as they do any."""
    found = worst([judge("direct", DIRECT), judge("direct", PROXY)])
    assert set(found) == set(judge("direct", DIRECT))
    assert found["declared"] == "direct"
    assert found["effective"] == "proxy"
    assert not agrees(snapshot_of(found))
