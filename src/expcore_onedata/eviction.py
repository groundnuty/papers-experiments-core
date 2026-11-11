# Provenance: new
"""Bulk eviction: many files off a provider, then verified together on their block maps (O01, O07, O09).

Evicting a reset's files one by one waits on each file's transfer and polls each file's block map in turn, a few
seconds a file, so hundreds of files take minutes. Here the waits are shared:

- **One call for a directory** (`evict_tree`). An eviction scheduled on a directory applies to every file under it
  (Oneprovider 25.0's REST description of `POST /transfers`: "In case of a directory, the transfer applies to all its
  subfiles and subdirectories (recursively)"), so a subtree leaves a provider in one transfer. The files under it are
  then listed once, one listing per directory, and verified.
- **Many files in parallel** (`evict_ids`). A set of files that is not one directory is evicted file by file, at most
  `[placement] concurrency` requests in flight at once (16 by default), and their transfers are waited on together.

Either way the transfers' end is a report, never evidence (O01): the files are then verified **together** on the
block maps (`require_empty_each`), each poll reading every file not yet settled with the same bound, and a file counts
as empty once its map shows nothing held on `[placement] consecutive_polls` polls in a row (O07). A file still holding
bytes at the bound fails the eviction, `NotPlaced` with the count, never a guess. A caller's `quiet` check runs between
the transfers' end and the first block-map read: the reset passes one that requires no transfer running on any provider,
so no map is read while another job still changes it. The data provider's own copy is never
evicted (O09): `Provisioner.check_eviction` refuses it, and a provider that does not support the space, before anything
is sent.
"""

from collections.abc import Callable, Iterable
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.placement import held_bytes
from expcore_onedata.provision import Entry, NotPlaced, PlacementReport, Provisioner, ProvisionError


def require_empty_each(provisioner: Provisioner, provider: str, file_ids: Iterable[str]) -> dict[str, Any]:
    """Wait, within the bound, for `provider` to hold nothing of each file, every file on consecutive polls; return
    the polls it took, or raise NotPlaced naming how many files still hold bytes."""
    settings, ids = provisioner.settings, list(dict.fromkeys(file_ids))
    result = consistency.converge_each(
        ids,
        lambda fid: held_bytes(provisioner.blocks(fid, provider)),
        lambda held: held == 0,
        timeout_s=settings.settle_timeout_s,
        poll_s=settings.poll_s,
        consecutive=settings.consecutive_polls,
        workers=settings.concurrency,
        sleep=provisioner.sleep,
        clock=provisioner.clock,
    )
    if not result.converged:
        held = sum(int(result.values.get(fid) or 0) for fid in result.pending)
        report = PlacementReport(len(ids), held, len(result.pending), 0, tuple(sorted(result.pending))[:50])
        raise NotPlaced(
            f"{provisioner.where}{len(result.pending)} of {len(ids)} file(s) still hold bytes on the provider, or "
            "could not be read, at the bound",
            report,
        )
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
        ),
        f"{provisioner.where}eviction",
    )
    return done.states()


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
        return {"files": 0, "transfers": 0, "transfer_states": {}, "empty": True, "polls": 0, "seconds": 0.0}
    began = provisioner.clock()
    provisioner.verify_space()
    tids = consistency.bounded_map(
        lambda fid: provisioner.schedule_transfer("eviction", fid, from_provider), ids, provisioner.settings.concurrency
    )
    states = _wait_ended(provisioner, from_provider, tids)
    if quiet is not None:
        quiet()  # before the first block-map read: a running job could still change a map
    verified = require_empty_each(provisioner, from_provider, ids)
    return {
        "files": len(ids),
        "transfers": len(tids),
        "transfer_states": states,
        "empty": True,
        "polls": verified["polls"],
        "seconds": round(provisioner.clock() - began, 3),
    }


def evict_tree(
    provisioner: Provisioner,
    path: str | Entry,
    from_provider: str,
    *,
    quiet: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Evict every file under a directory from a provider with one transfer, then verify all of them together, the
    tree listed once; return the record. Never from the data provider; `path` (or the entry a caller resolved
    already) must be a directory."""
    provisioner.check_eviction(from_provider)
    began = provisioner.clock()
    provisioner.verify_space()
    directory = path if isinstance(path, Entry) else provisioner.resolve(path)
    if directory.kind != "DIR":
        raise ProvisionError(f"{provisioner.where}a tree eviction names a directory; this path is not one")
    tid = provisioner.schedule_transfer("eviction", directory.file_id, from_provider)
    states = _wait_ended(provisioner, from_provider, [tid])
    if quiet is not None:
        quiet()
    files = [entry.file_id for entry, _ in provisioner.walk(directory.file_id) if entry.kind != "DIR"]
    verified = require_empty_each(provisioner, from_provider, files)
    return {
        "files": len(files),
        "transfers": 1,
        "transfer_states": states,
        "empty": True,
        "polls": verified["polls"],
        "seconds": round(provisioner.clock() - began, 3),
    }


__all__ = ["evict_ids", "evict_tree", "require_empty_each"]
