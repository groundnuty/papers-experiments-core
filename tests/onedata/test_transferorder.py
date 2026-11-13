"""The walk of the ended-transfer listing, newest first down to a cutoff, its order checked as it goes, and the band
at a window's start on the global clock, the one that stamps finish times, as Onezone's current-time endpoint gives it
(O32).

Each test of the walk hands `newest_first` a listing of its own: pages of ids with the next token, and each id's finish
time as its record: 0 for a job given none (cancelled before it was enqueued), None for a record with no readable one.
The walk reads pages through `page` and records through `read`, so a test sees each read made."""

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
from expcore_onedata.transferwindow import ZoneClock, zone_clock_of

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
    options = {"cutoff": CUTOFF, "bound": 10, "max_pages": 10, **options}
    return newest_first(listing.page, listing.read, lambda moment: moment, **options)


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


def test_o32_a_job_with_no_finish_time_stops_the_walk_and_what_follows_counts_for_nothing():
    """O32: the third delta review's case. A job cancelled while scheduled keeps finishTime 0, so its key is the
    largest and the listing puts it after every job that has a finish time. One recent finish followed by two of them,
    on a bound of 1: the walk keeps the finish, stops at the first of the others and counts neither it nor the rest
    against the bound, where it kept them and raised; it says no older job is listed."""
    listing = Listing([("run", 1010.0), ("cancel-1", 0.0), ("cancel-2", 0.0)])
    found = walk(listing, bound=1)
    assert (list(found.kept), found.newest, found.earlier, found.pages) == (["run"], 1010.0, False, 1)


def test_o32_a_long_tail_of_jobs_with_no_finish_time_costs_the_stops_page_and_the_check_past_it():
    """O32: the history's tail of such cancellations spans pages. The walk stops on the first page, reads the next for
    the order check and keeps none of them, and never reads the third."""
    listing = Listing([("run", 1010.0), ("cancel-a", 0.0)], [("cancel-b", 0.0), ("cancel-c", 0.0)], [("cancel-d", 0.0)])
    found = walk(listing, bound=1)
    assert (list(found.kept), found.pages) == (["run"], 2)
    assert listing.pages_read == [0, 1]
    assert listing.records_read == ["run", "cancel-a", "cancel-b", "cancel-c"]


def test_o32_a_listing_that_begins_with_jobs_with_no_finish_time_keeps_nothing_and_has_no_newest():
    """O32: a listing of such jobs alone has no finish time to name and holds nothing of the window."""
    found = walk(Listing([("cancel-a", 0.0), ("cancel-b", 0.0)]))
    assert (found.kept, found.newest, found.earlier) == ({}, None, False)


def test_o32_a_stop_at_an_old_job_then_jobs_with_no_finish_time_is_in_order():
    """O32: the jobs with no finish time follow the old ones, as the listing orders them, so they break nothing, and
    the old job that stopped the walk still says older jobs are listed."""
    found = walk(Listing([("old", 100.0)], [("cancel", 0.0)]))
    assert (found.kept, found.earlier, found.pages) == ({}, True, 2)


@pytest.mark.parametrize(
    "pages",
    [
        [[("cancel", 0.0), ("late", 1010.0)]],
        [[("run", 1010.0), ("cancel", 0.0)], [("late", 990.0)]],
        [[("run", 1010.0), ("cancel", 0.0)], [("other", 0.0), ("late", 5.0)]],
    ],
    ids=["same page", "page past the stop", "after another with none"],
)
def test_o32_a_job_with_a_finish_time_after_one_with_none_fails_closed(pages):
    """O32: a job with a finish time listed after one with none cannot be, since the keys sort the latter last: the
    order the stop rests on does not hold, so the walk raises on the stop's page and on the page past it, even for a
    time older than the cutoff."""
    with pytest.raises(OrderBroken, match="listed after one with none"):
        walk(Listing(*pages))


