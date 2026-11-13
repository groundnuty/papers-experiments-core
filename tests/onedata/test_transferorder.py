"""The walk of the ended-transfer listing, newest first down to a cutoff, its order checked as it goes, and where a
window opens on the providers' clocks (O32).

Each test of the walk hands `newest_first` a listing of its own: pages of ids with the next token, and each id's finish
time as its record. The walk reads pages through `page` and records through `read`, so a test sees each read made."""

import pytest

from expcore_onedata.transferorder import STOP_MARGIN_S, OrderBroken, newest_first, window_opens
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


def test_o32_the_check_past_the_stop_costs_one_page_and_one_record():
    """O32: past the page that holds the stop, the walk reads one more page and the record of its first job, finds it
    no later than the last finish read, and returns what it kept, saying older jobs exist. The page after that is
    never read."""
    listing = Listing([("run", 1010.0), ("old", 100.0)], [("older", 90.0), ("oldest", 80.0)], [("ancient", 10.0)])
    found = walk(listing)
    assert (list(found.kept), found.newest, found.earlier, found.pages) == (["run"], 1010.0, True, 2)
    assert listing.pages_read == [0, 1]
    assert listing.records_read == ["run", "old", "older"]


def test_o32_an_equal_finish_time_past_the_stop_is_in_order():
    """O32: the listing orders one second's jobs by id, so the next page's first job may share the last finish read."""
    found = walk(Listing([("old", 100.0)], [("same", 100.0)]))
    assert (found.kept, found.earlier, found.pages) == ({}, True, 2)


@pytest.mark.parametrize(("then", "broken"), [(1010.0, True), (50.0, False)])
def test_o32_a_record_past_the_stop_without_a_finish_time_passes_the_check_to_the_next(then, broken):
    """O32: a record that gives no finish time takes no part in the check, so the job after it on that page is the
    one compared: a later finish there still breaks the order, and an earlier one keeps it."""
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


# -- where the window opens on the providers' clocks -----------------------------------------------------------------


def test_o32_the_window_opens_where_the_least_offset_puts_it_in_whole_seconds():
    """O32: a record's times are on the clock of the provider that wrote them, so the window opens at its start moved
    by the least offset any listing answer gave, rounded down to the listing's whole seconds."""
    opens, clock = window_opens(1000.0, {"providers[0]": [0.2, -0.4], "providers[1]": [-3.5]}, 5.0)
    assert opens == 996
    assert clock == {"least_offset_s": -3.5, "greatest_offset_s": 0.2, "window_opens": 996}


@pytest.mark.parametrize(("behind", "fails"), [(STOP_MARGIN_S, False), (STOP_MARGIN_S + 1, True)])
def test_o32_a_clock_more_than_the_margin_behind_leaves_the_window_unplaced(behind, fails):
    """O32: a reader walks down to the start less the allowance and the margin; a provider clock behind by the margin
    still opens the window at that cutoff, and one a second further behind opens it below, past the stop."""
    offsets = {"providers[0]": [0.0], "providers[1]": [-behind]}
    if fails:
        with pytest.raises(Unreadable, match="clock runs more than"):
            window_opens(1000.0, offsets, 5.0)
    else:
        assert window_opens(1000.0, offsets, 5.0)[0] == 1000 - STOP_MARGIN_S


def test_o32_an_answer_without_a_readable_date_leaves_the_window_unplaced():
    """O32: one listing answer whose Date could not be read leaves its provider's clock unknown, named by its label."""
    with pytest.raises(Unreadable, match=r"providers\[1\]: a listing answer carried no Date"):
        window_opens(1000.0, {"providers[0]": [0.0], "providers[1]": [0.0, None]}, 5.0)
