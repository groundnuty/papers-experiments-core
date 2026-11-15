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
are tried again under the provisioner's policy (O29, `readretry.py`), within the wait's own bound, or under the policy
a caller hands in (`reads`): the reset's eviction stage hands its stage's, which tries a connection failure again too
(O46), and under which the space's name (`verify_space`), the tree's listings (`walk`) and the mutation gate's reads
before each eviction are read as well. The block-map reads
of the verification are inside a poll that counts a failed read as one that did not match, and stay one attempt.

**An eviction whose answer was lost is never sent again** (O09, O46). One that went past the gate and got no answer
back whole (`client.sent_unanswered`: a connection failure, a reset or a timeout once it was sent) may or may not have
been applied, and a second would be a second job on the transfer listing. It is not retried, and no later attempt
schedules its files (or its tree) again: with no transfer id there is no record to wait on, so the caller's `quiet`
check, which waits for every job listed waiting or ongoing, the lost one among them, and then the block maps decide,
the same verification as for any other attempt. An eviction already applied counts as done; one that never was leaves
bytes the last attempt's bound fails closed on. The record counts such evictions (`unanswered`) when there was one.
Only a caller that gives a `quiet` check judges a lost eviction so; without one the failure is raised, as it always
was. The reads before the request (the visibility wait) are made apart, so one that fails is a failed read, never a
lost eviction.

**A caller can watch.** `scheduled` is told each attempt's transfer ids when they are sent, `ended` the same ids once
their records have reported an ended state on the evicting provider (before `quiet` runs), and a `consistency.WaitLog`
given as `waits` logs every wait the eviction takes, by why: `transfers_ending` (the records' polls), `block_maps` (the
verification's polls) and `retry_pause` (before an attempt again). The reset records them (`reset.py`).
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError, sent_unanswered
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
from expcore_onedata.readretry import ReadRetry
from expcore_onedata.transferrecord import eviction_counters

COUNTERS = ("files_processed", "files_evicted", "files_failed")  # the record's keys of `eviction_counters`, in order
log = logging.getLogger(__name__)
# files -> (their transfer ids, the files whose eviction's answer was lost, how many evictions' answers were lost)
Schedule = Callable[[Sequence[str]], tuple[list[str], list[str], int]]


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


def _sleep(provisioner: Provisioner, waits: consistency.WaitLog | None, why: str) -> Callable[[float], None]:
    """The provisioner's sleep, each wait logged under `why` when the caller keeps a log of them."""
    return provisioner.sleep if waits is None else waits.sleeper(why, provisioner.sleep)


def _converge_empty(
    provisioner: Provisioner,
    provider: str,
    ids: Sequence[str],
    timeout_s: float,
    waits: consistency.WaitLog | None = None,
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
        sleep=_sleep(provisioner, waits, "block_maps"),
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


def _wait_ended(
    provisioner: Provisioner,
    provider: str,
    transfer_ids: list[str],
    waits: consistency.WaitLog | None,
    reads: ReadRetry,
) -> consistency.TransferReport:
    """Wait for the transfers to end, together, their records read under `reads`; raise WaitTimeout when one has not
    ended at the bound."""
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
            sleep=_sleep(provisioner, waits, "transfers_ending"),
            retry=reads,
        ),
        f"{provisioner.where}eviction",
    )


@dataclass(frozen=True)
class _Watch:
    """What a caller is told of an eviction's jobs and waits: `scheduled` gets each attempt's transfer ids when they
    are sent, `ended` the same ids once each record has reported an ended state on the evicting provider, and `waits`
    logs every wait the eviction takes, by why (`transfers_ending`, `block_maps`, `retry_pause`); `reads` is the policy
    of the waits' record reads (`evict_ids`), the provisioner's when the caller names none."""

    scheduled: Callable[[Sequence[str]], None] | None = None
    ended: Callable[[Sequence[str]], None] | None = None
    waits: consistency.WaitLog | None = None
    reads: ReadRetry | None = None


