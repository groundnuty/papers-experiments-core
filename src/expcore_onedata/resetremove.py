# Provenance: new
"""The reset's removal of the run's own QoS requirements: each waited on until it is fulfilled, deleted, and its
deletion waited on until every provider has processed it, before the files are settled and evicted (O33, #209).

Sources are op-worker `f118ed9` (paths below are its own), read for #209.

**The run's own.** A requirement present at the run's start (`SpaceReset.qos_requirements()`, the caller's baseline)
is kept; every other one the scan finds (`resetqos.scan`) is the run's and is removed, in this order.

**1. Fulfilled before it is deleted.** A deletion cancels a requirement's replication only on the provider that
processes it, and best-effort: its traverses are cancelled and its in-flight synchronizer requests aborted by an
asynchronous cast (`qos_traverse.erl:88-93`, `replica_synchronizer.erl:224-231`), both on lists that provider keeps for
itself. The provider that replicates learns of a deletion sent through another only when dbsync delivers it (by the
timers' sum, an inference, about 8 s on a quiet space and 15 s more when a batch must be asked for again,
`dbsync_in_stream_worker.erl:445-452`; `dbsync_events.erl:124-129`), and replicates whole files until then. So each
requirement is first read on every provider, its record (`GET /qos_requirements/{qid}`), until its `status` is
`fulfilled` or `impossible` there on `consecutive_polls` readings in a row, every `poll_s`, within `[placement]
qos_fulfil_timeout_s` (120 s by default); one still `pending` at the bound fails the reset, and nothing is deleted. A
provider that answers that the requirement is not there has not received it yet and is waited for: the build reports a
requirement `pending` while a provider that supports the space does not know it (`qos_status.erl:107-137,122-127`).
`impossible` is settled: no storage can hold the copy, and nothing replicates. **`pending` does not prove that bytes
are moving:** it says that a traverse has not finished or a reconciliation is outstanding (`qos_status.erl:224-234`),
and no REST answer shows bytes in flight.

**2. Deleted, then the deletion barrier.** Each is deleted through the gate on the provider whose scan showed it first.
Then, on every provider, the summary of the requirement's entry (`GET /data/{id}/qos/summary`) must no longer list it
and its record must answer that it is not there (`client.answered_absent`), on `consecutive_polls` readings in a row
within `BARRIER_BOUND_S` (60 s), or the reset fails. The summary decides: a provider drops the requirement from its
summaries first when it processes the deletion (`file_qos:remove_qos_entry_id`, `qos_logic.erl:84-95`), right before it
cancels the replication it runs, while its record can answer absent as soon as the deleted document is stored, before
that processing begins (`dbsync_events.erl:43-48`). An eviction a provider processes before then is cancelled for every
file the requirement still holds there (`replica_deletion_master.erl:351-371`, `file_qos.erl:232-242`), and reports
success; so no file is settled or evicted before every provider has processed every deletion. A second scan then finds
none but the baseline's. The affected files' block maps are settled next (`resetqos.settle`), for the blocks a cancel
had in flight.

**The residual.** A requirement an agent adds and removes during the run is gone by the reset, and its cancel ran then.
**An agent's add-then-remove during the run whose cancel missed may still be replicating one file at the reset**: a
cancel misses a file whose request was not registered yet, one shared with a client's read, or one on another node of
the provider (`replica_synchronizer.erl:607,1148-1157,377-382`), and that file then replicates whole. It is at most one
file's size, and rare, since it needs a missed cancel. It may land after T, the instant the quiet stage certifies
(`resetqos.quiet`), and is then caught by the next start's reset: its occupancy shows the bytes, or its eviction removes
them.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from expcore_onedata import consistency, resetqos
from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.provision import Provisioner, ProvisionError
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.transferrecord import Unreadable

SETTLED = frozenset({"fulfilled", "impossible"})  # the statuses a requirement may be deleted in
STATUSES = frozenset({*SETTLED, "pending"})  # every status the build's record gives (`qos_status.erl:130-137`)
BARRIER_BOUND_S = 60.0  # the longest the reset waits for every provider to process the deletions

Key = tuple[str, str]  # (provider, requirement id)


def _record(p: Provisioner, key: Key) -> dict[str, str] | None:
    """A requirement's `status` and `fileId` on one provider, read once (the poll is the retry); None where the
    provider answers that it is not there. Any other failure propagates, and the poll takes it for no match."""
    provider, qid = key
    try:
        record = p.client.get(f"/qos_requirements/{qid}", provider=provider, retry=ONE_ATTEMPT)
    except PlatformError as error:
        if answered_absent(error):
            return None
        raise
    status = record.get("status") if isinstance(record, dict) else None
    file_id = record.get("fileId") if isinstance(record, dict) else None
    if not isinstance(status, str) or status not in STATUSES:
        raise Unreadable("a QoS requirement's record has no status of fulfilled, pending or impossible")
    if not isinstance(file_id, str) or not resetqos.plain_id(file_id):
        raise Unreadable("a QoS requirement's record names no plain fileId")
    return {"status": status, "fileId": file_id}


def _gone(p: Provisioner, key: Key) -> bool:
    """Whether a deleted requirement's record answers on one provider that it is not there (`answered_absent`: a 404,
    or 25.0's 400 posix enoent); any other failure, another 400 included, propagates and does not count."""
    provider, qid = key
    try:
        p.client.get(f"/qos_requirements/{qid}", provider=provider, retry=ONE_ATTEMPT)
    except PlatformError as error:
        if answered_absent(error):
            return True
        raise
    return False


