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

The wait for the transfers to end polls each one's record, and a poll that times out ends the eviction, so those polls
are tried again under the provisioner's policy (O29, `readretry.py`), within the wait's own bound. The block-map reads
of the verification are inside a poll that counts a failed read as one that did not match, and stay one attempt.
"""

from collections.abc import Callable, Iterable, Sequence
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


def _not_empty(provisioner: Provisioner, ids: Sequence[str], result: consistency.EachConvergence) -> NotPlaced:
    """The refusal of files still holding bytes, or unreadable, at the bound: their count, never a guess."""
    held = sum(int(result.values.get(fid) or 0) for fid in result.pending)
    report = PlacementReport(len(ids), held, len(result.pending), 0, tuple(sorted(result.pending))[:50])
    return NotPlaced(
        f"{provisioner.where}{len(result.pending)} of {len(ids)} file(s) still hold bytes on the provider, or "
        "could not be read, at the bound",
        report,
    )


def require_empty_each(provisioner: Provisioner, provider: str, file_ids: Iterable[str]) -> dict[str, Any]:
    """Wait, within the bound, for `provider` to hold nothing of each file, every file on consecutive polls; return
    the polls it took, or raise NotPlaced naming how many files still hold bytes."""
    ids = list(dict.fromkeys(file_ids))
    result = _converge_empty(provisioner, provider, ids, provisioner.settings.settle_timeout_s)
    if not result.converged:
        raise _not_empty(provisioner, ids, result)
    return {"files": len(ids), "polls": result.polls}


def _wait_ended(provisioner: Provisioner, provider: str, transfer_ids: list[str]) -> dict[str, int]:
    """Wait for the transfers to end, together; raise WaitTimeout when one has not ended at the bound."""
    settings = provisioner.settings
    done = consistency.require_ended(
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
    return done.states()


def _evict_verified(
    provisioner: Provisioner,
    provider: str,
    files: Sequence[str],
    schedule: Callable[[Sequence[str]], list[str]],
    quiet: Callable[[], None] | None,
) -> dict[str, Any]:
    """Schedule the evictions (`schedule` takes the files still holding bytes and returns the transfer ids), wait for
    them to end, and judge them by the block maps alone, scheduling again what still holds bytes after
    `EVICTION_CHECK_S`, up to `EVICTION_ATTEMPTS` in all; the last attempt waits the whole settle bound. Return the
    transfers, their end states, the polls and the attempts; raise NotPlaced when files still hold bytes then."""
    pending, transfers, polls = list(files), 0, 0
    states: dict[str, int] = {}
    for attempt in range(1, EVICTION_ATTEMPTS + 1):
        tids = schedule(pending)
        transfers += len(tids)
        for state, count in _wait_ended(provisioner, provider, tids).items():
            states[state] = states.get(state, 0) + count
        if quiet is not None:
            quiet()  # before each round of block-map reads: a running job could still change a map
        last = attempt == EVICTION_ATTEMPTS
        bound = provisioner.settings.settle_timeout_s if last else EVICTION_CHECK_S
        result = _converge_empty(provisioner, provider, pending, bound)
        polls += result.polls
        if result.converged:
            return {"transfers": transfers, "transfer_states": states, "polls": polls, "attempts": attempt}
        if last:
            raise _not_empty(provisioner, files, result)
        pending = list(result.pending)  # completed, and these still hold bytes: never evicted; schedule them again
        provisioner.sleep(EVICTION_RETRY_PAUSE_S)
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def evict_ids(
    provisioner: Provisioner,
    file_ids: Iterable[str],
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Evict files, given by id, from a provider, at most `concurrency` requests at once, and return a record once
    every file's block map there shows nothing held. Never from the data provider; no id means nothing to do."""
    provisioner.check_eviction(from_provider)
    ids = list(dict.fromkeys(file_ids))
    if not ids:
        return {
            "files": 0,
            "transfers": 0,
            "transfer_states": {},
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

    done = _evict_verified(provisioner, from_provider, ids, schedule, quiet)
    return {"files": len(ids), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


def evict_tree(
    provisioner: Provisioner,
    path: str | Entry,
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Evict every file under a directory from a provider with one transfer, then verify all of them together, the
    tree listed once; return the record. Never from the data provider; `path` (or the entry a caller resolved
    already) must be a directory. An attempt that left bytes schedules the directory's eviction again."""
    provisioner.check_eviction(from_provider)
    began = provisioner.clock()
    provisioner.verify_space()
    directory = path if isinstance(path, Entry) else provisioner.resolve(path)
    if directory.kind != "DIR":
        raise ProvisionError(f"{provisioner.where}a tree eviction names a directory; this path is not one")
    files = [entry.file_id for entry, _ in provisioner.walk(directory.file_id) if entry.kind != "DIR"]
    done = _evict_verified(
        provisioner,
        from_provider,
        files,
        lambda _pending: [provisioner.schedule_transfer("eviction", directory.file_id, from_provider)],
        quiet,
    )
    return {"files": len(files), **done, "empty": True, "seconds": round(provisioner.clock() - began, 3)}


__all__ = ["evict_ids", "evict_tree", "require_empty_each"]