def _sent(
    provisioner: Provisioner, provider: str, entry_id: str, *, judged: bool, reads: ReadRetry | None
) -> str | None:
    """Send one eviction of a file or a directory (`entry_id`) from `provider`, its gate's reads under `reads`, and
    return its transfer id. What must hold before it goes is read first and apart: the space was verified by the caller,
    and the entry's visibility there (`Provisioner.await_visible`, its reads under `reads` too, each under a slot) is
    waited for here, so a read that fails before the request is sent propagates as the failed read it is;
    `schedule_transfer` then finds both kept and reads neither again. Only the request itself can come back unanswered
    (`client.sent_unanswered`): when the caller judges every eviction by the block maps once no transfer is listed
    running (`judged`, the reset's `quiet`), that is None, and it is never sent again; otherwise it is raised, as it
    always was. Any other failure propagates, a request the gate refused unsent (`NotSent`, `ScopeUnread`) among
    them."""
    provisioner.await_visible(provider, [entry_id], retry=reads)
    try:
        return provisioner.schedule_transfer("eviction", entry_id, provider, gate_retry=reads)
    except PlatformError as error:
        if not judged or not sent_unanswered(error):
            raise
        log.warning(
            "%s%s: an eviction was sent and its answer was lost (%s); it is never sent again, the block maps decide",
            provisioner.where,
            provisioner.client.provider_label(provider),
            error,
        )
        return None


