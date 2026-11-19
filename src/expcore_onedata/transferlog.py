# Provenance: new
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:269-281 (paged until nextPageToken is empty)
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:226-245 (a fresh transfer lags in the listing)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/otf_rest_test.py:50-56 (listed by state)
"""The transfers of the space in a run's window, whoever started them, read from the space's transfer listing (O08):
every one that ran, and of those cancelled before they started only the ones the walk reaches (below).

At run end `TransferLog` observes every tracked provider: it lists the space's transfers there, `GET
/spaces/{space}/transfers` with `state` waiting, ongoing and ended in that order (a job only moves forward, so one that
moves on while the lists are read is found in a later one) and `limit`, following `nextPageToken` until it is null
(a page without it is refused, O26), and reads each id it listed with `GET /transfers/{id}` on that same provider.
Every request goes through the probes' own probe (`probes.run_probe`): a real answer is final and only a missing one
is retried, a bounded number of times.
`transferrecord.py` reads the answers and says which of their fields are confirmed.

**The run's window, not the space's history (O32).** Ended jobs are never removed, so the space's history grows with
every run and reset. The waiting and ongoing listings are read to their end, since they are few and any job in them
can bear on the run. The ended listing, newest first by finish time, is read down to the first job whose record says
it ended (a final state) before the cutoff, `ended_since`: the window's start less `[placement]
clock_skew_allowance_s` and `transferorder.STOP_MARGIN_S`, on this host's clock and fixed for the whole reading, so
every observation reads alike. Such a job can be neither in the window, nor carried over into it, nor near its edge;
one that finished before the cutoff but whose record does not say it ended is read, and may be carried over; if its
record says it ended by the next observation, the walk stops at it and it leaves the listing, which flags every bound
(`transfer_left_listing`), so a record that catches up errs closed. The order is checked as the listing is read, and
a listing out of that order is unknown (`transferorder.py`). A job's times are stamped on the platform's global clock,
not on the system clock a provider's or Onezone's Date header gives (`transferorder.py` has the source), so the window
is placed on that clock, which each observation reads from Onezone's public current-time endpoint (`GET
/provider/public/get_current_time`, no credential, answered `timeMillis`): the answer less the midpoint of this host's
clock around the read is the clock's offset from this host, and half the round trip is how far that may be off. The
window on that clock must not open before the cutoff: a global clock more than the margin behind this host's leaves
the listing unknown, naming the clock. The band at each edge of the window, from the least offset the readings allow
less the allowance to the greatest plus it, is where a time may lie on either side: a job with a time in one is
recorded with `near_window_edge` and leaves its target's bound unknown (`transfer_near_window_edge`), never part of a
known figure. A reading that fails or cannot be read leaves the clock unknown, and every bound with it
(`clock_offset_unknown`). The `Date` headers, of Onezone's answer and of the providers', are recorded beside the
offset, a diagnostic that places nothing. A job cancelled before it started is given no finish time, so the ended
listing puts it after every job that has one and the walk stops at it: it and what follows are never read as part of
the window, so `records` and `listed` never hold it and no long tail of such cancellations can exhaust the bound, and
neither counts every transfer that ended in the window, only those with a finish time. It moved no bytes, so no figure
of bytes misses it.

**One provider's observation at a time.** A job's state and counters are always taken from one provider's listing and
record together, never one provider's state with another's counters. A provider lists a job only after a lag (a
source waits up to 60 s for a fresh one to show, the second `merged:` line), and a job posted on one provider reaches
another's listing later still, so every tracked provider is observed; a tracked provider must therefore support the
space. The record keeps the observation of the first tracked provider that lists the job (`observed_on`). When the
tracked providers do not all list the job with the same list, state and counters (`listings_agree` false), the job's
target's bound is unknown, `listings_disagree`, provided the job can bear on the run (in its window or carried over).

**Settled before it is believed (O07).** One observation can miss a job the listing has not shown yet. So the log
observes every tracked provider, waits `[placement] transfer_settle_s` (30 s by default, which must be at least the
listing's lag) and observes again, until two observations in a row are the same (the same ids, each listed alike with
the same state and counters on each provider), within `transfer_settle_polls` observations (4 by default). The section
records `settled` and `polls`. When the bound is reached first, every job that still changed between the last two
observations leaves its target's bound unknown, `listing_not_settled`. The wait happens at run end, outside the agent's
time. A job that lags for longer than the settle time and also stays out of the next observation is not seen; the
setting is what bounds that.

**The harness's own jobs (O08).** A job the provisioner scheduled that the accepted listing does not show proves the
listing incomplete, and what else it misses could be into any provider: every bound is unknown, `harness_unlisted`
counts them. The probes hand over only the jobs that bear on the run (`Provisioner.transfers_from`): those scheduled
since its start, and earlier ones unless the log noted, before the start, their end with a finish time before the
cutoff (`Provisioner.note_ended`, written when a wait of the provisioner's own saw them end, as a reset's eviction
batches and a replication's do): the proof `_ended_before_cutoff` asks of a job's records, so such a job is absent from
the listing by design, and the log, kept across runs, is not read again job by job. Those it hands over may include
jobs from before the run, and
the ended walk never keeps a job that ended before the cutoff, so for each harness job the listing lacks, its record is
read on every tracked provider. The job is absent by design and flags nothing (`harness_before_window` counts them)
when a record says it ended (a final state) with a finish time after 0 and before the cutoff and no readable record
contradicts that: every final record gives that same finish time. A copy that has not ended lags and contradicts
nothing; a record that cannot be read neither confirms nor contradicts. With no such record, or a final one that gives
another finish time, none, 0, or one at or after the cutoff, the job is missing.

**A deadline for the whole walk** (`[placement] transfer_walk_deadline_s`, 600 s by default): the harness's own records
(`probes.py`), every observation and the reads of the harness's jobs the listing lacks share one bound, counted on the
provisioner's clock from when the probes begin their transfer reads. It is looked at before every request, each of
which the client bounds itself, so a walk that has run past it sends nothing more: the listing is `probe_ok` false with
`reason` `deadline` and no records, never no transfers, and a record not read is a failed probe with that reason. Two
federations' walks once ran for 41 to 47 minutes. The section records the bound (`deadline_s`) and each observation's
seconds (`observations_s`), a failed walk's so far included.

**Fail closed.** A page or record that cannot be read, or one not in the documented shape (`transferrecord.py`), makes
the whole listing `probe_ok` false with the reason and no records, never fewer or no transfers. A listing longer than
its bound (`MAX_LISTED` distinct ids read: the waiting, the ongoing and the ended ones down to the cutoff, never the
older history; or a token that keeps coming back) is unknown in the same way, not truncated.
`transferwindow.py` places the jobs in the run's window, moved onto the global clock by the readings of it (recorded
under `clock`), and splits a provider's gained bytes.
"""

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from expcore_onedata.client import ZONE_TIME_PATH, OnezoneClient
from expcore_onedata.provision import Provisioner
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.transferorder import STOP_MARGIN_S, ZONE_BEHIND, newest_first
from expcore_onedata.transferrecord import STATES, UNDECLARED, Job, Unreadable, job_of, page_of, record_of, state_of
from expcore_onedata.transferwindow import (
    CARRIED_OVER,
    CLOCK_UNKNOWN,
    DISAGREE,
    FINAL_STATES,
    HARNESS_UNLISTED,
    LISTING_DROPPED,
    NEAR_EDGE,
    NOT_SETTLED,
    Edges,
    Window,
    ZoneClock,
    offset_of,
    span,
    zone_clock_of,
)

