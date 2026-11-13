# Provenance: new
#   merged: onedata-mcp @ 0cc17ce, onedata_mcp/api/transfers.py:28-63 (the listing's query and answer)
#   merged: onedata-mcp @ 0cc17ce, benchmark/oracles/placement.py:432-440 (each listed id read by its record)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:215-216,236-240 (state and counters)
"""A page of the space's transfer listing and one transfer's record, read as the transfer log needs them (O08).

What is confirmed. The listing's answer, `transfers` (ids) and `nextPageToken`, is what running code of the sources in
the header read; so are a record's state and counters (`transferStatus`, `replicationStatus`, `evictionStatus`,
`bytesReplicated`, `filesReplicated`, `filesEvicted`, `filesFailed`) and its `fileId`. A record's `type`,
`replicatingProviderId`, `evictingProviderId`, `scheduleTime`, `startTime` and `finishTime` were named only by the
provider's API description and a mocked client test; against Onedata 25.0 the live contract test confirmed them for a
replication and a direct read for an eviction, with the times in epoch seconds (`[L1]` in `client.py`), and the test
stays to pin them.

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
        """What two readings of the job must share to be the same observation: what it is (its kind, file, source and
        target, which no clock affects), its list, state and counters. Its times are not compared here, since the
        providers' clocks differ; each reading is placed against the window on its own instead."""
        what = (self.kind, self.file_id, self.source_id, self.target_id)
        counters = (self.bytes_replicated, self.files_replicated, self.files_evicted, self.files_failed)
        return (*what, self.listed_as, self.state, *counters)


CONFLICTING = "conflicting"  # the state of a record whose status fields disagree: never a final one


def _state(body: Mapping[str, Any]) -> str | None:
    """The record's state: what every status field it carries says, `CONFLICTING` when they disagree (a migration
    whose replication completed and whose eviction is still active, say) or one cannot be read, or None when it
    carries none."""
    present = [body[k] for k in _STATE_KEYS if k in body and body[k] is not None]
    if any(not isinstance(value, str) for value in present):  # a status field that cannot be read is no state
        return CONFLICTING
    states = set(present)
    if not states:
        return None
    return next(iter(states)) if len(states) == 1 else CONFLICTING


def page_of(answer: Any) -> tuple[list[str], str | None]:
    """Read one page of the listing: its ids, and the token of the next page or None on the last.

    The last page says so with `nextPageToken` null (the provider's API description: "When end of list is reached, the
    token is returned with a null value"). On 25.0 that was measured, in
    `deploy/compose-federation/day0/2026-11-12-federation-facts-listing-end.md`: every page carries `transfers` and
    `nextPageToken` and none `isLast`, which is not asked for; the token is a string on each page before the last and
    null on the last; and a page the last ids fill gets a token, followed by an empty page with a null one. A page
    without the key, or with one that is neither null nor a token, may be cut, so it is refused, never read as the end
    of the listing (O26).
    """
    if not isinstance(answer, dict) or not isinstance(answer.get("transfers"), list):
        raise Unreadable("a page of the listing has no list of transfers")
    ids = answer["transfers"]
    if not all(isinstance(t, str) and _ID.fullmatch(t) for t in ids):
        raise Unreadable("a page of the listing holds an id that is not a plain transfer id")
    if "nextPageToken" not in answer:
        raise Unreadable("a page of the listing has no nextPageToken, the marker of its last page, so it may be cut")
    token = answer["nextPageToken"]
    if token is not None and (not isinstance(token, str) or not token):
        raise Unreadable("a page of the listing has a nextPageToken that is neither null nor a token")
    return list(ids), token


def _count(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _evicted_key(body: Mapping[str, Any]) -> str:
    """The name a record carries its eviction counter under: `filesEvicted`, or the older `fileReplicasEvicted`."""
    return "filesEvicted" if "filesEvicted" in body else "fileReplicasEvicted"


def eviction_counters(body: Any) -> tuple[int, int, int] | None:
    """`(filesProcessed, filesEvicted, filesFailed)` of a transfer's record, or None when the record is not an object
    or any of the three is missing or not a count: unknown is never zero.

    In op-worker's source (f118ed9, `replica_eviction_worker.erl`) a file is counted in `filesProcessed` however it
    ended, and in `filesEvicted` or `filesFailed` only when it was evicted or failed. So `filesProcessed` above the
    other two counts files the eviction skipped without a failure: ones the provider holds nothing of, ones cancelled
    per file (a QoS requirement still covering the file there), ones open, and ones for which the provider knows of no
    other copy yet, which is how an eviction right after a placement can complete having evicted nothing (O01)."""
    if not isinstance(body, dict):
        return None
    processed = _count(body, "filesProcessed")
    evicted = _count(body, _evicted_key(body))
    failed = _count(body, "filesFailed")
    return None if processed is None or evicted is None or failed is None else (processed, evicted, failed)


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
    return Job(
        transfer_id=transfer_id,
        listed_as=listed_as,
        kind=kind,
        file_id=_provider(body, "fileId"),
        target_id=target,
        source_id=_provider(body, "evictingProviderId") if kind != "replication" else None,
        state=_state(body),
        bytes_replicated=moved,
        files_replicated=_count(body, "filesReplicated"),
        files_evicted=_count(body, _evicted_key(body)),
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