def test_o32_a_record_with_no_finish_time_that_does_not_say_it_ended_may_lag_and_is_kept():
    """O32: a record whose finishTime is 0 but that does not say it ended may be one that lags its listing entry, whose
    true finish time is unread: it is kept, the walk goes on past it, and it breaks no order, so the job after it that
    has a finish time is read too."""
    found = walk(Listing([("run", 1010.0), ("lag", 0.0), ("also", 990.0)]), ended=lambda moment: moment > 0)
    assert (list(found.kept), found.earlier, found.pages) == (["run", "lag", "also"], False, 1)


def test_o32_past_the_stop_no_record_is_kept_not_even_one_that_may_lag():
    """O32 (Codex's case): once the walk has stopped, at an old job or at one with no finish time that says it ended,
    the records after it on the page are read for the order check alone; one with no finish time that does not say it
    ended is not kept there either, so a tail of them cannot exhaust the bound."""
    says_ended = {"run": True, "done": True, "lag1": False, "lag2": False}
    listing = Listing([("run", 1010.0), ("done", 0.0), ("lag1", 0.0), ("lag2", 0.0)])
    found = newest_first(
        listing.page,
        list,  # each record is its id here, so `ended` can tell the records apart
        lambda tid: listing.finish[tid],
        cutoff=CUTOFF,
        bound=2,
        max_pages=10,
        ended=lambda tid: says_ended[tid],
    )
    assert (list(found.kept), found.pages) == (["run"], 1)


# -- the band at the window's start ----------------------------------------------------------------------------------


def test_o32_the_band_runs_from_the_global_clocks_least_offset_less_the_allowance_to_its_greatest_plus_it():
    """O32: a finish time is stamped on the global clock, so the band at the window's start spans every place the start
    may have on it: the least offset the readings allow less the allowance, rounded down, to the greatest plus it,
    rounded up, in the listing's whole seconds."""
    band, clock = start_band(1000.0, [ZoneClock(0.2, 0.0, 0.0), ZoneClock(-3.0, 0.5, -1.0)], 5.0)
    assert (band.first, band.last) == (991, 1006)
    assert clock == {
        "zone_least_offset_s": -3.5,
        "zone_greatest_offset_s": 0.2,
        "zone_half_trip_s": 0.5,
        "zone_date_offsets_s": [-1.0, 0.0],
        "allowance_s": 5.0,
        "band": [991, 1006],
    }


def test_o32_half_the_round_trip_widens_the_band_on_both_sides_with_the_allowance():
    """O32: a reading's offset is the global time less the midpoint of the host's clock around the read, and the stamp
    may have been made anywhere between the two host readings, so half the round trip widens the band both ways: 2 s
    with an allowance of 5 s opens it 7 s below the start and closes it 7 s above."""
    band, clock = start_band(1000.0, [ZoneClock(0.0, 2.0)], 5.0)
    assert clock["band"] == [993, 1007]
    assert (band.place(992.0), band.place(993.0), band.place(1007.0), band.place(1008.0)) == (
        EARLIER,
        UNCERTAIN,
        UNCERTAIN,
        INSIDE,
    )


@pytest.mark.parametrize(("moment", "place"), [(994.0, EARLIER), (1001.0, UNCERTAIN), (1006.0, INSIDE)])
def test_o32_codexs_case_a_node_whose_date_runs_ahead_of_the_global_clock_never_places_a_job_earlier(moment, place):
    """O32: the third delta review's case: a multi-node Onezone's answering node has a system clock 8 s ahead and a
    global-clock bias of -8 s, so its Date header says +8 while the global clock, which providers stamp finish times
    with, agrees with this host. The Date header put the band at 1003 to 1013 and a finish at 1001 earlier, with no
    uncertainty. The reading of the global clock gives offset 0: the band is 995 to 1005, a finish at 1001 is
    uncertain, and the Date's +8 is only recorded."""
    band, clock = start_band(1000.0, [ZoneClock(0.0, 0.0, 8.0)], 5.0)
    assert clock["band"] == [995, 1005]
    assert clock["zone_date_offsets_s"] == [8.0, 8.0]
    assert band.place(moment) == place


