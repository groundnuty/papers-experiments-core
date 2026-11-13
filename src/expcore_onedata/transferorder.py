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

**The check.** Every finish time read must be no later than the one before it, on a page and across pages; equal times
are allowed, since the listing orders one second's jobs by id. The page that holds the stop is read to its end for the
check, so an older job listed out of place ahead of newer ones on that page, which would end the walk early, is caught
there. One that fills the page alone, or ends it, is caught past it: when the stop's page is not the last, the walk
reads the next page and its first job's record, and that job must have finished no later than the last finish read.
The check costs one page more and, as a rule, one record (a record without a finish time passes the check to the job
after it on that page), and it is the only read past the stop's page; the jobs beyond that first one rest on the order.
A time later than the one before it raises `OrderBroken`, an `Unreadable`: the reader fails closed, naming the
assumption, and never stops early on a listing it cannot trust. A record that gives no finish time neither stops the
walk nor takes part in the check, and is kept; a reader that needs the time says so in `finish`.

**Never** a page token built by hand to start at a time: the token's form is undocumented. The walk starts at the
first page and follows the tokens the platform gives.

`STOP_MARGIN_S` widens a reader's cutoff below its window's start (and below the clock-skew allowance it adds): it is
how far behind this host a provider's clock may run before the probes fail closed (`transferlog.py`), and it keeps the
second-level times of the listing clear of rounding at the edge.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore_onedata.transferrecord import Unreadable
from expcore_onedata.transferwindow import Edges, Window

STOP_MARGIN_S = 10.0  # seconds a reader's cutoff lies below its window's start, beyond the clock-skew allowance


class OrderBroken(Unreadable):
    """The ended listing is not newest first by finish time, the order a reader of a window rests on."""


@dataclass(frozen=True)
class Walk:
    """What a walk of the ended listing found: each kept job's id with what `read` gave for it, newest first; the
    first finish time read (the newest ended job's, in the window or not; None for an empty listing); whether it
    stopped at a job that ended before the cutoff, so that older ones exist; and how many pages it read, the one the
    check past the stop reads included."""

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
    `cutoff`, keeping every other job read.

    `page(token)` reads one page (None: the first) and returns its ids and the next token, None on the last; `read`
    reads the records of a few ids in their order, `chunk` at a time; `finish` gives a record's finish time, None when
    it has none, and `ended` whether the record says the job ended (every record, by default). Past `bound` kept jobs,
    or `max_pages` pages without an end, the listing is unknown: raise Unreadable. Raise OrderBroken when a finish time
    is later than the one before it, the first job past the stop's page included, which the walk reads for that check
    alone (one page more than `max_pages` at most). `where` starts every message."""
    if bound < 1 or max_pages < 1 or chunk < 1:
        raise ValueError("a walk keeps at least one job, reads at least one page and one record at a time")
    kept: dict[str, Any] = {}
    newest = last = None
    stopped = False
    token: str | None = None
    for pages in range(1, max_pages + 1):
        ids, token = page(token)
        for start in range(0, len(ids), chunk):
            part = ids[start : start + chunk]
            for tid, record in zip(part, read(part), strict=True):
                moment = finish(record)
                if moment is not None:
                    if last is not None and moment > last:
                        raise OrderBroken(
                            f"{where}the ended listing is not newest first: a job's finishTime is later than that of "
                            "the job listed before it, and reading only a window assumes that order; the listing is "
                            "unknown, not cut"
                        )
                    last = moment
                    newest = moment if newest is None else newest
                    if moment < cutoff and ended(record):
                        stopped = True
                        continue  # ended before the window: the walk ends with this page, which is read for the check
                kept[tid] = record
                if len(kept) > bound:
                    raise Unreadable(
                        f"{where}the ended listing holds more than {bound} transfers in the window: the listing is "
                        "unknown, not cut"
                    )
        if stopped and token is not None and last is not None:
            _check_past_stop(page, read, finish, token, last, where)
            return Walk(kept, newest, stopped, pages + 1)
        if stopped or token is None:
            return Walk(kept, newest, stopped, pages)
    raise Unreadable(f"{where}the ended listing did not end within {max_pages} pages")


def _check_past_stop(
    page: Callable[[str | None], tuple[list[str], str | None]],
    read: Callable[[Sequence[str]], Sequence[Any]],
    finish: Callable[[Any], float | None],
    token: str,
    last: float,
    where: str,
) -> None:
    """Raise OrderBroken when the first job past the stop, on the page after `token`, finished later than `last`, the
    last finish read: the jobs past the stop are taken to be older and never read, so an older job alone ahead of newer
    ones would otherwise end the walk unseen. The page's records are read one at a time, in their order, until one
    gives a finish time, so as a rule the check costs one page and one record; none that gives one passes."""
    ids, _ = page(token)
    for tid in ids:
        moment = finish(read([tid])[0])
        if moment is None:
            continue  # no time to compare: the next job on the page is the one checked
        if moment > last:
            raise OrderBroken(
                f"{where}the ended listing is not newest first: the first job past the page that ends the walk "
                "finished later than the last job read on it, and reading only a window assumes that order; the "
                "listing is unknown, not cut"
            )
        return


def window_opens(
    since: float, offsets: Mapping[str, Sequence[float | None]], allowance: float
) -> tuple[int, dict[str, float]]:
    """Where a window that starts at `since` on this host's clock opens on the providers' clocks, from the offsets the
    Date headers of their listing answers gave (`offsets`, by provider label; `transferwindow.offset_of`), with the
    record of the clock. It opens at `since` moved by the least offset, in whole seconds as the listing's times are:
    a record's times are on the clock of the provider that wrote them, so the earliest any provider puts it.

    A reader walks down to `since` less `allowance` and `STOP_MARGIN_S` on this host's clock, so the check is the
    probes' (`transferlog.py`): the window widened by the offsets and the allowance (`transferwindow.Edges`) must not
    open below that cutoff, which a provider clock more than the margin behind would make it do, leaving jobs of the
    window past the stop. Raise Unreadable, naming the clock, when it would, and when an answer's Date could not be
    read, since the window cannot then be placed. The margin decides where reading stops, never what counts."""
    if not offsets:
        raise ValueError("a window is placed from one provider's listing answers at least")
    unread = [label for label, samples in offsets.items() if not samples or None in samples]
    if unread:
        raise Unreadable(
            f"{', '.join(unread)}: a listing answer carried no Date header that could be read, so the provider's clock "
            "is unknown and the window cannot be placed on it"
        )
    seen = [offset for samples in offsets.values() for offset in samples if offset is not None]
    low, high = min(seen), max(seen)
    if Edges(Window(since, since), low, high, allowance).first < since - allowance - STOP_MARGIN_S:
        raise Unreadable(
            f"a provider's clock runs more than {STOP_MARGIN_S:.0f} s behind this host's, beyond the margin the ended "
            "listing is read back by, so the window may hold jobs past where the reading stopped"
        )
    opens = math.floor(since + low)
    return opens, {"least_offset_s": round(low, 3), "greatest_offset_s": round(high, 3), "window_opens": opens}


__all__ = ["STOP_MARGIN_S", "OrderBroken", "Walk", "newest_first", "window_opens"]
