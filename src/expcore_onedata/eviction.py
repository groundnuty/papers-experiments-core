# Provenance: new
"""Bulk eviction: many files off a provider, then verified together on their block maps (O01, O07, O09).

Evicting a reset's files one by one waits on each file's transfer and polls each file's block map in turn, a few
seconds a file, so hundreds of files take minutes. Here the waits are shared:

- **One call for a directory** (`evict_tree`). An eviction scheduled on a directory applies to every file under it
  (Oneprovider 25.0's REST description of `POST /transfers`: "In case of a directory, the transfer applies to all its
  subfiles and subdirectories (recursively)"), so a subtree leaves a provider in one transfer. The files under it are
  listed once beforehand, one listing per directory, and verified once the transfer has ended.
- **Many files in parallel** (`evict_ids`). A set of files that is not one directory is evicted file by file, at most
  `[placement] concurrency` requests in flight at once (16 by default), and their transfers are waited on together.

Either way the transfers' end is a report, never evidence (O01): the files are then verified **together** on the
block maps (`require_empty_each`), each poll reading every file not yet settled with the same bound, and a file counts
as empty once its map shows nothing held on `[placement] consecutive_polls` polls in a row (O07). A file still holding
bytes at the bound fails the eviction, `NotPlaced` with the count, never a guess. A caller's `quiet` check runs between
the transfers' end and each round of block-map reads: the reset passes one that requires no transfer running on any
provider, so no map is read while another job still changes it. The data provider's own copy is never evicted (O09):
`Provisioner.check_eviction` refuses it, and a provider that does not support the space, before anything is sent.

**An eviction that evicted nothing is scheduled again**, by the rule `Provisioner.evict` follows (Onedata 25.0: an
eviction scheduled within about a second of a placement settling can complete with `filesEvicted` 0 and remove
nothing). While a file's block map still shows bytes `EVICTION_CHECK_S` after the transfers ended, its eviction is
scheduled again after `EVICTION_RETRY_PAUSE_S`, the directory's as a whole for a tree and each such file's for a set,
up to `EVICTION_ATTEMPTS` in all; after the last the whole settle bound is waited before `NotPlaced` refuses it. Only
the block map judges, never a transfer's counters, and the record says how many attempts it took.

**The counters name the cause and are recorded; they never decide.** An eviction ends `completed` with `filesProcessed`
above `filesEvicted` and `filesFailed` 0 when files were skipped without a failure (op-worker f118ed9,
`replica_eviction_worker.erl:51-59, 88-96`): one the provider holds nothing of, one open, one for which it knows of no
other copy yet (the O01 no-op above), and one **cancelled per file because a QoS requirement still covers it there**
(`replica_deletion_master.erl:364-371`: a requirement's deletion that has not reached that provider through dbsync
yet leaves it in force). The first three are normal, so a gap in an attempt is no failure while the block maps show
the files empty, which the attempts, and the reset's QoS stage before them, usually bring about. When files still
hold bytes after the last attempt, the refusal (`EvictionIncomplete`, a `NotPlaced`) names the provider, the last
attempt's counts and, when they show files skipped without a failure, the likely cause. A success and a refusal both
record the last attempt's counts (`files_processed`, `files_evicted`, `files_failed`), each None when a record lacks
one: unknown is never zero.

The wait for the transfers to end polls each one's record, and a poll that times out ends the eviction, so those polls
are tried again under the provisioner's policy (O29, `readretry.py`), within the wait's own bound. The block-map reads
of the verification are inside a poll that counts a failed read as one that did not match, and stay one attempt.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.placement import held_bytes
from expcore_onedata.provision import (
    EVICTION_ATTEMPTS,
    EVICTION_CHECK_S,
    EVICTION_RETRY_PAUSE_S,
    Entry,
    NotPlaced,
    PlacementReport,
    Provisioner,
    ProvisionError,
)
from expcore_onedata.transferrecord import eviction_counters

COUNTERS = ("files_processed", "files_evicted", "files_failed")  # the record's keys of `eviction_counters`, in order


class EvictionIncomplete(NotPlaced):
    """Files still hold bytes after the last attempt. `facts` is what the reset's failed stage records of the provider:
    its position label, the counts of the last attempt, the transfers and the attempts."""

    def __init__(self, message: str, report: PlacementReport, facts: dict[str, Any]) -> None:
        super().__init__(message, report)
        self.facts = facts


def _counted(records: Iterable[Any]) -> dict[str, int | None]:
    """The sum of each counter over the transfers' records, all None when there is none or one lacks a counter."""
    read = [eviction_counters(record) for record in records]
    if not read or any(counts is None for counts in read):
        return dict.fromkeys(COUNTERS)
    return {key: sum(counts[i] for counts in read if counts) for i, key in enumerate(COUNTERS)}


