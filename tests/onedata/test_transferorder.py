"""The walk of the ended-transfer listing, newest first down to a cutoff, its order checked as it goes, and the band
at a window's start on Onezone's clock, the one that stamps finish times (O32).

Each test of the walk hands `newest_first` a listing of its own: pages of ids with the next token, and each id's finish
time as its record. The walk reads pages through `page` and records through `read`, so a test sees each read made."""

import pytest

from expcore_onedata.transferorder import (
    EARLIER,
    INSIDE,
    STOP_MARGIN_S,
    UNCERTAIN,
    OrderBroken,
    newest_first,
    start_band,
)
from expcore_onedata.transferrecord import Unreadable

CUTOFF = 985.0  # a window that opens at 1000, less an allowance of 5 s and the margin of 10 s


class Listing:
    """A listing of pages, each a list of `(id, finish time or None)`, with the reads a walk made of it."""

    def __init__(self, *pages: list[tuple[str, float | None]]) -> None:
        self.pages = list(pages)
        self.finish = {tid: moment for page in pages for tid, moment in page}
        self.pages_read: list[int] = []
        self.records_read: list[str] = []

    def page(self, token: str | None) -> tuple[list[str], str | None]:
        index = 0 if token is None else int(token)
        self.pages_read.append(index)
        following = str(index + 1) if index + 1 < len(self.pages) else None
        return [tid for tid, _ in self.pages[index]], following

    def read(self, ids):
        self.records_read += ids
        return [self.finish[tid] for tid in ids]


def walk(listing: Listing, **options):
    return newest_first(
        listing.page, listing.read, lambda moment: moment, cutoff=CUTOFF, bound=10, max_pages=10, **options
    )


def test_o32_an_order_violation_across_the_stopping_pages_boundary_fails_closed():
    """O32: an old job alone on the first page would end the walk there; the run's job on the next page finished
    later, so the order the stop rests on does not hold, and the walk raises rather than return an empty window."""
    listing = Listing([("old", 100.0)], [("run", 1010.0)])
    with pytest.raises(OrderBroken, match="newest first"):
        walk(listing)


def test_o32_a_job_of_the_window_behind_an_older_one_past_the_stop_fails_closed():
    """O32: an old job alone on the first page ends the walk there; on the next page an older job comes first and the
    run's job after it, so the first record past the stop is in order and the run's job is not. Every record of the
    page past the stop is checked, so the walk raises rather than return an empty window (cutoff 985)."""
    listing = Listing([("old", 100.0)], [("older", 90.0), ("run", 1010.0)])
    with pytest.raises(OrderBroken, match="newest first"):
        walk(listing)
    assert listing.records_read == ["old", "older", "run"]


def test_o32_the_check_past_the_stop_costs_one_page_and_its_records():
    """O32: past the page that holds the stop, the walk reads one more page and every record on it, finds each no
    later than the one before it, keeps none, and returns what it kept, saying older jobs exist. The page after that
    is never read."""
    listing = Listing([("run", 1010.0), ("old", 100.0)], [("older", 90.0), ("oldest", 80.0)], [("ancient", 10.0)])
    found = walk(listing, chunk=2)
    assert (list(found.kept), found.newest, found.earlier, found.pages) == (["run"], 1010.0, True, 2)
    assert listing.pages_read == [0, 1]
    assert listing.records_read == ["run", "old", "older", "oldest"]


def test_o32_an_equal_finish_time_past_the_stop_is_in_order():
    """O32: the listing orders one second's jobs by id, so the next page's first job may share the last finish read."""
    found = walk(Listing([("old", 100.0)], [("same", 100.0)]))
    assert (found.kept, found.earlier, found.pages) == ({}, True, 2)


@pytest.mark.parametrize(("then", "broken"), [(1010.0, True), (50.0, False)])
def test_o32_a_record_past_the_stop_without_a_finish_time_takes_no_part_in_the_check(then, broken):
    """O32: a record that gives no finish time takes no part in the check, so the job after it on that page is
    compared with the last finish read: a later finish there still breaks the order, and an earlier one keeps it."""
    listing = Listing([("old", 100.0)], [("unknown", None), ("then", then)])
    if broken:
        with pytest.raises(OrderBroken):
            walk(listing)
    else:
        assert walk(listing).earlier is True
    assert listing.records_read == ["old", "unknown", "then"]


