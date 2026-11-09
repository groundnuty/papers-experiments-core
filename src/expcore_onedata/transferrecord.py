# Provenance: new
#   merged: onedata-mcp @ 0cc17ce, onedata_mcp/api/transfers.py:28-63 (the listing's query and answer)
#   merged: onedata-mcp @ 0cc17ce, benchmark/oracles/placement.py:432-440 (each listed id read by its record)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:215-216,236-240 (state and counters)
"""A page of the space's transfer listing and one transfer's record, read as the transfer log needs them (O08).

What is confirmed. The listing's answer, `transfers` (ids) and `nextPageToken`, is what running code of the sources in
the header read; so are a record's state and counters (`transferStatus`, `replicationStatus`, `evictionStatus`,
`bytesReplicated`, `filesReplicated`, `filesEvicted`, `filesFailed`) and its `fileId`. A record's `type`,
`replicatingProviderId`, `evictingProviderId`, `scheduleTime`, `startTime` and `finishTime` are not: only the
provider's API description and a mocked client test name them. They are in `client.UNVERIFIED_ENDPOINTS`, with a live
contract test.

**Fail closed.** A page not in that shape, and a record without a readable `scheduleTime`, a `type` of replication,
eviction or migration, or (for a job that replicates) its `replicatingProviderId` and `bytesReplicated`, raise
`Unreadable`; the transfer log then records the whole listing as `probe_ok` false with the reason and no records.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

TYPES = ("replication", "eviction", "migration")
REPLICATING = ("replication", "migration")
STATES = ("waiting", "ongoing", "ended")  # the order the lists are read in: the order a job moves through them
UNDECLARED = "undeclared"  # the label of a provider id that no declared provider has
_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a transfer id: one plain path segment
_STATE_KEYS = ("transferStatus", "transferState", "replicationStatus", "evictionStatus")


class Unreadable(ValueError):
    """The listing or a record is not in the shape the transfer log reads; the message names a field, never a value."""


@dataclass(frozen=True)
class Job:
    """One transfer as one provider lists it and answers its record; the provider ids are as the record gives them."""

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

    def observed(self) -> tuple[Any, ...]:
        """What two readings of the job must share to be the same observation: its list, state and counters."""
        counters = (self.bytes_replicated, self.files_replicated, self.files_evicted, self.files_failed)
        return (self.listed_as, self.state, *counters)


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


def record_of(job: Job, *, label: Callable[[str | None], str | None], **extra: Any) -> dict[str, Any]:
    """Return a job as the snapshot records it, providers by position label, never by name or id; `extra` are what
    the transfer log adds (the tracked file it moved, where it was observed, whether the providers agree)."""
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
        **extra,
    }
