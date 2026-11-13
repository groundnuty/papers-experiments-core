# Provenance: new
"""The run's window on the transfer log, and the split of a provider's gained bytes into transfer bytes and the rest.

**The window.** The run's window runs from its start to just after the block maps were read at its end, on this
host's clock. A job's times are stamped on the platform's global clock (`transferorder.py` has the source), so the
window is moved onto it (`Edges`): Onezone's own reading of that clock, from its public current-time endpoint, gives
its offset from this host (`ZoneClock`: the global time less the midpoint of this host's clock around the read, with
half the round trip as its uncertainty), and the window is widened by the least and the greatest offset the reads allow
(each read's offset less and plus its half round trip) and by `[placement] clock_skew_allowance_s` (5 s by default) on
each side, which covers a provider's synchronisation error to that clock. A job is in the window when its `scheduleTime`
lies in the widened one; the schedule time is the one every job has, a waiting one included, and it says when someone
(the harness, the agent through the tool plane, a QoS requirement) started it. A job scheduled before the widened
window that had not ended by its start (listed as waiting or ongoing, ended at or after it, or with no finish time) is
not in the record, but it may have placed bytes during the run, so it is counted as carried over.

Each edge of the window has a band of the same width on either side of it. A job whose schedule, start or finish time
falls in a band may lie on either side of the edge, so it is ambiguous. A read of the global clock that fails or whose
answer cannot be read leaves its offset unknown, and with it the window on its clock. The `Date` header of the same
answer is Onezone's answering node's system clock, which a multi-node Onezone's global clock may differ from by a bias:
it places nothing and is recorded beside the offset as a cross-check (`ZoneClock.date_offset`).

**The split.** `split_gained` gives, per tracked receiving provider, `transfer_bytes_in`: the `bytesReplicated` of every
job in the window that replicates into it (a replication's or a migration's `replicatingProviderId`; an eviction moves
nothing in), and `on_demand_lower_bound` = `gained_bytes` - `transfer_bytes_in`, floored at zero. `gained_bytes` is
net placement growth from the block maps (`probes.py`), not bytes on the wire. The bound is a lower bound on the bytes
the provider came to hold that no transfer placed (fetched on demand by a read through it), if a job's byte counter
does not under-report what it placed (O01: a job's report is a report). The sum counts every file a job moved, tracked
or not, so the bound only errs low; `difference` keeps the unfloored value so that a negative stays visible.

**A bound is known only when every input to it is established.** Otherwise it is None and `unknown` lists the reasons:
`transfer_not_ended` (a job into the provider had not ended at run end, so its counter was not final),
`transfer_carried_over` (a job into it from before the run was still running when the run began),
`listings_disagree` (the tracked providers do not list and answer a job into it alike, `transferlog.py`),
`listing_not_settled` (a job into it still changed between the last two observations of the listing),
`transfer_near_window_edge` (a job into it has a time in a band at an edge of the window), and
`clock_offset_unknown` (the global clock could not be read, which leaves every bound unknown).
"""

import math
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from expcore_onedata.transferrecord import Job

NOT_ENDED = "transfer_not_ended"
# The record states after which a job moves no more bytes; any other, or none, leaves its counters open.
FINAL_STATES = frozenset({"completed", "failed", "cancelled", "skipped"})
CARRIED_OVER = "transfer_carried_over"
DISAGREE = "listings_disagree"
NOT_SETTLED = "listing_not_settled"
NEAR_EDGE = "transfer_near_window_edge"
CLOCK_UNKNOWN = "clock_offset_unknown"
HARNESS_UNLISTED = "harness_transfer_not_listed"
LISTING_DROPPED = "transfer_left_listing"


def offset_of(date: str | None, host_now: float) -> float | None:
    """Return a server's system-clock offset from this host, in seconds, from the `Date` header of an answer that
    arrived at `host_now`: positive when the server's clock is ahead. None when the header is missing or cannot be
    read. It gives a provider's or Onezone's system clock, never the global clock a job's times are stamped on."""
    if not isinstance(date, str):
        return None
    try:
        moment = parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None or moment.tzinfo is None:
        return None  # a date without a zone is not a moment
    return moment.timestamp() - host_now


@dataclass(frozen=True)
class ZoneClock:
    """One reading of Onezone's global clock, the one a job's times are stamped on.

    `offset` is the global time less the midpoint of this host's clock around the read, in seconds, positive when the
    global clock is ahead; the stamp was made somewhere between the two host readings, so the true offset lies within
    `half_trip`, half the round trip, of it (`low` to `high`). `date_offset` is what the answer's `Date` header gives,
    Onezone's answering node's system clock against the midpoint (None when unreadable): a cross-check, never a
    place."""

    offset: float
    half_trip: float
    date_offset: float | None = None

    @property
    def low(self) -> float:
        return self.offset - self.half_trip

    @property
    def high(self) -> float:
        return self.offset + self.half_trip