def _evict_verified(
    provisioner: Provisioner,
    provider: str,
    files: Sequence[str],
    schedule: Schedule,
    quiet: Callable[[], None] | None,
    watch: _Watch,
) -> dict[str, Any]:
    """Schedule the evictions (`schedule` takes the files still holding bytes and returns the transfer ids, which
    `watch.scheduled` is given when set, and `watch.ended` once they have ended, with the files whose eviction's
    answer was lost and how many evictions those were), wait for them to end, and judge them by the block maps alone,
    scheduling again what still holds bytes after `EVICTION_CHECK_S`, up to `EVICTION_ATTEMPTS` in all, but never a
    file whose eviction's answer was lost (it may have been applied, O09); the last attempt waits the whole settle
    bound. Return the transfers, their end states, the last attempt's counters, the polls, the attempts and, when there
    were any, how many evictions' answers were lost (`unanswered`); raise `EvictionIncomplete` when files still hold
    bytes then."""
    pending, transfers, polls = list(files), 0, 0
    states: dict[str, int] = {}
    lost: set[str] = set()  # files whose eviction went unanswered: never scheduled again
    unanswered = 0  # the evictions whose answers were lost
    for attempt in range(1, EVICTION_ATTEMPTS + 1):
        sending = [file_id for file_id in pending if file_id not in lost]
        tids, unread, count = schedule(sending) if sending else ([], [], 0)
        lost.update(unread)
        unanswered += count
        if watch.scheduled is not None:
            watch.scheduled(tids)
        transfers += len(tids)
        done = _wait_ended(provisioner, provider, tids, watch.waits, watch.reads or provisioner.retry)
        if watch.ended is not None:
            watch.ended(tids)
        for state, count in done.states().items():
            states[state] = states.get(state, 0) + count
        counts = _counted(done.records.values())  # only the last attempt's are kept: an earlier gap is what it retried
        if quiet is not None:
            quiet()  # before each round of block-map reads: a running job could still change a map
        last = attempt == EVICTION_ATTEMPTS
        bound = provisioner.settings.settle_timeout_s if last else EVICTION_CHECK_S
        result = _converge_empty(provisioner, provider, pending, bound, watch.waits)
        polls += result.polls
        noted = {"unanswered": unanswered} if unanswered else {}
        if result.converged:
            return {
                "transfers": transfers,
                "transfer_states": states,
                **counts,
                "polls": polls,
                "attempts": attempt,
                **noted,
            }
        if last:
            raise _not_empty(
                provisioner,
                files,
                result,
                provider=provider,
                last={**counts, **noted},
                transfers=transfers,
                attempts=attempt,
            )
        pending = list(result.pending)  # completed, and these still hold bytes: never evicted; schedule them again
        _sleep(provisioner, watch.waits, "retry_pause")(EVICTION_RETRY_PAUSE_S)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def evict_ids(
    provisioner: Provisioner,
    file_ids: Iterable[str],
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
    scheduled: Callable[[Sequence[str]], None] | None = None,
    ended: Callable[[Sequence[str]], None] | None = None,
    waits: consistency.WaitLog | None = None,
    reads: ReadRetry | None = None,
) -> dict[str, Any]:
    """Evict files, given by id, from a provider, at most `concurrency` requests at once, and return a record once
    every file's block map there shows nothing held. Never from the data provider; no id means nothing to do.
    `scheduled`, when given, is called with the ids of each attempt's transfers: the reset tells its own jobs from
    others' by them (O46); `ended` with the same ids once their records have reported an ended state on the evicting
    provider, before `quiet` is called; `waits` logs every wait the eviction takes, by why; `reads` is the policy of
    the waits' record reads, of the space's name and of each eviction's gate (the provisioner's by default; the reset's
    stage hands its own, O46). An eviction whose answer was lost is never sent again (`unanswered`)."""
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
    provisioner.verify_space(retry=reads)

    def send(fid: str) -> str | None:
        return _sent(provisioner, from_provider, fid, judged=quiet is not None, reads=reads)

    def schedule(pending: Sequence[str]) -> tuple[list[str], list[str], int]:
        sent = consistency.bounded_map(send, list(pending), provisioner.settings.concurrency)
        lost = [fid for fid, tid in zip(pending, sent, strict=True) if tid is None]
        return [tid for tid in sent if tid is not None], lost, len(lost)

    watch = _Watch(scheduled, ended, waits, reads)
    done = _evict_verified(provisioner, from_provider, ids, schedule, quiet, watch)
    return {"files": len(ids), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


def evict_tree(
    provisioner: Provisioner,
    path: str | Entry,
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
    listed: Callable[[Sequence[str]], None] | None = None,
    scheduled: Callable[[Sequence[str]], None] | None = None,
    ended: Callable[[Sequence[str]], None] | None = None,
    waits: consistency.WaitLog | None = None,
    reads: ReadRetry | None = None,
) -> dict[str, Any]:
    """Evict every file under a directory from a provider with one transfer, then verify all of them together, the
    tree listed once; return the record. Never from the data provider; `path` (or the entry a caller resolved
    already) must be a directory. An attempt that left bytes schedules the directory's eviction again. `listed`, when
    given, is called with the ids of the files the listing found, before the transfer: the reset's quiet check holds
    those files' block maps on this provider to the empty map the eviction verified (O33), so it never lists the tree a
    second time. `scheduled`, `ended`, `waits` and `reads` are as `evict_ids` takes them; the tree's listings go under
    `reads` too. A directory's eviction whose answer was lost is never sent again, for any file under it."""
    provisioner.check_eviction(from_provider)
    began = provisioner.clock()
    provisioner.verify_space(retry=reads)
    directory = path if isinstance(path, Entry) else provisioner.resolve(path, retry=reads)
    if directory.kind != "DIR":
        raise ProvisionError(f"{provisioner.where}a tree eviction names a directory; this path is not one")
    files = [entry.file_id for entry, _ in provisioner.walk(directory.file_id, retry=reads) if entry.kind != "DIR"]
    if listed is not None:
        listed(files)

    def schedule(pending: Sequence[str]) -> tuple[list[str], list[str], int]:
        tid = _sent(provisioner, from_provider, directory.file_id, judged=quiet is not None, reads=reads)
        return ([tid], [], 0) if tid is not None else ([], list(pending), 1)

    done = _evict_verified(provisioner, from_provider, files, schedule, quiet, _Watch(scheduled, ended, waits, reads))
    return {"files": len(files), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


__all__ = ["EvictionIncomplete", "evict_ids", "evict_tree", "require_empty_each", "sum_counts"]
