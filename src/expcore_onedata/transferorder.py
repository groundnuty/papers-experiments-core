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
have run on into the window. A reader thus reads its window and at most one page past it, whatever the space's
history, and its bound counts the jobs it keeps, never the history. A window that truly holds more than the bound is
unknown, as before, never cut. The waiting and ongoing listings are not walked: they are few, and any job in them
matters, so the readers read them to their end.

**The check.** Every finish time read must be no later than the one before it, on a page and across pages; equal times
are allowed, since the listing orders one second's jobs by id. The page that holds the stop is read to its end for the
check, so an older job listed out of place ahead of newer ones, which would end the walk early, is caught there. A
time later than the one before it raises `OrderBroken`, an `Unreadable`: the reader fails closed, naming the
assumption, and never stops early on a listing it cannot trust. A record that gives no finish time neither stops the
walk nor takes part in the check, and is kept; a reader that needs the time says so in `finish`.

**Never** a page token built by hand to start at a time: the token's form is undocumented. The walk starts at the
first page and follows the tokens the platform gives.

`STOP_MARGIN_S` widens a reader's cutoff below its window's start (and below the clock-skew allowance it adds): it is
how far behind this host a provider's clock may run before the probes fail closed (`transferlog.py`), and it keeps the
second-level times of the listing clear of rounding at the edge.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from expcore_onedata.transferrecord import Unreadable

STOP_MARGIN_S = 10.0  # seconds a reader's cutoff lies below its window's start, beyond the clock-skew allowance


class OrderBroken(Unreadable):
    """The ended listing is not newest first by finish time, the order a reader of a window rests on."""


@dataclass(frozen=True)
class Walk:
    """What a walk of the ended listing found: each kept job's id with what `read` gave for it, newest first; the
    first finish time read (the newest ended job's, in the window or not; None for an empty listing); whether it
    stopped at a job that ended before the cutoff, so that older ones exist; and how many pages it read."""

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
    is later than the one before it. `where` starts every message."""
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
        if stopped or token is None:
            return Walk(kept, newest, stopped, pages)
    raise Unreadable(f"{where}the ended listing did not end within {max_pages} pages")


__all__ = ["STOP_MARGIN_S", "OrderBroken", "Walk", "newest_first"]
