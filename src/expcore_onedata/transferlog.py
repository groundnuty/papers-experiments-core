# Provenance: new
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:269-281 (paged until nextPageToken is empty)
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:226-245 (a fresh transfer lags in the listing)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/otf_rest_test.py:50-56 (listed by state)
"""Every transfer of the space in a run's window, whoever started it, read from the space's transfer listing (O08).

At run end `TransferLog` observes every tracked provider: it lists the space's transfers there, `GET
/spaces/{space}/transfers` with `state` waiting, ongoing and ended in that order (a job only moves forward, so one that
moves on while the lists are read is found in a later one) and `limit`, following `nextPageToken` until it is empty,
and reads each id it listed with `GET /transfers/{id}` on that same provider. Every request goes through the probes'
own probe (`probes.run_probe`): a real answer is final and only a missing one is retried, a bounded number of times.
`transferrecord.py` reads the answers and says which of their fields are confirmed.

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

**Fail closed.** A page or record that cannot be read, or one not in the documented shape (`transferrecord.py`), makes
the whole listing `probe_ok` false with the reason and no records, never fewer or no transfers. A listing longer than
its bound (`MAX_LISTED` distinct ids, or a token that keeps coming back) is unknown in the same way, not truncated.
`transferwindow.py` places the jobs in the run's window, moved onto the providers' clocks by the offsets the listing
answers' Date headers give (recorded under `clock`), and splits a provider's gained bytes.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from expcore_onedata.client import OnezoneClient
from expcore_onedata.provision import Provisioner
from expcore_onedata.transferrecord import STATES, UNDECLARED, Job, Unreadable, job_of, page_of, record_of
from expcore_onedata.transferwindow import (
    CARRIED_OVER,
    CLOCK_UNKNOWN,
    DISAGREE,
    NEAR_EDGE,
    NOT_SETTLED,
    Edges,
    Window,
    offset_of,
)

PAGE_LIMIT = 1000  # ids asked for per page; the listing takes 1 to 1000
MAX_LISTED = 2000  # distinct ids read before the listing is unknown: each one costs a read of its record


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
        self._client, self._provisioner, self._providers, self._probe = client, provisioner, list(providers), probe
        self._page_limit, self._max_listed, self._sleep, self._now = page_limit, max_listed, sleep, now
        self._pages = 0
        self._offsets: dict[str, list[float | None]] = {}  # provider -> each listing answer's clock offset, or None

    def _dated_page(self, path: str, provider: str, params: Mapping[str, Any]) -> Any:
        """Read one page of a provider's listing and keep the provider's clock offset its Date header gives."""
        answer, date = self._client.get_dated(path, provider=provider, params=params)
        self._offsets.setdefault(provider, []).append(offset_of(date, self._now()))
        return answer

    def _listed_on(self, provider: str) -> dict[str, str]:
        """List one provider's transfers in every state, page by page; return each id with the furthest state that
        provider listed it in. Raise Unreadable when that fails."""
        listed: dict[str, str] = {}
        path = f"/spaces/{self._provisioner.space_id}/transfers"
        max_pages = self._max_listed // self._page_limit + 1
        where = self._client.provider_label(provider)
        for state in STATES:
            token: str | None = None
            for _ in range(max_pages):
                params = {"state": state, "limit": self._page_limit, **({"page_token": token} if token else {})}
                found = self._probe(lambda q=params: self._dated_page(path, provider, q))
                self._pages += 1
                if not found.ok:
                    raise Unreadable(f"{where}: a page of the {state} listing could not be read: {found.error}")
                ids, token = page_of(found.value)
                for tid in ids:
                    listed[tid] = max(listed.get(tid, state), state, key=STATES.index)
                if len(listed) > self._max_listed:
                    raise Unreadable(
                        f"the space lists more than {self._max_listed} transfers: the listing is unknown, not cut"
                    )
                if token is None:
                    break
            else:
                raise Unreadable(f"{where}: the {state} listing did not end within {max_pages} pages")
        return listed

    def _observe(self) -> Observation:
        """List every tracked provider and read each id it listed on that same provider; raise Unreadable on failure."""
        observation: Observation = {}
        for provider in self._providers:
            jobs: dict[str, Job] = {}
            for tid, state in self._listed_on(provider).items():
                found = self._probe(lambda t=tid, p=provider: self._client.get(f"/transfers/{t}", provider=p))
                if not found.ok:
                    raise Unreadable(f"a transfer's record could not be read: {found.error}")
                jobs[tid] = job_of(tid, state, found.value)
            observation[provider] = jobs
        if len(set().union(*observation.values())) > self._max_listed:
            raise Unreadable(f"the space lists more than {self._max_listed} transfers: the listing is unknown, not cut")
        return observation

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
        previous = self._observe()
        changed: list[Job] = []
        for polls in range(2, settings.transfer_settle_polls + 1):
            self._sleep(settings.transfer_settle_s)
            current = self._observe()
            changed = changed_between(previous, current)
            if not changed:
                return current, [], polls
            previous = current
        return previous, changed, settings.transfer_settle_polls

    def read(self, window: Window | None, file_ids: Mapping[str, str], harness: Collection[str]) -> Reading:
        """Return the snapshot's record of the space's transfers in the window, with the jobs in it and those that leave
        a bound unknown. `file_ids` labels a tracked file's id, and `harness` holds the ids the provisioner scheduled.
        When the listing is unknown the jobs are None and the section's `probe_ok` is false with the reason."""
        self._pages = 0
        section: dict[str, Any] = {
            "probe_ok": False,
            "error": None,
            "window": None if window is None else window.to_json(),
            "providers": [self._client.provider_label(p) for p in self._providers],
            "pages": 0,
            "settle_s": self._provisioner.settings.transfer_settle_s,
            "settled": None,
            "polls": None,
            "listed": None,
            "records": None,
            "carried_over": None,
        }
        if window is None:
            return Reading({**section, "error": "the run's start was not read, so the run has no window"}, None)
        self._offsets = {}
        try:
            observation, changed, polls = self._settled()
        except Unreadable as error:
            return Reading({**section, "pages": self._pages, "error": str(error)}, None)
        flagged = [(t, NOT_SETTLED) for t in dict.fromkeys(j.target_id for j in changed) if t]
        edges, clock = self._edges(window)
        section.update(settled=not changed, polls=polls, clock=clock)
        reading = self._classified(edges, observation, file_ids, harness, section, flagged)
        # A listing that never settled may still be missing a transfer that has not appeared in any reading, and such a
        # transfer could be into any tracked provider: every bound is then unknown, not only those of the changed jobs.
        everywhere = set() if clock["known"] else {CLOCK_UNKNOWN}
        if changed:
            everywhere.add(NOT_SETTLED)
        return Reading(reading.section, reading.inside, reading.flagged, frozenset(everywhere))

    def _edges(self, window: Window) -> tuple[Edges, dict[str, Any]]:
        """Move the window onto the providers' clocks by the offsets their listing answers gave, and describe that for
        the snapshot. A provider with an answer whose Date could not be read leaves the clock unknown, and the window
        is then taken as it is on this host's clock."""
        allowance = self._provisioner.settings.clock_skew_allowance_s
        offsets = {p: self._offsets.get(p, []) for p in self._providers}
        known = bool(offsets) and all(samples and None not in samples for samples in offsets.values())
        seen = [o for samples in offsets.values() for o in samples if o is not None]
        edges = Edges(window, min(seen), max(seen), allowance) if known else Edges(window, 0.0, 0.0, allowance)
        described = {}
        for provider, samples in offsets.items():
            readable = [o for o in samples if o is not None]
            described[self._client.provider_label(provider)] = {
                "samples": len(samples),
                "unreadable": len(samples) - len(readable),
                "min_s": round(min(readable), 3) if readable else None,
                "max_s": round(max(readable), 3) if readable else None,
            }
        return edges, {"known": known, "offsets": described, **edges.to_json()}

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