def zone_clock_of(answer: Any, date: str | None, sent: float, received: float) -> ZoneClock | None:
    """Read Onezone's public current-time answer (`{"timeMillis": N}`, the global clock in epoch milliseconds) that
    was asked at `sent` and received at `received` on this host's clock, with its `Date` header. None when the answer
    holds no `timeMillis` that is a non-negative number, or this host's clock moved backwards during the read: the
    clock is then unknown, never guessed. When the read was retried, `sent` to `received` spans the retries and their
    pauses, so `half_trip` grows and the bands widen, the safe way."""
    millis = answer.get("timeMillis") if isinstance(answer, dict) else None
    if isinstance(millis, bool) or not isinstance(millis, int | float) or not math.isfinite(millis) or millis < 0:
        return None
    if not (math.isfinite(sent) and math.isfinite(received)) or received < sent:
        return None
    midpoint = (sent + received) / 2
    return ZoneClock(millis / 1000 - midpoint, (received - sent) / 2, offset_of(date, midpoint))


def span(samples: Sequence[float | None]) -> list[float] | None:
    """The least and greatest of the readable samples, rounded to the millisecond; None when none can be read."""
    seen = [round(sample, 3) for sample in samples if sample is not None]
    return [min(seen), max(seen)] if seen else None


@dataclass(frozen=True)
class Window:
    """The run's window in epoch seconds of this host's clock: from its start to just after its end's readings."""

    start: float
    end: float

    def to_json(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class Edges:
    """The run's window on the clock a job's times are stamped on, the global one: widened by the least (`low`) and
    greatest (`high`) of its offsets the reads allow (`ZoneClock.low`, `.high`) and the allowance, with a band of that
    width on either side of each edge."""

    window: Window
    low: float
    high: float
    allowance: float

    def _band(self, edge: float) -> tuple[float, float]:
        return math.floor(edge + self.low - self.allowance), math.ceil(edge + self.high + self.allowance)

    @property
    def opening(self) -> tuple[float, float]:
        """The band at the window's start, both ends included: a provider time in it may lie on either side of the
        start, one before it lies before the start, and one after it lies after the start."""
        return self._band(self.window.start)

    @property
    def first(self) -> float:
        """The earliest provider time in the widened window."""
        return self.opening[0]

    @property
    def last(self) -> float:
        """The latest provider time in the widened window."""
        return self._band(self.window.end)[1]

    def holds(self, moment: float) -> bool:
        """Tell whether a provider's time lies in the widened window."""
        return self.first <= moment <= self.last

    def near_edge(self, job: Job) -> bool:
        """Tell whether one of the job's times falls in the band at an edge, where it may lie on either side."""
        bands = (self._band(self.window.start), self._band(self.window.end))
        times = (t for t in (job.scheduled, job.started, job.finished) if t)
        return any(low <= t <= high for t in times for low, high in bands)

    def carried_over(self, job: Job) -> bool:
        """Tell whether a job scheduled before the widened window may have gone on into it."""
        ended_before = (
            job.listed_as == "ended"
            and job.state in FINAL_STATES  # the record must say it ended too, or its counters are still open
            and job.finished is not None
            and 0 < job.finished < self.first
        )
        return job.scheduled < self.first and not ended_before

    def bears_on(self, job: Job) -> bool:
        """Tell whether a job can have placed bytes in the run: in the window, carried over into it, or at its edge."""
        return self.holds(job.scheduled) or self.carried_over(job) or self.near_edge(job)

    def to_json(self) -> dict[str, float]:
        return {"first": self.first, "last": self.last, "allowance_s": self.allowance}


def split_gained(
    gained: Mapping[str, int],
    jobs: Iterable[Job],
    name_of: Callable[[str], str | None],
    *,
    flagged: Iterable[tuple[str, str]] = (),
    everywhere: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Split each tracked provider's gained bytes into what the jobs in the window moved into it and a lower bound on
    the rest. `name_of` maps a provider id to the key `gained` uses for it (the probes pass its position label,
    `providers[i]`); a job into no tracked provider counts for none.
    `flagged` holds `(target id, reason)` of jobs that leave their target's bound unknown, and `everywhere` the
    reasons that leave every bound unknown."""
    into = dict.fromkeys(gained, 0)
    reasons: dict[str, set[str]] = {provider: set(everywhere) for provider in gained}
    for job in jobs:
        name = name_of(job.target_id) if job.target_id else None
        if name in into:
            into[name] += job.bytes_replicated or 0
            if job.listed_as != "ended" or job.state not in FINAL_STATES:  # both must say its counters are final
                reasons[name].add(NOT_ENDED)
    for target_id, reason in flagged:
        name = name_of(target_id)
        if name in reasons:
            reasons[name].add(reason)
    out: dict[str, dict[str, Any]] = {}
    for provider, bytes_gained in gained.items():
        difference = bytes_gained - into[provider]
        out[provider] = {
            "gained_bytes": bytes_gained,
            "transfer_bytes_in": into[provider],
            "difference": difference,
            "on_demand_lower_bound": None if reasons[provider] else max(0, difference),
            "unknown": sorted(reasons[provider]) or None,
        }
    return out