def _processed(p: Provisioner, entries: dict[str, str], key: Key) -> bool:
    """Whether one provider has processed a requirement's deletion: the summary of its entry, read once, no longer
    lists it (an entry that is not there lists none), and its record answers that it is not there."""
    provider, qid = key
    try:
        answer = p.client.get(f"/data/{entries[qid]}/qos/summary", provider=provider, retry=ONE_ATTEMPT)
    except PlatformError as error:
        if not answered_absent(error):
            raise
        answer = {"requirements": {}}
    return qid not in resetqos.requirements_of(answer) and _gone(p, key)


def _poll(
    p: Provisioner, keys: list[Key], read: Callable[[Key], Any], matches: Callable[[Any], bool], bound_s: float
) -> consistency.EachConvergence:
    """Read every key until each matches on `consecutive_polls` readings in a row, every `poll_s`, within `bound_s`."""
    settings = p.settings
    return consistency.converge_each(
        keys,
        read,
        matches,
        timeout_s=bound_s,
        poll_s=settings.poll_s,
        consecutive=settings.consecutive_polls,
        workers=settings.concurrency,
        clock=p.clock,
        sleep=p.sleep,
    )


def fulfilled(p: Provisioner, providers: Sequence[str], qids: Sequence[str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Wait until each requirement is `fulfilled` or `impossible` on every provider; return the wait's facts and each
    requirement's entry id. Raise ProvisionError at `qos_fulfil_timeout_s` with one still pending, or not received."""
    began, bound = p.clock(), p.settings.qos_fulfil_timeout_s
    keys = [(provider, qid) for qid in qids for provider in providers]
    waited = _poll(p, keys, lambda key: _record(p, key), lambda r: r is not None and r["status"] in SETTLED, bound)
    if not waited.converged:
        raise ProvisionError(
            f"{len(waited.pending)} QoS requirement reading(s) still pending, or not yet received, after "
            f"{bound:.0f} s: none is deleted while its replication may still run"
        )
    entries = {qid: waited.values[(providers[0], qid)]["fileId"] for qid in qids}
    impossible = sum(any(waited.values[(q, qid)]["status"] == "impossible" for q in providers) for qid in qids)
    facts = {"fulfil_s": round(p.clock() - began, 3), "fulfil_polls": waited.polls, "impossible": impossible}
    return facts, entries


def barrier(p: Provisioner, providers: Sequence[str], entries: dict[str, str]) -> dict[str, Any]:
    """Wait until every provider has processed each deletion (`_processed`); return the wait's facts. Raise
    ProvisionError at `BARRIER_BOUND_S` with one still listed."""
    began = p.clock()
    keys = [(provider, qid) for qid in entries for provider in providers]
    waited = _poll(p, keys, lambda key: _processed(p, entries, key), bool, BARRIER_BOUND_S)
    if not waited.converged:
        raise ProvisionError(
            f"{len(waited.pending)} QoS requirement reading(s) still listed after the deletions, past "
            f"{BARRIER_BOUND_S:.0f} s: a provider has not processed one, and may still replicate for it"
        )
    return {"barrier_s": round(p.clock() - began, 3), "barrier_polls": waited.polls}


def remove(
    p: Provisioner, providers: Sequence[str], baseline: Iterable[str]
) -> tuple[dict[str, Any], dict[str, int], int]:
    """Find every requirement, and remove each one not in `baseline`: wait until it is fulfilled on every provider,
    delete it through the gate, wait until every provider has processed the deletion, and show by a second scan that
    none is left but the baseline's. Return the stage's facts, the affected files (those the removed requirements
    applied to, each with its depth) and the depth of the deepest regular file of the space, from the first scan."""
    kept = frozenset(baseline)
    found = resetqos.scan(p, providers)
    added = {qid: where for qid, where in found.where.items() if qid not in kept}
    facts: dict[str, Any] = {"scan": p.settings.qos_scan, "found": len(found.where)}
    facts["kept"] = len(found.where) - len(added)
    affected = {file_id: depth for qid in added for file_id, depth in found.files[qid].items()}
    waits: dict[str, Any] = {"fulfil_s": 0.0, "fulfil_polls": 0, "impossible": 0, "barrier_s": 0.0, "barrier_polls": 0}
    reads = dict(found.reads)
    if added:
        waits, entries = fulfilled(p, providers, sorted(added))
        for qid, provider in added.items():
            p.mutate("DELETE", f"/qos_requirements/{qid}", provider=provider)
        waits.update(barrier(p, providers, entries))
        again = resetqos.scan(p, providers)
        left = [qid for qid in again.where if qid not in kept]
        if left:
            raise ProvisionError(f"the scan after the removals still finds {len(left)} QoS requirement(s) not kept")
        reads = {k: reads.get(k, 0) + again.reads.get(k, 0) for k in reads.keys() | again.reads.keys()}
    facts.update(removed=len(added), affected_files=len(affected), **waits, reads=reads)
    return facts, affected, found.deepest


__all__ = ["BARRIER_BOUND_S", "SETTLED", "STATUSES", "barrier", "fulfilled", "remove"]