def test_o32_a_stop_on_the_last_page_reads_nothing_more():
    """O32: when the page that holds the stop is the listing's last, there is no page after it to check."""
    listing = Listing([("run", 1010.0)], [("old", 100.0)])
    found = walk(listing)
    assert (list(found.kept), found.earlier, found.pages) == (["run"], True, 2)
    assert listing.pages_read == [0, 1]


def test_o32_a_listing_that_ends_inside_the_window_says_no_older_job_exists():
    """O32: a listing whose every job finished after the cutoff is read to its end, and the walk says nothing older is
    listed."""
    found = walk(Listing([("run", 1010.0)], [("also", 990.0)]))
    assert (list(found.kept), found.earlier, found.pages) == (["run", "also"], False, 2)


# -- the band at the window's start ----------------------------------------------------------------------------------


def test_o32_the_band_runs_from_onezones_least_offset_less_the_allowance_to_its_greatest_plus_it():
    """O32: a finish time is stamped on the global clock, Onezone's, so the band at the window's start spans every
    place the start may have on it: Onezone's least offset less the allowance, rounded down, to its greatest plus it,
    rounded up, in the listing's whole seconds."""
    band, clock = start_band(1000.0, [0.2, -3.5], 5.0)
    assert (band.first, band.last) == (991, 1006)
    assert clock == {
        "zone_least_offset_s": -3.5,
        "zone_greatest_offset_s": 0.2,
        "allowance_s": 5.0,
        "band": [991, 1006],
    }


@pytest.mark.parametrize(("moment", "place"), [(994.0, EARLIER), (1001.0, UNCERTAIN), (1006.0, INSIDE)])
def test_o32_codexs_case_providers_ahead_and_onezone_agreeing_place_a_job_at_1001_never_earlier(moment, place):
    """O32: the second delta review's case: both providers' Date headers run 8 s ahead, Onezone agrees with this host,
    the allowance is 5 s. The providers' offsets gave the band 1003 to 1013, so a job that truly finished at 1001, on
    Onezone's clock, was placed before the window: neither ended nor uncertain. On Onezone's offset the band is 995 to
    1005, so it is uncertain, and one at 1006 is inside where the old band left it uncertain."""
    band, clock = start_band(1000.0, [0.0, 0.0], 5.0)
    assert clock["band"] == [995, 1005]
    assert band.place(moment) == place


@pytest.mark.parametrize(
    ("moment", "place"), [(986.0, EARLIER), (987.0, UNCERTAIN), (1005.0, UNCERTAIN), (1006.0, INSIDE)]
)
def test_o32_the_bands_edges_are_uncertain_and_a_second_past_either_is_not(moment, place):
    """O32: with Onezone's offsets 0 and -8 and an allowance of 5 s, the band at a start of 1000 is 987 to 1005, both
    ends included: a finish a second before it ended before the start, and one a second after it ended after it."""
    band, clock = start_band(1000.0, [0.0, -8.0], 5.0)
    assert clock["band"] == [987, 1005]
    assert band.place(moment) == place


@pytest.mark.parametrize(("behind", "fails"), [(STOP_MARGIN_S, False), (STOP_MARGIN_S + 1, True)])
def test_o32_onezones_clock_more_than_the_margin_behind_leaves_the_window_unplaced(behind, fails):
    """O32: a reader walks down to the start less the allowance and the margin; Onezone's clock behind by the margin
    still opens the band at that cutoff, and one a second further behind opens it below, past the stop."""
    if fails:
        with pytest.raises(Unreadable, match="Onezone's clock, the one finish times are stamped on, runs more than"):
            start_band(1000.0, [0.0, -behind], 5.0)
    else:
        assert start_band(1000.0, [0.0, -behind], 5.0)[0].first == 1000 - 5 - STOP_MARGIN_S


@pytest.mark.parametrize("zone", [[], [0.0, None]])
def test_o32_onezones_clock_unread_leaves_the_window_unplaced(zone):
    """O32: no answer of Onezone's read, or one whose Date could not be read, leaves the clock that stamps finish
    times unknown: the band is not placed."""
    with pytest.raises(Unreadable, match="zone: an answer of Onezone's carried no Date"):
        start_band(1000.0, zone, 5.0)