@pytest.mark.parametrize(
    ("moment", "place"), [(986.0, EARLIER), (987.0, UNCERTAIN), (1005.0, UNCERTAIN), (1006.0, INSIDE)]
)
def test_o32_the_bands_edges_are_uncertain_and_a_second_past_either_is_not(moment, place):
    """O32: with the global clock's offsets 0 and -8 and an allowance of 5 s, the band at a start of 1000 is 987 to
    1005, both ends included: a finish a second before it ended before the start, and one a second after it ended
    after it."""
    band, clock = start_band(1000.0, [ZoneClock(0.0, 0.0), ZoneClock(-8.0, 0.0)], 5.0)
    assert clock["band"] == [987, 1005]
    assert band.place(moment) == place


@pytest.mark.parametrize(("behind", "fails"), [(STOP_MARGIN_S, False), (STOP_MARGIN_S + 1, True)])
def test_o32_the_global_clock_more_than_the_margin_behind_leaves_the_window_unplaced(behind, fails):
    """O32: a reader walks down to the start less the allowance and the margin; a global clock behind by the margin
    still opens the band at that cutoff, and one a second further behind opens it below, past the stop."""
    if fails:
        with pytest.raises(Unreadable, match="Onezone's clock, the one finish times are stamped on, runs more than"):
            start_band(1000.0, [ZoneClock(0.0, 0.0), ZoneClock(-behind, 0.0)], 5.0)
    else:
        assert (
            start_band(1000.0, [ZoneClock(0.0, 0.0), ZoneClock(-behind, 0.0)], 5.0)[0].first == 1000 - 5 - STOP_MARGIN_S
        )


@pytest.mark.parametrize("zone", [[], [ZoneClock(0.0, 0.0), None]])
def test_o32_a_reading_of_the_global_clock_that_could_not_be_read_leaves_the_window_unplaced(zone):
    """O32: no reading, or one that could not be read, leaves the clock that stamps finish times unknown: the band is
    not placed."""
    with pytest.raises(Unreadable, match="zone: Onezone's reading of the global clock could not be read"):
        start_band(1000.0, zone, 5.0)


# -- one reading of the global clock ---------------------------------------------------------------------------------


def test_o32_a_reading_is_the_global_time_less_the_midpoint_of_the_hosts_clock_around_the_read():
    """O32: asked at 1000.0 and received at 1002.0 on this host, an answer of 1005000 ms is 1005 s on the global
    clock; the midpoint of the host's clock is 1001, so the offset is +4 s, within half the round trip, 1 s, of the
    truth. The Date header (a whole second, 1004 here) is the cross-check, against the same midpoint."""
    reading = zone_clock_of({"timeMillis": 1005000}, "Thu, 01 Jan 1970 00:16:44 GMT", 1000.0, 1002.0)
    assert reading is not None
    assert (reading.offset, reading.half_trip, reading.low, reading.high) == (4.0, 1.0, 3.0, 5.0)
    assert reading.date_offset == 3.0  # 1004 s against 1001


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"timeMillis": None},
        {"timeMillis": "1005000"},
        {"timeMillis": True},
        {"timeMillis": -1},
        {"timeMillis": float("nan")},
        {"timeMillis": float("inf")},
        [1005000],
        None,
    ],
)
def test_o32_an_answer_without_a_timemillis_that_is_a_number_gives_no_reading(answer):
    """O32: the answer must hold `timeMillis` as a non-negative number, else the clock is unknown, never guessed."""
    assert zone_clock_of(answer, None, 1000.0, 1001.0) is None


def test_o32_a_host_clock_that_moved_backwards_during_the_read_gives_no_reading():
    """O32: a round trip of negative length has no midpoint to measure from."""
    assert zone_clock_of({"timeMillis": 1000000}, None, 1002.0, 1000.0) is None


def test_o32_a_date_header_that_cannot_be_read_does_not_spoil_the_reading():
    """O32: the Date header is a cross-check alone: without one the reading stands, with no Date offset."""
    reading = zone_clock_of({"timeMillis": 1000000}, None, 1000.0, 1000.0)
    assert reading is not None
    assert (reading.offset, reading.half_trip, reading.date_offset) == (0.0, 0.0, None)