PAGE_LIMIT = 1000  # ids asked for per page; the listing takes 1 to 1000
MAX_LISTED = 2000  # distinct ids of the window read before the listing is unknown: each one costs a read of its record
ACTIVE = ("waiting", "ongoing")  # the listings read to their end; the ended one is read down to the window
DEADLINE = "deadline"  # the reason of a walk that ran past `[placement] transfer_walk_deadline_s`


class WalkDeadline(Exception):
    """The transfer walk ran past its deadline: nothing more is sent, and the listing is unknown. Deliberately not an
    `Unreadable`, which a check of one harness job takes for one provider's unread record and goes on past."""


def deadline_at(provisioner: Provisioner, started: float | None = None) -> float:
    """The instant, on the provisioner's clock, past which a walk begun at `started` (by default now) reads no more."""
    began = provisioner.clock() if started is None else started
    return began + provisioner.settings.transfer_walk_deadline_s


def require_time(provisioner: Provisioner, deadline: float) -> None:
    """Raise WalkDeadline once the provisioner's clock is past `deadline`."""
    if provisioner.clock() > deadline:
        raise WalkDeadline(
            f"the transfer walk ran past its deadline of {provisioner.settings.transfer_walk_deadline_s:g} s "
            "([placement] transfer_walk_deadline_s): the listing is unknown"
        )


