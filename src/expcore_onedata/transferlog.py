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

**Fail closed.** A page or record that cannot be read, or one not in the documented shape (`transferrecord.py`), makes
the whole listing `probe_ok` false with the reason and no records, never fewer or no transfers. A listing longer than
its bound (`MAX_LISTED` distinct ids, or a token that keeps coming back) is unknown in the same way, not truncated.
`transferwindow.py` places the jobs in the run's window and splits a provider's gained bytes.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from expcore_onedata.client import OnezoneClient
from expcore_onedata.provision import Provisioner
from expcore_onedata.transferrecord import STATES, UNDECLARED, Job, Unreadable, job_of, page_of, record_of
from expcore_onedata.transferwindow import CARRIED_OVER, DISAGREE, Window

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
    and `(target id, reason)` of the jobs that leave their target's bound unknown."""

    section: dict[str, Any]
    inside: list[Job] | None
    flagged: list[tuple[str, str]] = field(default_factory=list)


Observation = dict[str, dict[str, Job]]  # provider name -> transfer id -> the job as that provider has it


class TransferLog:
    """Reads the space's transfer listing and each listed record through the probes' own probe, at run end."""

    def __init__(
        self,
        client: OnezoneClient,
        provisioner: Provisioner,
        providers: Sequence[str],
        probe: Callable[[Callable[[], Any]], Probed],
        *,
        page_limit: int = PAGE_LIMIT,
        max_listed: int = MAX_LISTED,
    ) -> None:
        """List on `providers` (the tracked ones), `page_limit` ids a page; past `max_listed` ids it is unknown."""
        if not 1 <= page_limit <= PAGE_LIMIT or max_listed < 1:
            raise ValueError(f"a page holds 1 to {PAGE_LIMIT} ids and the listing's bound is at least one")
        self._client, self._provisioner, self._providers, self._probe = client, provisioner, list(providers), probe
        self._page_limit, self._max_listed = page_limit, max_listed
        self._pages = 0

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
                found = self._probe(lambda q=params: self._client.get(path, provider=provider, params=q))
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
            "listed": None,
            "records": None,
            "carried_over": None,
        }
        if window is None:
            return Reading({**section, "error": "the run's start was not read, so the run has no window"}, None)
        try:
            observation = self._observe()
        except Unreadable as error:
            return Reading({**section, "pages": self._pages, "error": str(error)}, None)
        inside, records, flagged, carried = [], [], [], 0
        merged = self._merged(observation)
        for provider, job, views in merged:
            agree = len(views) == len(self._providers) and all(v.observed() == job.observed() for v in views)
            if not agree and any(window.bears_on(v) for v in views):
                flagged += [(t, DISAGREE) for t in dict.fromkeys(v.target_id for v in views) if t]
            if window.carried_over(job):
                carried += 1
                flagged += [(job.target_id, CARRIED_OVER)] if job.target_id else []
            if not window.holds(job.scheduled):
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
                )
            )
        listed = len(merged)
        section.update(probe_ok=True, pages=self._pages, listed=listed, records=records, carried_over=carried)
        return Reading(section, inside, flagged)