def sum_counts(results: Iterable[Mapping[str, Any]]) -> dict[str, int | None]:
    """The counters of several evictions' records added up, each None when any of them is."""
    rows = list(results)
    return {key: None if any(r.get(key) is None for r in rows) else sum(r[key] for r in rows) for key in COUNTERS}


def _converge_empty(
    provisioner: Provisioner, provider: str, ids: Sequence[str], timeout_s: float
) -> consistency.EachConvergence:
    """Poll the files' block maps on `provider` together until each shows nothing held on consecutive polls."""
    settings = provisioner.settings
    return consistency.converge_each(
        ids,
        lambda fid: held_bytes(provisioner.blocks(fid, provider)),
        lambda held: held == 0,
        timeout_s=timeout_s,
        poll_s=settings.poll_s,
        consecutive=settings.consecutive_polls,
        workers=settings.concurrency,
        sleep=provisioner.sleep,
        clock=provisioner.clock,
    )


def _skipped_note(label: str, counts: Mapping[str, int | None]) -> str:
    """What the last attempt's counts say, for the refusal: the counts, and when files were processed and neither
    evicted nor failed, the likely cause; nothing when a count is unknown."""
    processed, evicted, failed = (counts[key] for key in COUNTERS)
    if processed is None or evicted is None or failed is None:
        return ""
    note = (
        f"; on {label} the last attempt's eviction transfer(s) ended with filesProcessed {processed}, "
        f"filesEvicted {evicted} and filesFailed {failed}"
    )
    if processed > evicted + failed:
        note += (
            f", so {processed - evicted - failed} file(s) were skipped without a failure: the likely cause is an "
            "eviction cancelled per file, e.g. a QoS requirement still covering it there (a file open there is "
            "skipped alike)"
        )
    return note


def _not_empty(
    provisioner: Provisioner,
    ids: Sequence[str],
    result: consistency.EachConvergence,
    *,
    provider: str | None = None,
    last: Mapping[str, int | None] | None = None,
    transfers: int = 0,
    attempts: int = 0,
) -> NotPlaced:
    """The refusal of files still holding bytes, or unreadable, at the bound: their count, never a guess. Given the
    provider and the last attempt's counters it is an `EvictionIncomplete`, naming them and the likely cause too."""
    held = sum(int(result.values.get(fid) or 0) for fid in result.pending)
    report = PlacementReport(len(ids), held, len(result.pending), 0, tuple(sorted(result.pending))[:50])
    message = (
        f"{provisioner.where}{len(result.pending)} of {len(ids)} file(s) still hold bytes on the provider, or "
        "could not be read, at the bound"
    )
    if provider is None or last is None:
        return NotPlaced(message, report)
    label = provisioner.client.provider_label(provider)
    facts = {"providers": {label: {**last, "transfers": transfers, "attempts": attempts}}}
    return EvictionIncomplete(message + _skipped_note(label, last), report, facts)


def require_empty_each(provisioner: Provisioner, provider: str, file_ids: Iterable[str]) -> dict[str, Any]:
    """Wait, within the bound, for `provider` to hold nothing of each file, every file on consecutive polls; return
    the polls it took, or raise NotPlaced naming how many files still hold bytes."""
    ids = list(dict.fromkeys(file_ids))
    result = _converge_empty(provisioner, provider, ids, provisioner.settings.settle_timeout_s)
    if not result.converged:
        raise _not_empty(provisioner, ids, result)
    return {"files": len(ids), "polls": result.polls}


def _wait_ended(provisioner: Provisioner, provider: str, transfer_ids: list[str]) -> consistency.TransferReport:
    """Wait for the transfers to end, together; raise WaitTimeout when one has not ended at the bound."""
    settings = provisioner.settings
    return consistency.require_ended(
        consistency.wait_transfers(
            provisioner.client,
            provider,
            transfer_ids,
            timeout_s=settings.transfer_timeout_s,
            poll_s=settings.poll_s,
            workers=settings.concurrency,
            clock=provisioner.clock,
            sleep=provisioner.sleep,
            retry=provisioner.retry,
        ),
        f"{provisioner.where}eviction",
    )