class Probed(Protocol):
    """What a probe of the probes returns (`probes.ProbeResult`): whether it worked, its value, and why not."""

    ok: bool
    value: Any
    error: str | None


@dataclass(frozen=True)
class Reading:
    """What the transfer log found: the snapshot's section, the jobs in the window (None when the listing is unknown),
    `(target id, reason)` of the jobs that leave their target's bound unknown, and the reasons that leave every
    bound unknown."""

    section: dict[str, Any]
    inside: list[Job] | None
    flagged: list[tuple[str, str]] = field(default_factory=list)
    everywhere: frozenset[str] = frozenset()


Observation = dict[str, dict[str, Job]]  # provider name -> transfer id -> the job as that provider has it


def changed_between(previous: Observation, current: Observation) -> list[Job]:
    """Return every reading of a job that two observations do not share: one provider listed it in one of them only,
    or with another list, state or counters. None of them means the two are the same observation."""
    changed: list[Job] = []
    for provider in current.keys() | previous.keys():
        before, after = previous.get(provider, {}), current.get(provider, {})
        for tid in before.keys() | after.keys():
            old, new = before.get(tid), after.get(tid)
            if old is None or new is None or old.observed() != new.observed():
                changed += [job for job in (old, new) if job is not None]
    return changed


class TransferLog:
    """Reads the space's transfer listing and each listed record through the probes' own probe, at run end."""

    def __init__(
        self,
        client: OnezoneClient,
        provisioner: Provisioner,
        providers: Sequence[str],
        probe: Callable[[Callable[[], Any]], Probed],
        *,
        sleep: Callable[[float], None],
        now: Callable[[], float],
        page_limit: int = PAGE_LIMIT,
        max_listed: int = MAX_LISTED,
    ) -> None:
        """List on `providers` (the tracked ones), `page_limit` ids a page; past `max_listed` ids it is unknown. `now`
        is this host's epoch clock, the one the run's window is on."""
        if not 1 <= page_limit <= PAGE_LIMIT or max_listed < 1:
            raise ValueError(f"a page holds 1 to {PAGE_LIMIT} ids and the listing's bound is at least one")
        self._client, self._provisioner, self._providers, self._run_probe = client, provisioner, list(providers), probe
        self._page_limit, self._max_listed, self._sleep, self._now = page_limit, max_listed, sleep, now
        self._pages = 0
        self._offsets: dict[str, list[float | None]] = {}  # provider -> each listing answer's clock offset, or None
        self._zone: list[ZoneClock | None] = []  # the global clock each observation read from Onezone, or None
        self._seen: set[str] = set()  # every job id any observation of the current reading showed
        self._cutoff = 0.0  # the earliest finish time the ended listing is read down to, this host's clock
        self._deadline = math.inf  # the walk's deadline, on the provisioner's clock (`deadline_at`)
        self._durations: list[float] = []  # each observation's seconds, on the provisioner's clock

    def _probe(self, read: Callable[[], Any]) -> Probed:
        """`read` through the probes' own probe, each attempt sent only while the walk is within its deadline
        (`require_time`, which raises WalkDeadline past it and so ends the walk)."""

        def within() -> Any:
            require_time(self._provisioner, self._deadline)
            return read()

        return self._run_probe(within)

    def _dated_page(self, path: str, provider: str, params: Mapping[str, Any]) -> Any:
        """Read one page of a provider's listing and keep the provider's system-clock offset its Date header gives, a
        diagnostic, since a job's times are on the global clock."""
        answer, date = self._client.get_dated(path, provider=provider, params=params, retry=ONE_ATTEMPT)
        self._offsets.setdefault(provider, []).append(offset_of(date, self._now()))
        return answer

    def _zone_dated(self) -> Any:
        """Read Onezone's public current-time endpoint and keep the global clock's offset from this host, the clock a
        job's times are stamped on, with half the round trip as its uncertainty, or None when the answer cannot be read
        (the clock is then unknown)."""
        sent = self._now()
        answer, date = self._client.get_dated(ZONE_TIME_PATH, retry=ONE_ATTEMPT, anonymous=True)
        self._zone.append(zone_clock_of(answer, date, sent, self._now()))
        return answer

    def _read_zone(self) -> None:
        """Read the global clock through the probe; a read that fails leaves the clock unknown (None), as an answer
        that cannot be read does."""
        if not self._probe(self._zone_dated).ok:
            self._zone.append(None)

    def _page(self, provider: str, state: str, token: str | None) -> tuple[list[str], str | None]:
        """Read one page of a provider's listing in one state through the probe; raise Unreadable when it fails."""
        path = f"/spaces/{self._provisioner.space_id}/transfers"
        params = {"state": state, "limit": self._page_limit, **({"page_token": token} if token else {})}
        found = self._probe(lambda: self._dated_page(path, provider, params))
        self._pages += 1
        if not found.ok:
            where = self._client.provider_label(provider)
            raise Unreadable(f"{where}: a page of the {state} listing could not be read: {found.error}")
        return page_of(found.value)

    def _record(self, provider: str, tid: str) -> Any:
        """Read one job's record on a provider through the probe; raise Unreadable when it fails."""
        found = self._probe(lambda: self._client.get(f"/transfers/{tid}", provider=provider, retry=ONE_ATTEMPT))
        if not found.ok:
            raise Unreadable(f"a transfer's record could not be read: {found.error}")
        return found.value

    def _listed_on(self, provider: str) -> tuple[dict[str, str], dict[str, Any]]:
        """List one provider's transfers: the waiting and ongoing listings to their end, and the ended one newest first
        down to the cutoff (`transferorder.newest_first`), each ended job's record read in its turn. Return each id with
        the furthest state that provider listed it in, and the records the ended walk read. Raise Unreadable when that
        fails, when the order does not hold, or past the bound, which counts these jobs alone."""
        listed: dict[str, str] = {}
        max_pages = self._max_listed // self._page_limit + 1
        where = self._client.provider_label(provider)
        for state in ACTIVE:
            token: str | None = None
            for _ in range(max_pages):
                ids, token = self._page(provider, state, token)
                listed.update((tid, max(listed.get(tid, state), state, key=STATES.index)) for tid in ids)
                if len(listed) > self._max_listed:
                    raise Unreadable(f"{where}: more than {self._max_listed} transfers are waiting or ongoing")
                if token is None:
                    break
            else:
                raise Unreadable(f"{where}: the {state} listing did not end within {max_pages} pages")
        walk = newest_first(
            lambda token: self._page(provider, "ended", token),
            lambda ids: [self._record(provider, tid) for tid in ids],
            _finished,
            cutoff=self._cutoff,
            bound=self._max_listed,
            max_pages=max_pages,
            ended=lambda body: state_of(body) in FINAL_STATES,  # one whose record has not ended may run into the window
            where=f"{where}: ",
        )
        listed.update(dict.fromkeys(walk.kept, "ended"))
        if len(listed) > self._max_listed:
            raise Unreadable(f"{where}: more than {self._max_listed} transfers in the window: unknown, not cut")
        return listed, walk.kept

    def _observe(self) -> Observation:
        """Read the global clock, then list every tracked provider and read each id it listed on that same provider (an
        ended one's record is the one its walk read); raise Unreadable when a listing or a record fails."""
        self._read_zone()
        observation: Observation = {}
        for provider in self._providers:
            listed, ended = self._listed_on(provider)
            jobs: dict[str, Job] = {}
            for tid, state in listed.items():
                body = ended[tid] if tid in ended else self._record(provider, tid)
                jobs[tid] = job_of(tid, state, body)
            observation[provider] = jobs
        if len(set().union(*observation.values())) > self._max_listed:
            raise Unreadable(f"more than {self._max_listed} transfers in the window: the listing is unknown, not cut")
        return observation

    def _timed_observe(self) -> Observation:
        """One observation (`_observe`), its seconds kept for the snapshot (`observations_s`), a failed one's too."""
        began = self._provisioner.clock()
        try:
            return self._observe()
        finally:
            self._durations.append(round(self._provisioner.clock() - began, 3))

    def _label(self, provider_id: str | None) -> str | None:
        """Name a provider id by its declared provider's position label, or `undeclared`; None stays None."""
        if provider_id is None:
            return None
        name = self._provisioner.provider_named(provider_id)
        return UNDECLARED if name is None else self._client.provider_label(name)

    def _merged(self, observation: Observation) -> list[tuple[str, Job, list[Job]]]:
        """One entry per listed id: the provider whose observation stands for it, that job, and every provider's."""
        merged = []
        for tid in dict.fromkeys(t for provider in self._providers for t in observation[provider]):
            views = [(p, observation[p][tid]) for p in self._providers if tid in observation[p]]
            merged.append((views[0][0], views[0][1], [job for _, job in views]))
        return merged

    def _settled(self) -> tuple[Observation, list[Job], int]:
        """Observe, wait the settle time and observe again, until two observations in a row are the same or the bound
        on observations is reached. Return the last observation, the readings that still changed between the last two
        (none when the listing settled), and how many observations were made. Raise Unreadable when one fails."""
        settings = self._provisioner.settings
        previous = self._timed_observe()
        self._seen = set(_ids(previous, self._providers))
        changed: list[Job] = []
        for polls in range(2, settings.transfer_settle_polls + 1):
            if self._provisioner.clock() + settings.transfer_settle_s > self._deadline:
                raise WalkDeadline(  # the next observation could only begin past the deadline: none is waited for
                    f"the transfer walk's next observation would begin past its deadline of "
                    f"{settings.transfer_walk_deadline_s:g} s ([placement] transfer_walk_deadline_s): the listing is "
                    "unknown"
                )
            self._sleep(settings.transfer_settle_s)
            current = self._timed_observe()
            self._seen |= _ids(current, self._providers)
            changed = changed_between(previous, current)
            if not changed:
                return current, [], polls
            previous = current
        return previous, changed, settings.transfer_settle_polls

    def read(
        self,
        window: Window | None,
        file_ids: Mapping[str, str],
        harness: Collection[str],
        *,
        deadline: float | None = None,
    ) -> Reading:
        """Return the snapshot's record of the space's transfers in the window, with the jobs in it and those that leave
        a bound unknown. `file_ids` labels a tracked file's id, and `harness` holds the ids the provisioner scheduled,
        before the run as well as in it: one the accepted listing does not show leaves every bound unknown
        (`harness_unlisted`), unless its records agree that it ended before the cutoff (`harness_before_window`).
        When the listing is unknown the jobs are None and the section's `probe_ok` is false with the reason. `deadline`
        is the walk's (`deadline_at`, the probes' own, begun before their records); by default one begun now. Past it
        the listing is unknown with `reason` `deadline`."""
        self._deadline = deadline_at(self._provisioner) if deadline is None else deadline
        self._durations = []
        try:
            reading = self._read(window, file_ids, harness)
        except WalkDeadline as error:
            section = {**self._section(window), "pages": self._pages, "error": str(error), "reason": DEADLINE}
            reading = Reading(section, None)
        reading.section["observations_s"] = list(self._durations)  # the section is this reading's own
        return reading

    def _section(self, window: Window | None) -> dict[str, Any]:
        """The section before anything is read: unknown, with the settings it is read under."""
        settings = self._provisioner.settings
        return {
            "probe_ok": False,
            "error": None,
            "window": None if window is None else window.to_json(),
            "providers": [self._client.provider_label(p) for p in self._providers],
            "pages": 0,
            "settle_s": settings.transfer_settle_s,
            "settled": None,
            "polls": None,
            "ended_since": None,
            "listed": None,
            "records": None,
            "carried_over": None,
            "deadline_s": settings.transfer_walk_deadline_s,
            "observations_s": [],
        }

    def _read(self, window: Window | None, file_ids: Mapping[str, str], harness: Collection[str]) -> Reading:
        """`read` within its deadline: raise WalkDeadline past it."""
        self._pages = 0
        section = self._section(window)
        if window is None:
            return Reading({**section, "error": "the run's start was not read, so the run has no window"}, None)
        self._offsets, self._zone = {}, []
        # One cutoff for the whole reading, so that observations compare alike: the window's start on this host's
        # clock, less the allowance and the margin (`transferorder.STOP_MARGIN_S`).
        allowance = self._provisioner.settings.clock_skew_allowance_s
        self._cutoff = section["ended_since"] = window.start - allowance - STOP_MARGIN_S
        try:
            observation, changed, polls = self._settled()
        except Unreadable as error:
            return Reading({**section, "pages": self._pages, "error": str(error)}, None)
        flagged = [(t, NOT_SETTLED) for t in dict.fromkeys(j.target_id for j in changed) if t]
        edges, clock = self._edges(window)
        if edges.first < self._cutoff:
            # The window opens before the cutoff on the global clock: a job of the run could lie past the stop.
            return Reading({**section, "pages": self._pages, "clock": clock, "error": ZONE_BEHIND}, None)
        section.update(settled=not changed, polls=polls, clock=clock)
        reading = self._classified(edges, observation, file_ids, harness, section, flagged)
        # A listing that never settled may still be missing a transfer that has not appeared in any reading, and such a
        # transfer could be into any tracked provider: every bound is then unknown, not only those of the changed jobs.
        everywhere = set() if clock["known"] else {CLOCK_UNKNOWN}
        if changed:
            everywhere.add(NOT_SETTLED)
        listed = _ids(observation, self._providers)
        # A job an earlier observation showed that the accepted one does not: the listing dropped it, so it is not to
        # be trusted to hold every job, and what it dropped could be into any provider.
        if self._seen - listed:
            everywhere.add(LISTING_DROPPED)
            section["left_listing"] = len(self._seen - listed)
        # A transfer the harness itself scheduled that the accepted listing does not show proves the listing
        # incomplete, and what else it misses could be into any provider; but not one whose record says it ended
        # before the cutoff, such as a reset's eviction before the run: the ended walk never keeps such a job, so the
        # listing lacks it by design (O32).
        missing = sorted(set(harness) - listed)
        before = [tid for tid in missing if self._ended_before_cutoff(tid)]
        if before:
            section["harness_before_window"] = len(before)
        if len(missing) > len(before):
            everywhere.add(HARNESS_UNLISTED)
            section["harness_unlisted"] = len(missing) - len(before)
        return Reading(reading.section, reading.inside, reading.flagged, frozenset(everywhere))

    def _ended_before_cutoff(self, tid: str) -> bool:
        """Tell whether the job's records on the tracked providers show it ended before the cutoff: a job the ended
        walk never keeps (`transferorder.newest_first`), whose absence from the listing says nothing of it. Every
        tracked provider's record is read. It did when a record says it ended (a final state) with a finish time after 0
        and before the cutoff and no readable record contradicts that: every final record gives that same finish time.
        A record that is not final is a copy that lags and contradicts nothing; one that cannot be read neither confirms
        nor contradicts. With no final record, or final records that give different finish times, or a finish time
        that is none, 0, or at or after the cutoff, the job stays a harness job the listing does not show."""
        finishes: set[float | None] = set()  # the finish time of every final record read
        for provider in self._providers:
            try:
                body = self._record(provider, tid)
            except Unreadable:
                continue  # this provider's record was not read: it neither confirms the end nor contradicts it
            if state_of(body) in FINAL_STATES:  # a copy that has not ended lags, and contradicts nothing
                finishes.add(_finished(body))
        if len(finishes) != 1:
            return False  # no final record, or final records that do not agree on when the job ended
        (finish,) = finishes
        return finish is not None and 0 < finish < self._cutoff

    def _edges(self, window: Window) -> tuple[Edges, dict[str, Any]]:
        """Move the window onto the global clock, the one a job's times are stamped on, by the readings of it, and
        describe that for the snapshot with the providers' Date offsets beside it, a diagnostic. A reading that could
        not be read, or a read that failed, leaves the clock unknown, and the window is then taken as it is on this
        host's clock."""
        allowance = self._provisioner.settings.clock_skew_allowance_s
        zone = [reading for reading in self._zone if reading is not None]
        known = bool(zone) and len(zone) == len(self._zone)
        edges = (
            Edges(window, min(r.low for r in zone), max(r.high for r in zone), allowance)
            if known
            else Edges(window, 0.0, 0.0, allowance)
        )
        described = {
            self._client.provider_label(provider): _described(self._offsets.get(provider, []))
            for provider in self._providers
        }
        return edges, {"known": known, "zone": _described_zone(self._zone), "offsets": described, **edges.to_json()}

    def _classified(
        self,
        edges: Edges,
        observation: Observation,
        file_ids: Mapping[str, str],
        harness: Collection[str],
        section: dict[str, Any],
        flagged: list[tuple[str, str]],
    ) -> Reading:
        """Place each job in or out of the window, record those in it and flag those that leave a bound unknown."""
        inside, records, carried = [], [], 0
        merged = self._merged(observation)
        for provider, job, views in merged:
            targets = [t for t in dict.fromkeys(v.target_id for v in views) if t]
            agree = len(views) == len(self._providers) and all(v.observed() == job.observed() for v in views)
            if not agree and any(edges.bears_on(v) for v in views):
                flagged += [(t, DISAGREE) for t in targets]
            near = any(edges.near_edge(v) for v in views)
            if near:
                flagged += [(t, NEAR_EDGE) for t in targets]
            # Each provider's reading is placed against the window on its own: readings whose times put the job on
            # different sides (one inside, one before the run, or carried over) leave its targets unknown. A job any
            # reading places in the window is counted, since counting it can only lower the bound.
            placements = {_placement(edges, v) for v in views}
            if len(placements) > 1:
                flagged += [(t, DISAGREE) for t in targets]
            if "carried" in placements:
                carried += 1
                flagged += [(t, CARRIED_OVER) for t in targets]
            if "inside" not in placements:
                continue
            inside.append(job)
            records.append(
                record_of(
                    job,
                    label=self._label,
                    file=file_ids.get(job.file_id or ""),
                    scheduled_by_harness=job.transfer_id in harness,
                    observed_on=self._client.provider_label(provider),
                    listings_agree=agree,
                    near_window_edge=near,
                )
            )
        listed = len(merged)
        section.update(probe_ok=True, pages=self._pages, listed=listed, records=records, carried_over=carried)
        return Reading(section, inside, flagged)


