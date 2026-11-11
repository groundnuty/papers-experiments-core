# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/mplus.py, blob 2f5fe937abe6f53f8cce023ad17e8c960e7053a0
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Range placement: put declared byte ranges of files on a provider by reading them through it (O02).

Replication jobs move whole files. To have a provider hold only some ranges of a file, `place_ranges` reads those
ranges through that provider, which fetches what it lacks from the data provider on the fly. It sends range reads and
never a transfer job. Each placement is then confirmed by the block map on the receiving provider (O01, `provision.py`):
a read that returned bytes but left the block map short, as a provider that does not cache would, is `NotPlaced`.

The map is declared, per file and provider (`rangemap.py`); a range beyond the end of its file is refused, since
clipping it would place less than was declared. The receiving provider's block map before and after is recorded
(`held_before`, `held_after`), so bytes fetched later by a run's own reads show as growth and do not pass as placed,
and per file (`per_file`) the ranges asked for beside the ranges the block map holds: a provider places more than it
is asked (Onedata 25.0 places at least 1 MiB from a read's offset, clipped to the file), and what is recorded as held
is always what the block map shows, never the request.

The wait for the block map to show the placement is a consistency wait of provisioning (O07). Call `place_ranges`
before a run starts or after it ends, never inside the agent's time.
"""

from collections.abc import Iterable
from typing import Any

from expcore_onedata.placement import Range, held_bytes, merged
from expcore_onedata.provision import NotPlaced, Provisioner, ProvisionError
from expcore_onedata.rangemap import RangeRow


def _chunks(ranges: Iterable[Range], limit: int) -> Iterable[Range]:
    """Split ranges so that no single read is longer than `limit` bytes."""
    for offset, length in ranges:
        while length > 0:
            step = min(length, limit)
            yield offset, step
            offset, length = offset + step, length - step


def _held(provisioner: Provisioner, provider: str, file_id: str, row: RangeRow) -> dict[str, Any]:
    """One file's ranges asked for beside what the provider's block map holds of it now, as the probes read it."""
    held = merged(provisioner.blocks(file_id, provider))
    return {
        "asked": [list(r) for r in row.ranges],
        "asked_bytes": sum(length for _, length in row.ranges),
        "held": [list(r) for r in held],
        "held_bytes": held_bytes(held),
    }


def place_ranges(provisioner: Provisioner, rows: Iterable[RangeRow], *, start_empty: bool = False) -> dict[str, Any]:
    """Place every row's ranges on its receiving provider by range reads, and return a record once the block map shows them.

    With `start_empty` a provider must hold nothing of the rows' files before the reads (the cold-start rule: bytes
    cached by an earlier run would otherwise pass as placed); it raises `NotPlaced` when one does. The record lists per
    provider the files and bytes asked for, the bytes read, what was held before and after, and the polls it took.
    """
    rows = list(rows)
    if not rows:
        raise ProvisionError("no ranges were declared to place")
    provisioner.verify_space()
    entries = {row.file: provisioner.resolve(row.file) for row in rows}
    for row in rows:
        size = entries[row.file].size
        if any(offset + length > size for offset, length in row.ranges):
            raise ProvisionError("a declared range lies beyond the end of its file; nothing was read")
    providers = list(dict.fromkeys(row.provider for row in rows))
    record: dict[str, Any] = {"providers": {}}
    for provider in providers:
        mine = [row for row in rows if row.provider == provider]
        wanted = {entries[row.file].file_id: list(row.ranges) for row in mine}
        # the receiver must see each file before anything is read through it
        provisioner.await_visible(provider, wanted)
        before = provisioner.holds(provider, wanted)
        if start_empty and before:
            raise NotPlaced(f"the provider already holds {before} B of the files, so it does not start empty")
        read = 0
        for row in mine:
            for offset, length in _chunks(row.ranges, provisioner.settings.chunk_bytes):
                read += provisioner.read_through(provider, entries[row.file].file_id, offset, length)
        confirmation = provisioner.confirm_placed(provider, wanted)
        record["providers"][provider] = {
            "files": len(wanted),
            "ranges": sum(len(row.ranges) for row in mine),
            "bytes_asked": sum(length for row in mine for _, length in row.ranges),
            "bytes_read": read,
            "held_before": before,
            "held_after": confirmation.report.held_bytes,
            "polls": confirmation.polls,
            "placement": confirmation.report.to_json(),
            "per_file": {row.file: _held(provisioner, provider, entries[row.file].file_id, row) for row in mine},
        }
        if not confirmation.placed:
            raise NotPlaced(
                f"the reads returned {read} B but the block map is {confirmation.report.bytes_short} B short",
                confirmation.report,
            )
    return record
