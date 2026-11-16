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

**A record's state is read per kind (O32).** Every record carries both halves, `replicationStatus` and
`evictionStatus`, beside its summary `transferStatus` (op-worker f118ed9, the federation's build,
`transfer_rest_translator.erl:119-121`), and the half a job does not do reads `skipped`: a replication that ended
reads `completed`, `completed` and `skipped` (the first federation study's 1,080 answers of its 20 replications, all
so). A replication's state is its replication half's, an eviction's its eviction half's, and a migration's that of the
half it is in, final only once both are; `skipped` on the half a job does not do contradicts nothing, and fields that
do contradict each other read `CONFLICTING`, never final (`_state`). Read as a set of fields that must all agree, every
ended replication and eviction read `CONFLICTING`, so no poll of one ever saw it end, the transfer log carried every
earlier one into the window, and the ended listing's walk never stopped.

**Fail closed.** A page not in that shape, and a record without a readable `scheduleTime`, a `type` of replication,
eviction or migration, or (for a job that replicates) its `replicatingProviderId` and `bytesReplicated`, raise
`Unreadable`; the transfer log then records the whole listing as `probe_ok` false with the reason and no records.
"""

import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

TYPES = ("replication", "eviction", "migration")
REPLICATING = ("replication", "migration")
STATES = ("waiting", "ongoing", "ended")  # the order the lists are read in: the order a job moves through them
UNDECLARED = "undeclared"  # the label of a provider id that no declared provider has
_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a transfer id: one plain path segment
_REPLICATION, _EVICTION = "replicationStatus", "evictionStatus"  # the record's two halves
_SUMMARIES = (
    "transferStatus",
    "transferState",
)  # the record's reading of both halves; `transferState` is no 25.0 field
_STATE_KEYS = (*_SUMMARIES, _REPLICATION, _EVICTION)
SKIPPED = "skipped"  # a half the job does not do, from its creation on (op-worker f118ed9, `transfer.erl:157-166`)
_HALF_ENDED = frozenset({"completed", "failed", "cancelled", SKIPPED})  # a half that ended: `transfer.erl:443-455`
_HALF_WAITING = frozenset({"scheduled", "enqueued", "active"})  # an eviction a migration's summary reads `evicting`
_PROVIDER_KEYS = ("replicatingProviderId", "evictingProviderId")
# Which of the two provider ids each kind names: op-worker derives a record's `type` from them (`transfer.erl:397-403`).
_NAMED = {"replication": (True, False), "eviction": (False, True), "migration": (True, True)}


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


CONFLICTING = "conflicting"  # the state of a record whose status fields contradict each other: never a final one


def _agreed(values: Collection[str]) -> str | None:
    """What every one of the values says, `CONFLICTING` when they disagree, or None when there is none."""
    distinct = set(values)
    if not distinct:
        return None
    return next(iter(distinct)) if len(distinct) == 1 else CONFLICTING


def _halves(kind: str, replication: str | None, eviction: str | None) -> str:
    """The state of a job of `kind` from its record's halves (None for a half the record does not carry).

    On op-worker f118ed9 the half a job does not do is `skipped` from the job's creation (`transfer.erl:157-166`), no
    status is ever marked over an ended one (`replication_status.erl:198-282`, `replica_eviction_status.erl:202-269`),
    and a job has ended once both halves have (`transfer.erl:437-455`). So a replication reads its replication half and
    an eviction its eviction half, beside the other one absent or `skipped`; any other value there, or `skipped` on the
    half the job does, is a contradiction. A migration does both, replication first: it reads its replication half
    until that ends, and its eviction half once the replication completed, so it is final only once both halves are.
    A replication that aborts, fails or is cancelled marks a migration's eviction alike
    (`replication_status.erl:206-282`), so a migration whose replication ended otherwise than completed must have its
    eviction ended the same way: one whose replication failed and whose eviction completed is a contradiction, never
    a final state. One whose replication completed and whose eviction then failed is `failed`, as op-worker's own
    summary reads it (`transfer.erl:475-485`). A half a migration's state rests on that the record does not carry
    leaves it unread, never final."""
    if kind != "migration":
        own, other = (replication, eviction) if kind == "replication" else (eviction, replication)
        return own if own is not None and own != SKIPPED and other in (None, SKIPPED) else CONFLICTING
    if replication is None or SKIPPED in (replication, eviction):
        return CONFLICTING
    if replication not in _HALF_ENDED:  # still replicating: its eviction has not begun
        return replication if eviction not in _HALF_ENDED else CONFLICTING
    if replication == "completed":
        return CONFLICTING if eviction is None else eviction
    return replication if eviction == replication else CONFLICTING


def _summary(kind: str, replication: str | None, eviction: str | None) -> str | None:
    """The `transferStatus` op-worker gives a job with these halves (`transfer:status/1`, `transfer.erl:474-495`): the
    status of the half it is in, `active` read as `replicating` or `evicting`, and a migration's eviction not ended yet
    read as `evicting` once its replication completed."""
    if kind == "migration" and replication == "completed":
        return "evicting" if eviction in _HALF_WAITING else eviction
    if kind == "eviction":
        return "evicting" if eviction == "active" else eviction
    return "replicating" if replication == "active" else replication


def _state(body: Mapping[str, Any]) -> str | None:
    """The record's state, read per kind: the state of the half its job is in (`_halves`), `CONFLICTING` when its
    status fields contradict each other or one cannot be read, or None when it carries none.

    The record's summary, `transferStatus` (and `transferState`, which 25.0 does not give), is op-worker's own reading
    of the halves, so a summary other than the one the halves give is a contradiction (`_summary`). A record that
    carries no half is read from its summary alone. A record whose provider ids name another kind than its `type` is a
    contradiction too, since op-worker derives the type from them (a `type` of replication beside an
    `evictingProviderId` is a migration's record); an id the record does not carry is not checked. A record whose
    `type` is none of the three has no half its job does not do, so every status field it carries must say the same;
    `job_of` refuses such a record anyway.

    `effectiveJobStatus` is not read: it is the summary of the newest job in the record's chain of reruns
    (`transfer_rest_translator.erl:78-88,123`; `transfer.erl:303-318`), another job, with its own id, record and
    counters, once this one was rerun, and this one's own `transferStatus` until then."""
    present = {key: body[key] for key in _STATE_KEYS if body.get(key) is not None}
    if any(not isinstance(value, str) for value in present.values()):  # a status field that cannot be read
        return CONFLICTING
    kind = body.get("type")
    if kind not in TYPES:
        return _agreed(present.values())
    named = (isinstance(body.get(key), str) and bool(body.get(key)) for key in _PROVIDER_KEYS)
    if any(key in body and n is not want for key, n, want in zip(_PROVIDER_KEYS, named, _NAMED[kind], strict=True)):
        return CONFLICTING
    replication, eviction = present.get(_REPLICATION), present.get(_EVICTION)
    summaries = [present[key] for key in _SUMMARIES if key in present]
    if replication is None and eviction is None:
        return _agreed(summaries)
    state = _halves(kind, replication, eviction)
    if state != CONFLICTING and any(summary != _summary(kind, replication, eviction) for summary in summaries):
        return CONFLICTING
    return state


def state_of(body: Any) -> str | None:
    """A record's state as `job_of` reads it; None for a record that is not an object or carries no state. Every
    reader of a transfer's state in the platform layer reads it here: the transfer log and its window, the waits for a
    transfer's end (`consistency.transfer_state`) and the probes' records of the harness's transfers."""
    return _state(body) if isinstance(body, dict) else None


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