def _placement(edges: Edges, job: Job) -> str:
    """Where one reading of a job falls against the run's window: inside it, carried over into it, or outside it."""
    if edges.holds(job.scheduled):
        return "inside"
    return "carried" if edges.carried_over(job) else "outside"


def _finished(body: Any) -> float | None:
    """An ended job's finish time from its record: 0 for a job given none (cancelled before it was enqueued), which
    stops the walk when its record says it ended, and None when the record gives no readable one, which neither stops
    the walk nor takes part in its order check, and is placed against the window as any other is."""
    value = body.get("finishTime") if isinstance(body, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _described(samples: Sequence[float | None]) -> dict[str, Any]:
    """A clock's offsets for the snapshot: how many answers were read, how many had no readable Date, the extremes."""
    readable = [o for o in samples if o is not None]
    return {
        "samples": len(samples),
        "unreadable": len(samples) - len(readable),
        "min_s": round(min(readable), 3) if readable else None,
        "max_s": round(max(readable), 3) if readable else None,
    }


def _described_zone(readings: Sequence[ZoneClock | None]) -> dict[str, Any]:
    """The global clock's readings for the snapshot: how many were made, how many could not be read, the least offset
    they allow (`min_s`) and the greatest (`max_s`), each reading's offset less and plus its half round trip, the
    greatest half round trip, and the least and greatest `Date` offset of the same answers, a cross-check."""
    read = [reading for reading in readings if reading is not None]
    return {
        "samples": len(readings),
        "unreadable": len(readings) - len(read),
        "min_s": round(min(r.low for r in read), 3) if read else None,
        "max_s": round(max(r.high for r in read), 3) if read else None,
        "half_trip_s": round(max(r.half_trip for r in read), 3) if read else None,
        "date_offsets_s": span([r.date_offset for r in read]),
    }


def _ids(observation: Observation, providers: Sequence[str]) -> set[str]:
    """Every job id an observation shows, on any provider."""
    return {tid for provider in providers for tid in observation[provider]}
