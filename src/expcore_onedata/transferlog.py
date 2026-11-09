# Provenance: new
#   merged: onedata-mcp @ 0cc17ce, onedata_mcp/api/transfers.py:28-63 (the listing's query and answer)
#   merged: onedata-mcp @ 0cc17ce, benchmark/fixture_runner.py:269-281 (paged until nextPageToken is empty)
#   merged: onedata-mcp @ 0cc17ce, benchmark/oracles/placement.py:432-440 (each listed id read by its record)
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/otf_rest_test.py:50-56 (listed by state)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:215-216,236-240 (state and counters)
"""Every transfer of the space in a run's window, whoever started it, read from the space's transfer listing (O08).

The probes (`probes.py`) list the space's transfers at run end on every tracked provider, `GET
/spaces/{space}/transfers` with `state` waiting, ongoing and ended in that order (a job only moves forward, so one that
moves on while the lists are read is found in a later one) and `limit`, following `nextPageToken` until it is empty.
Each distinct id is then read with `GET /transfers/{id}` on the provider that listed it. A provider lists a job in its
listing only after a lag (a source waits up to 60 s for a fresh one to show, `fixture_runner.py:226-245` of the first
`merged:` repository), and a job posted on one provider reaches another's listing later still, so every tracked
provider's listing is read and their ids are merged. This module reads those answers; the requests are the probes'.

What is confirmed. The listing's path, `state` ongoing and ended, `limit`, `page_token`, the answer's `transfers` (ids)
and `nextPageToken` are what running code of the sources in the header sent and read; so are a record's state and
counters (`transferStatus`, `replicationStatus`, `evictionStatus`, `bytesReplicated`, `filesReplicated`,
`filesEvicted`, `filesFailed`) and its `fileId`. The state `waiting` and a record's `type`, `replicatingProviderId`,
`evictingProviderId`, `scheduleTime`, `startTime` and `finishTime` are not: only the provider's API description and a
mocked client test name them. Both are in `client.UNVERIFIED_ENDPOINTS`, each with a live contract test.

**Fail closed.** A page or record that cannot be read, a page not in the shape above, and a record without a readable
`scheduleTime`, a `type` of replication, eviction or migration, or (for a job that replicates) its
`replicatingProviderId` and `bytesReplicated`, raise `Unreadable`, and the probes record the whole listing as
`probe_ok` false with the reason and no records, never as fewer or no transfers. A listing longer than its bound
(`MAX_LISTED` distinct ids, or a token that keeps coming back) is unknown in the same way, not truncated.

**The window.** A job is in the run's window when its `scheduleTime` lies between the whole second the run started in
and the whole second after the block maps were read at its end. The schedule time is the one every job has, a waiting
one included, and it says when someone (the harness, the agent through the tool plane, a QoS requirement) started it.
The run's times are this host's clock and the job's the provider's; the two are compared as they are, and a skew
between them moves a job across the edge. A job scheduled before the window that had not ended by its start (listed
as waiting or ongoing, or ended at or after the start's whole second, or with no finish time) is not in the record,
but it may have placed bytes during the run, so the probes count it (`carried_over`).
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

TYPES = ("replication", "eviction", "migration")
REPLICATING = ("replication", "migration")
STATES = ("waiting", "ongoing", "ended")  # the order the lists are read in: the order a job moves through them
PAGE_LIMIT = 1000  # ids asked for per page; the listing takes 1 to 1000
MAX_LISTED = 2000  # distinct ids read before the listing is unknown: each one costs a read of its record
UNDECLARED = "undeclared"  # the label of a provider id that no declared provider has
_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a transfer id: one plain path segment
_STATE_KEYS = ("transferStatus", "transferState", "replicationStatus", "evictionStatus")


class Unreadable(ValueError):
    """The listing or a record is not in the shape this module reads; the message names a field, never a value."""


@dataclass(frozen=True)
class Window:
    """The run's window in epoch seconds of this host's clock: from its start to just after its end's readings."""

    start: float
    end: float

    def holds(self, moment: float) -> bool:
        """Tell whether a provider's time, in whole seconds, lies in the window."""
        return math.floor(self.start) <= moment <= math.ceil(self.end)

    def to_json(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class Job:
    """One transfer as its record reads; the provider ids are as the record gives them."""

    transfer_id: str
    listed_as: str
    kind: str
    file_id: str | None
    target_id: str | None
    source_id: str | None
    state: str | None
    bytes_replicated: int | None
    files_replicated: int | None
    files_evicted: int | None
    files_failed: int | None
    scheduled: float
    started: float | None
    finished: float | None

    def carried_over(self, window: Window) -> bool:
        """Tell whether a job scheduled before the window may have gone on into it."""
        start = math.floor(window.start)
        ended_before = self.listed_as == "ended" and self.finished is not None and 0 < self.finished < start
        return self.scheduled < start and not ended_before


def page_of(answer: Any) -> tuple[list[str], str | None]:
    """Read one page of the listing: its ids, and the token of the next page or None on the last."""
    if not isinstance(answer, dict) or not isinstance(answer.get("transfers"), list):
        raise Unreadable("a page of the listing has no list of transfers")
    ids = answer["transfers"]
    if not all(isinstance(t, str) and _ID.fullmatch(t) for t in ids):
        raise Unreadable("a page of the listing holds an id that is not a plain transfer id")
    token = answer.get("nextPageToken")
    if token is not None and not isinstance(token, str):
        raise Unreadable("a page of the listing has a nextPageToken that is not text")
    return list(ids), token or None


def _count(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _time(body: Mapping[str, Any], key: str) -> float | None:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return value


def _provider(body: Mapping[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and value else None


def job_of(transfer_id: str, listed_as: str, body: Any) -> Job:
    """Read a transfer's record; raise Unreadable when what the window or the sum rests on is missing."""
    if not isinstance(body, dict):
        raise Unreadable("a transfer's record is not an object")
    kind = body.get("type")
    if kind not in TYPES:
        raise Unreadable("a transfer's record has no type of replication, eviction or migration")
    scheduled = _time(body, "scheduleTime")
    if scheduled is None:
        raise Unreadable("a transfer's record has no readable scheduleTime")
    target = _provider(body, "replicatingProviderId") if kind in REPLICATING else None
    moved = _count(body, "bytesReplicated")
    if kind in REPLICATING and target is None:
        raise Unreadable(f"a {kind} record names no replicatingProviderId")
    if kind in REPLICATING and moved is None:
        raise Unreadable(f"a {kind} record has no readable bytesReplicated")
    evicted = "filesEvicted" if "filesEvicted" in body else "fileReplicasEvicted"
    return Job(
        transfer_id=transfer_id,
        listed_as=listed_as,
        kind=kind,
        file_id=_provider(body, "fileId"),
        target_id=target,
        source_id=_provider(body, "evictingProviderId") if kind != "replication" else None,
        state=next((body[k] for k in _STATE_KEYS if isinstance(body.get(k), str)), None),
        bytes_replicated=moved,
        files_replicated=_count(body, "filesReplicated"),
        files_evicted=_count(body, evicted),
        files_failed=_count(body, "filesFailed"),
        scheduled=scheduled,
        started=_time(body, "startTime"),
        finished=_time(body, "finishTime"),
    )


def record_of(job: Job, *, label: Callable[[str | None], str | None], file: str | None, harness: bool) -> dict:
    """Return a job as the snapshot records it: providers by position label, never by name or id."""
    return {
        "transfer_id": job.transfer_id,
        "type": job.kind,
        "source": label(job.source_id),
        "target": label(job.target_id),
        "listed_as": job.listed_as,
        "state": job.state,
        "files_replicated": job.files_replicated,
        "bytes_replicated": job.bytes_replicated,
        "files_evicted": job.files_evicted,
        "files_failed": job.files_failed,
        "schedule_time": job.scheduled,
        "start_time": job.started,
        "end_time": job.finished,
        "file": file,
        "scheduled_by_harness": harness,
    }
