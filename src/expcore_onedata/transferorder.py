# Provenance: new
"""The ended-transfer listing read newest first, down to a window's start, its order checked as it goes (O32).

**The order, from the source.** On the federation's build (op-worker `f118ed9`), `GET /spaces/{space}/transfers`
lists one state's jobs in increasing order of a link key: `9999999999 - T`, ten digits, then the first six characters
of the job's id (`src/modules/transfer/transfer_links.erl:160-164`), where T is the job's finish time for `ended` and
its schedule time for `waiting` and `ongoing` (`:56-95`), in seconds. So the newest comes first, and one second's jobs
in the order of their ids. A `page_token` is the key of the last job a page gave, and the next page starts after it,
so the order holds across pages. No parameter filters by time, `limit` is 1 to 1000, and an ended job is never
removed and never expires: `DELETE /transfers/{id}` is a cancel, which a job that has ended refuses, and the call that
deletes a job's document has no caller outside the platform's tests. A record (`GET /transfers/{id}`) carries the
times as `scheduleTime`, `startTime` and `finishTime`, in seconds. Oneprovider's REST description states none of the
order: it is a property of this build, so the readers check it as they go.

**The walk.** `newest_first` pages through the ended listing and reads each job's record in its turn. It keeps every
job it reads except one whose record says it ended (`ended`, a reader's test) before a cutoff, and it stops at the end
of the page that holds the first such job: by the order, every job listed after it finished earlier still. A job whose
record finished before the cutoff but does not say it ended is kept, and the walk goes on past it, since such a job may
have run on into the window. A reader thus reads its window, the rest of the page that holds the stop, and the check
past it (below), whatever the space's history, and its bound counts the jobs it keeps, never the history. A window
that truly holds more than the bound is unknown, as before, never cut. The waiting and ongoing listings are not walked:
they are few, and any job in them matters, so the readers read them to their end.

**The jobs with no finish time sort last.** A job cancelled while still scheduled, before it was enqueued, is given no
finish time (`replication_status.erl:264-271`, `replica_eviction_status.erl:257-262`; one cancelled once enqueued is
stamped): its `finishTime` stays 0, so its link key, `9999999999 - 0`, is the largest and the ended listing puts it
after every job that has a finish time. The walk therefore stops at the first record whose `finish` is 0 and that says
it ended, as at a job older than the cutoff: that record, and every one listed after it, is neither kept nor counted
against the bound, so a history that accumulates such cancellations cannot exhaust a window's bound. A job with a finish
time listed after one with none breaks the order and is `OrderBroken`, on the stop's page and on the page past it. A
record with a finish time of 0 that does not say it ended may be a record that lags its listing entry, whose true
finish time is unread: it is kept, and the walk goes on, as for any record that gives no readable time.

**The check.** Every finish time read must be no later than the one before it, on a page and across pages; equal times
are allowed, since the listing orders one second's jobs by id. The page that holds the stop is read to its end for the
check, so an older job listed out of place ahead of newer ones on that page, which would end the walk early, is caught
there. One that fills the page alone, or ends it, is caught past it: when the stop's page is not the last, the walk
reads the next page and every record on it, in the reader's chunks, and keeps none of them; each must have finished no
later than the one before it, the last finish read on the stop's page first. So a job of the window anywhere on that
page, behind an older one (cutoff 985, pages `[100]` and `[90, 1010]`), is caught. The check costs one page and the
records on it (up to a page's `limit`, as many reads as the stop's page cost at most), and it is the only read past the
stop's page. A time later than the one before it, or any time after a record with none, raises `OrderBroken`, an
`Unreadable`: the reader fails closed, naming the assumption, and never stops early on a listing it cannot trust. A
record that gives no readable finish time at all (None) neither stops the walk nor takes part in the check, and is
kept; a reader that needs the time says so in `finish`.

**The residual.** A job of the window listed two or more pages past the stop is not seen, and nothing reports it:
catching it would take reading the whole listing, the space's history, which is what the walk exists to avoid. The
walk rests there on the order holding by construction: an ended job's link is added with its finish time as the
timestamp (`transfer_links.erl:83-95`, `add_ended`) and its key is `9999999999 - T` ahead of the id
(`:160-164`, `link_key`), so the listing cannot put a job that finished later after one that finished earlier while
the source is what runs. The check past the stop guards that assumption where its failure would cost a job of the
window, at the stop; it is not a proof of the order beyond it. A reader's count of ended jobs is a count of finish
times in its window, which never holds a job that was given none; the reset counts its own cancels apart.

**Never** a page token built by hand to start at a time: the token's form is undocumented. The walk starts at the
first page and follows the tokens the platform gives.

`STOP_MARGIN_S` widens a reader's cutoff below its window's start (and below the clock-skew allowance it adds): it is
how far behind this host the global clock may run before the readers fail closed, and it keeps the second-level times
of the listing clear of rounding at the edge.

**The clock a finish time is on, from the source.** The provider that runs a job stamps its finish: a replication's
(`replicatingProviderId` in the record), an eviction's (`evictingProviderId`), and for a migration the evicting one
when its replication completed and the replicating one when that part failed or was cancelled. Each status change runs
only on that provider (`transfer_changes.erl:21-28,75-89,121-135`), and a completed migration's replication leaves the
finish to its eviction (`replication_status.erl:222-231`; `replica_eviction_status.erl:223-229`). It stamps it with
`global_clock:monotonic_timestamp_seconds`, which ctool (op-worker's pin `8ae7baf`, `global_clock.erl:8-46`) defines
as Onezone's cluster time: the provider's system clock moved by a bias measured against Onezone, with an error "below
a second" that "can grow to a couple of seconds", or the system clock itself while it is not synchronised; by default a
provider connects to Onezone only once it is (`gs_channel_service.erl:41-46`). A provider's answer's `Date` header is
cowboy's `erlang:universaltime()` (cowboy 2.9.0, ctool's pin, `cowboy_clock.erl:61,74,97`), its system clock, so a
provider's Date offset says nothing of the clock its finish times are on: providers 8 s ahead by their Date headers
may stamp on a global clock that agrees with this host's.

**The global clock places a finish time.** On Onezone the global clock is its own: `global_clock`'s time is the node's
system clock plus a bias that is 0 until a synchronisation sets one, "identical to Onezone's cluster time" (ctool
`8ae7baf`, `global_clock.erl:9-10,86-87,259-262`), and the providers synchronise theirs to Onezone's public
`GET /provider/public/get_current_time`, which needs no privilege and answers `global_clock:timestamp_millis()` as
`timeMillis` (oz-worker `14b5bda`, which pins the same ctool: `provider_routes.erl:192-198`,
`provider_logic_plugin.erl:304-305,491-492`, `provider_rest_translator.erl:128-129`; `provider_logic.erl:548-554`,
"used by providers to synchronize clocks with onezone", "with the accuracy of about one second"). So a reader reads
that clock itself and places its window on it (`transferwindow.ZoneClock`): the answer's `timeMillis` less the midpoint
of this host's clock around the read is the global clock's offset from this host, and half the round trip is how far
that may be off; the reads are made before and after the listings. It is never taken from the `Date` header of an
Onezone answer: that is the answering node's system clock (cowboy's `erlang:universaltime()`), and a multi-node
Onezone's node can have a system clock that differs from the cluster's global clock by its bias (the node's system
clock 8 s ahead with a bias of -8 s stamps the same instant as a global clock that agrees with this host, though its
`Date` header says +8). The clock-skew allowance covers what lies between the offset and a stamp: a provider's
synchronisation error to Onezone's clock, and the cluster's own spread. A provider that is not synchronised stamps on
its own system clock, which Onezone's reading does not bound; the places rest on the providers being synchronised, as
the cutoff does. The `Date` headers, of Onezone's answer and of the providers', are recorded beside the offset, a
diagnostic that places nothing.

**The band at the window's start.** A reader that counts the jobs that ended since a window's start places each finish
time against a band, `start_band`: from the start moved by the global clock's least offset (each read's offset less its
half round trip) less the clock-skew allowance to the start moved by its greatest offset (each read's plus its half
round trip) plus the allowance, in whole seconds (`transferwindow.Edges.opening`, the band in which the probes flag a
job `transfer_near_window_edge`). A finish before the band ended before the start (`EARLIER`), one after it ended after
the start (`INSIDE`), and one in it may lie on either side (`UNCERTAIN`), which the reader reports apart and never
counts as either. The places are exact when every stamp lies within the allowance of the global clock as its readings
gave it.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from expcore_onedata.transferrecord import Unreadable
from expcore_onedata.transferwindow import Edges, Window, ZoneClock, span

STOP_MARGIN_S = 10.0  # seconds a reader's cutoff lies below its window's start, beyond the clock-skew allowance


class OrderBroken(Unreadable):
    """The ended listing is not newest first by finish time, the order a reader of a window rests on."""


@dataclass(frozen=True)
class Walk:
    """What a walk of the ended listing found: each kept job's id with what `read` gave for it, newest first; the
    first finish time read (the newest ended job's, in the window or not; None for a listing with none); whether it
    stopped at a job that ended before the cutoff, so that older ones exist (`earlier`; a stop at a job with no finish
    time says nothing of older ones); and how many pages it read, the one the check past the stop reads included."""

    kept: dict[str, Any]
    newest: float | None
    earlier: bool
    pages: int


def newest_first(
    page: Callable[[str | None], tuple[list[str], str | None]],
    read: Callable[[Sequence[str]], Sequence[Any]],
    finish: Callable[[Any], float | None],
    *,
    cutoff: float,
    bound: int,
    max_pages: int,
    ended: Callable[[Any], bool] = lambda record: True,
    where: str = "",
    chunk: int = 1,
) -> Walk:
    """Walk the ended listing from its first page to the end of the page that holds the first job that ended before
    `cutoff`, or the first that has no finish time, keeping every other job read.

    `page(token)` reads one page (None: the first) and returns its ids and the next token, None on the last; `read`
    reads the records of a few ids in their order, `chunk` at a time; `finish` gives a record's finish time, 0 for a
    job cancelled before it was enqueued (given none), None when the record gives no readable one, and `ended` whether
    the record says the job ended (every record, by default). A record with a finish time of 0 that says it ended
    stops the walk as a job older than the cutoff does, and is not kept, nor is any record after a stop, whatever its
    finish time: past the stop a record is read for the order check alone. Past `bound` kept jobs, or `max_pages` pages
    without an end, the listing is unknown: raise Unreadable. Raise OrderBroken when a finish time is later than the
    one before it, or follows a record with none, through the page past the stop's, whose every record the walk reads
    for that check alone and keeps none of (one page more than `max_pages` at most). An order broken only further on is
    not seen: the module docstring has the residual. `where` starts every message."""
    if bound < 1 or max_pages < 1 or chunk < 1:
        raise ValueError("a walk keeps at least one job, reads at least one page and one record at a time")
    kept: dict[str, Any] = {}
    order = _Order(where)
    stopped = earlier = False
    token: str | None = None
    for pages in range(1, max_pages + 1):
        ids, token = page(token)
        for tid, record in _records(ids, read, chunk):
            moment = finish(record)
            if moment is not None and moment > 0:
                order.check(moment, "a job's finishTime is later than that of the job listed before it")
                if moment < cutoff and ended(record):
                    stopped = earlier = True
                    continue  # ended before the window: the walk ends with this page, which is read for the check
            elif moment == 0 and ended(record):
                order.unstamped()
                stopped = True
                continue  # no finish time sorts last: none after it has one, so none counts, and the page is checked
            if stopped:
                continue  # past the stop a record is read for the order check alone, never kept, not even one that lags
            kept[tid] = record
            if len(kept) > bound:
                raise Unreadable(
                    f"{where}the ended listing holds more than {bound} transfers in the window: the listing is "
                    "unknown, not cut"
                )
        if stopped and token is not None:
            _check_past_stop(page(token)[0], read, finish, ended, chunk, order)
            return Walk(kept, order.newest, earlier, pages + 1)
        if stopped or token is None:
            return Walk(kept, order.newest, earlier, pages)
    raise Unreadable(f"{where}the ended listing did not end within {max_pages} pages")


class _Order:
    """The running check of a walk: each finish time read is no later than the one before it, and none follows a
    record that has no finish time, which the listing puts last. `check`, `unstamped` and the fields update in place,
    since the walk's pages and the page past its stop are one sequence."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.last: float | None = None  # the last finish time read
        self.newest: float | None = None  # the first one read
        self.none_after = False  # a record with no finish time was read: only such records may follow

    def check(self, moment: float, what: str) -> None:
        """Take the next finish time read, or raise OrderBroken, saying `what` broke the order."""
        if self.none_after:
            raise OrderBroken(
                f"{self.where}the ended listing is not newest first: a job with a finishTime is listed after one "
                "with none, which sorts last, and reading only a window assumes that order; the listing is unknown, "
                "not cut"
            )
        if self.last is not None and moment > self.last:
            raise OrderBroken(
                f"{self.where}the ended listing is not newest first: {what}, and reading only a window assumes that "
                "order; the listing is unknown, not cut"
            )
        self.last = moment
        self.newest = moment if self.newest is None else self.newest

    def unstamped(self) -> None:
        """Take a record with no finish time: from here the listing holds no job that has one."""
        self.none_after = True


def _records(
    ids: Sequence[str], read: Callable[[Sequence[str]], Sequence[Any]], chunk: int
) -> Iterator[tuple[str, Any]]:
    """Each id of a page with its record, the records read `chunk` at a time in the page's order."""
    for start in range(0, len(ids), chunk):
        part = ids[start : start + chunk]
        yield from zip(part, read(part), strict=True)


def _check_past_stop(
    ids: Sequence[str],
    read: Callable[[Sequence[str]], Sequence[Any]],
    finish: Callable[[Any], float | None],
    ended: Callable[[Any], bool],
    chunk: int,
    order: _Order,
) -> None:
    """Read every record of the page past the stop (`ids`) and raise OrderBroken when a finish time on it is later
    than the one before it, the last read on the stop's page first, or follows a record with none: the jobs past the
    stop are taken to be older and are never kept, so an older job ahead of newer ones, alone on the stop's page or
    anywhere on this one, would otherwise end the walk with a job of the window unseen. A record whose time cannot be
    read takes no part."""
    for _, record in _records(ids, read, chunk):
        moment = finish(record)
        if moment is not None and moment > 0:
            order.check(moment, "a job on the page past the one that ends the walk finished later than one before it")
        elif moment == 0 and ended(record):
            order.unstamped()


EARLIER, UNCERTAIN, INSIDE = "earlier", "uncertain", "inside"  # where a finish time lies against a window's start


@dataclass(frozen=True)
class Band:
    """The band at a window's start on the clocks that stamp finish times, both ends included, in whole seconds."""

    first: int
    last: int

    def place(self, moment: float) -> str:
        """`EARLIER` for a finish before the band (it ended before the start), `INSIDE` for one after it (it ended
        after the start), `UNCERTAIN` for one in it, which may lie on either side."""
        if moment < self.first:
            return EARLIER
        return UNCERTAIN if moment <= self.last else INSIDE


ZONE_UNREAD = (
    "zone: Onezone's reading of the global clock could not be read, so the clock finish times are stamped on is "
    "unknown and the window cannot be placed on it"
)
ZONE_BEHIND = (
    f"Onezone's clock, the one finish times are stamped on, runs more than {STOP_MARGIN_S:.0f} s behind this host's, "
    "beyond the margin the ended listing is read back by, so the window may hold jobs past where the reading stopped"
)


def start_band(since: float, zone: Sequence[ZoneClock | None], allowance: float) -> tuple[Band, dict]:
    """The band at a window that starts at `since` on this host's clock, from the readings of the global clock
    (`zone`; `transferwindow.zone_clock_of`), with the record of the clock: `since` moved by the least offset the
    readings allow (each one's offset less its half round trip) less `allowance` to `since` moved by the greatest (each
    one's plus it) plus it, in whole seconds (`transferwindow.Edges.opening`). A finish time is stamped on the global
    clock, which Onezone's endpoint reads, and the allowance covers what lies between (the module docstring has the
    source); the `Date` headers place nothing and are recorded as a cross-check.

    A reader walks down to `since` less `allowance` and `STOP_MARGIN_S` on this host's clock, so the band must not open
    below that cutoff, which a global clock more than the margin behind would make it do, leaving jobs that may be the
    window's past the stop. Raise Unreadable, naming the clock, when it would, and when a reading could not be read,
    since the band cannot then be placed. The margin decides where reading stops, never what counts."""
    seen = [reading for reading in zone if reading is not None]
    if not seen or len(seen) < len(zone):
        raise Unreadable(ZONE_UNREAD)
    low, high = min(r.low for r in seen), max(r.high for r in seen)
    first, last = Edges(Window(since, since), low, high, allowance).opening
    if first < since - allowance - STOP_MARGIN_S:
        raise Unreadable(ZONE_BEHIND)
    band = Band(int(first), int(last))
    clock = {
        "zone_least_offset_s": round(low, 3),
        "zone_greatest_offset_s": round(high, 3),
        "zone_half_trip_s": round(max(r.half_trip for r in seen), 3),
        "zone_date_offsets_s": span([r.date_offset for r in seen]),
        "allowance_s": allowance,
        "band": [band.first, band.last],
    }
    return band, clock


__all__ = [
    "EARLIER",
    "INSIDE",
    "STOP_MARGIN_S",
    "UNCERTAIN",
    "ZONE_BEHIND",
    "ZONE_UNREAD",
    "Band",
    "OrderBroken",
    "Walk",
    "newest_first",
    "start_band",
]
