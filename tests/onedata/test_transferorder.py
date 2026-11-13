"""The walk of the ended-transfer listing, newest first down to a cutoff, its order checked as it goes, and the band
at a window's start on the clocks that stamp finish times (O32).

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


def test_o32_the_band_runs_from_the_least_offset_less_the_allowance_to_the_greatest_plus_it():
    """O32: a finish time is on the platform's global clock, not the system clock a Date header gives, so the band at
    the window's start spans every place the start may have: the least offset less the allowance, rounded down, to the
    greatest plus it, rounded up, in the listing's whole seconds."""
    band, clock = start_band(1000.0, {"providers[0]": [0.2, -0.4], "providers[1]": [-3.5]}, 5.0)
    assert (band.first, band.last) == (991, 1006)
    assert clock == {"least_offset_s": -3.5, "greatest_offset_s": 0.2, "allowance_s": 5.0, "band": [991, 1006]}


def test_o32_a_job_one_provider_stamped_before_the_window_is_uncertain_never_inside():
    """O32: the delta review's case: offsets A 0 and B -8 and a job finishing at 995, which A's clock puts before a
    window that starts at 1000. The least offset alone would open the window at 992 and count it; the record cannot
    say whose clock stamped it, so it lies in the band and is uncertain, never inside."""
    band, _ = start_band(1000.0, {"providers[0]": [0.0], "providers[1]": [-8.0]}, 5.0)
    assert band.place(995.0) == UNCERTAIN


@pytest.mark.parametrize(
    ("moment", "place"), [(986.0, EARLIER), (987.0, UNCERTAIN), (1005.0, UNCERTAIN), (1006.0, INSIDE)]
)
def test_o32_the_bands_edges_are_uncertain_and_a_second_past_either_is_not(moment, place):
    """O32: with offsets A 0 and B -8 and an allowance of 5 s, the band at a start of 1000 is 987 to 1005, both ends
    included: a finish a second before it ended before the start, and one a second after it ended after the start."""
    band, clock = start_band(1000.0, {"providers[0]": [0.0], "providers[1]": [-8.0]}, 5.0)
    assert clock["band"] == [987, 1005]
    assert band.place(moment) == place


@pytest.mark.parametrize(("behind", "fails"), [(STOP_MARGIN_S, False), (STOP_MARGIN_S + 1, True)])
def test_o32_a_clock_more_than_the_margin_behind_leaves_the_window_unplaced(behind, fails):
    """O32: a reader walks down to the start less the allowance and the margin; a provider clock behind by the margin
    still opens the band at that cutoff, and one a second further behind opens it below, past the stop."""
    offsets = {"providers[0]": [0.0], "providers[1]": [-behind]}
    if fails:
        with pytest.raises(Unreadable, match="clock runs more than"):
            start_band(1000.0, offsets, 5.0)
    else:
        assert start_band(1000.0, offsets, 5.0)[0].first == 1000 - 5 - STOP_MARGIN_S


def test_o32_an_answer_without_a_readable_date_leaves_the_window_unplaced():
    """O32: one listing answer whose Date could not be read leaves its provider's clock unknown, named by its label."""
    with pytest.raises(Unreadable, match=r"providers\[1\]: a listing answer carried no Date"):
        start_band(1000.0, {"providers[0]": [0.0], "providers[1]": [0.0, None]}, 5.0)