def _evict_verified(
    provisioner: Provisioner,
    provider: str,
    files: Sequence[str],
    schedule: Callable[[Sequence[str]], list[str]],
    quiet: Callable[[], None] | None,
    scheduled: Callable[[Sequence[str]], None] | None = None,
) -> dict[str, Any]:
    """Schedule the evictions (`schedule` takes the files still holding bytes and returns the transfer ids, which
    `scheduled` is given when set), wait for them to end, and judge them by the block maps alone, scheduling again
    what still holds bytes after `EVICTION_CHECK_S`, up to `EVICTION_ATTEMPTS` in all; the last attempt waits the whole
    settle bound. Return the transfers, their end states, the last attempt's counters, the polls and the attempts;
    raise `EvictionIncomplete` when files still hold bytes then."""
    pending, transfers, polls = list(files), 0, 0
    states: dict[str, int] = {}
    for attempt in range(1, EVICTION_ATTEMPTS + 1):
        tids = schedule(pending)
        if scheduled is not None:
            scheduled(tids)
        transfers += len(tids)
        done = _wait_ended(provisioner, provider, tids)
        for state, count in done.states().items():
            states[state] = states.get(state, 0) + count
        counts = _counted(done.records.values())  # only the last attempt's are kept: an earlier gap is what it retried
        if quiet is not None:
            quiet()  # before each round of block-map reads: a running job could still change a map
        last = attempt == EVICTION_ATTEMPTS
        bound = provisioner.settings.settle_timeout_s if last else EVICTION_CHECK_S
        result = _converge_empty(provisioner, provider, pending, bound)
        polls += result.polls
        if result.converged:
            return {"transfers": transfers, "transfer_states": states, **counts, "polls": polls, "attempts": attempt}
        if last:
            raise _not_empty(
                provisioner, files, result, provider=provider, last=counts, transfers=transfers, attempts=attempt
            )
        pending = list(result.pending)  # completed, and these still hold bytes: never evicted; schedule them again
        provisioner.sleep(EVICTION_RETRY_PAUSE_S)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def evict_ids(
    provisioner: Provisioner,
    file_ids: Iterable[str],
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
    scheduled: Callable[[Sequence[str]], None] | None = None,
) -> dict[str, Any]:
    """Evict files, given by id, from a provider, at most `concurrency` requests at once, and return a record once
    every file's block map there shows nothing held. Never from the data provider; no id means nothing to do.
    `scheduled`, when given, is called with the ids of each attempt's transfers: the reset tells its own jobs from
    others' by them (O46)."""
    provisioner.check_eviction(from_provider)
    ids = list(dict.fromkeys(file_ids))
    if not ids:
        return {
            "files": 0,
            "transfers": 0,
            "transfer_states": {},
            "files_processed": 0,
            "files_evicted": 0,
            "files_failed": 0,
            "empty": True,
            "polls": 0,
            "attempts": 0,
            "seconds": 0.0,
        }
    began = provisioner.clock()
    provisioner.verify_space()

    def schedule(pending: Sequence[str]) -> list[str]:
        return consistency.bounded_map(
            lambda fid: provisioner.schedule_transfer("eviction", fid, from_provider),
            list(pending),
            provisioner.settings.concurrency,
        )

    done = _evict_verified(provisioner, from_provider, ids, schedule, quiet, scheduled)
    return {"files": len(ids), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


def evict_tree(
    provisioner: Provisioner,
    path: str | Entry,
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
    listed: Callable[[Sequence[str]], None] | None = None,
    scheduled: Callable[[Sequence[str]], None] | None = None,
) -> dict[str, Any]:
    """Evict every file under a directory from a provider with one transfer, then verify all of them together, the
    tree listed once; return the record. Never from the data provider; `path` (or the entry a caller resolved
    already) must be a directory. An attempt that left bytes schedules the directory's eviction again. `listed`, when
    given, is called with the ids of the files the listing found, before the transfer: the reset's quiet check holds
    those files' block maps on this provider to the empty map the eviction verified (O33), so it never lists the tree a
    second time. `scheduled`, when given, is called with each attempt's transfer id (O46), as `evict_ids` does."""
    provisioner.check_eviction(from_provider)
    began = provisioner.clock()
    provisioner.verify_space()
    directory = path if isinstance(path, Entry) else provisioner.resolve(path)
    if directory.kind != "DIR":
        raise ProvisionError(f"{provisioner.where}a tree eviction names a directory; this path is not one")
    files = [entry.file_id for entry, _ in provisioner.walk(directory.file_id) if entry.kind != "DIR"]
    if listed is not None:
        listed(files)
    done = _evict_verified(
        provisioner,
        from_provider,
        files,
        lambda _pending: [provisioner.schedule_transfer("eviction", directory.file_id, from_provider)],
        quiet,
        scheduled,
    )
    return {"files": len(files), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


__all__ = ["EvictionIncomplete", "evict_ids", "evict_tree", "require_empty_each", "sum_counts"]
