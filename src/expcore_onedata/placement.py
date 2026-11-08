# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/placement.py, blob 5b4173d17d74ec745cefca9176e8f0ab89bcd6b1
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Block placement on a provider: `GET /data/{id}/distribution`, the only placement evidence the layer accepts (O01).

A transfer's own status is not evidence: Onedata 25.0 reports `completed` for a replication that placed zero bytes and
for an eviction that removed none. Every claim about where data is comes from the block map. These are pure functions
on the JSON, so a unit test hands them a hand-written map. A range is `(offset, length)` in bytes and a provider's
blocks are the ranges it holds.
"""

from collections.abc import Iterable, Mapping
from typing import Any

Range = tuple[int, int]


def provider_blocks(distribution: Mapping[str, Any], provider_id: str) -> list[Range]:
    """Return the sorted `(offset, length)` blocks a provider holds of one file, from a `/distribution` answer.

    A provider absent from the answer holds nothing. A provider that is listed must say what it holds: a table of
    storage backends, each with a list of blocks, whose union is returned (an empty list is a real answer). An entry
    without such a table, a backend without a `blocks` list, or any other shape raises ValueError: what is held is
    unknown then, and an unknown is never read as an empty list (O01, O08), or an eviction could be confirmed by it.
    """
    per_provider = distribution.get("distributionPerProvider")
    if not isinstance(per_provider, Mapping):
        raise ValueError("the distribution answer has no 'distributionPerProvider' table")
    if provider_id not in per_provider:
        return []
    entry = per_provider[provider_id]
    backends = entry.get("distributionPerStorageBackend") if isinstance(entry, Mapping) else None
    if not isinstance(backends, Mapping) or not backends:
        raise ValueError("the distribution answer lists the provider but has no 'distributionPerStorageBackend' table")
    out: list[Range] = []
    for backend in backends.values():
        if not isinstance(backend, Mapping):
            raise ValueError("the distribution answer's storage backend entry is not a table")
        blocks = backend.get("blocks")
        if not isinstance(blocks, list | tuple):
            raise ValueError("the distribution answer's storage backend has no list of 'blocks'")
        for block in blocks:
            if not (isinstance(block, list | tuple) and len(block) == 2 and all(isinstance(n, int) for n in block)):
                raise ValueError("the distribution answer holds a block that is not an [offset, length] pair")
            out.append((block[0], block[1]))
    return sorted(out)


def merged(blocks: Iterable[Range]) -> list[Range]:
    """Return the blocks with overlapping or adjacent ones joined and empty ones dropped."""
    joined: list[list[int]] = []
    for offset, length in sorted(blocks):
        if length <= 0:
            continue
        if joined and offset <= joined[-1][0] + joined[-1][1]:
            joined[-1][1] = max(joined[-1][1], offset + length - joined[-1][0])
        else:
            joined.append([offset, length])
    return [(a, b) for a, b in joined]


def held_bytes(blocks: Iterable[Range]) -> int:
    """Return how many bytes the blocks hold, each byte once."""
    return sum(length for _, length in merged(blocks))


def uncovered(blocks: Iterable[Range], ranges: Iterable[Range]) -> list[Range]:
    """Return the parts of `ranges` the blocks do not hold; empty means fully placed."""
    held = merged(blocks)
    gaps: list[Range] = []
    for offset, length in merged(ranges):
        position, end = offset, offset + length
        for block_offset, block_length in held:
            block_end = block_offset + block_length
            if block_end <= position:
                continue
            if block_offset >= end:
                break
            if block_offset > position:
                gaps.append((position, block_offset - position))
            position = max(position, block_end)
            if position >= end:
                break
        if position < end:
            gaps.append((position, end - position))
    return gaps


def covers(blocks: Iterable[Range], ranges: Iterable[Range]) -> bool:
    """Tell whether the blocks hold every byte of every range, and at least one range was asked for."""
    wanted = list(ranges)
    return bool(merged(wanted)) and not uncovered(blocks, wanted)


def file_fully_placed(blocks: Iterable[Range], size: int) -> bool:
    """Tell whether the blocks hold the whole file; an empty file is placed by definition."""
    return not uncovered(blocks, [(0, size)]) if size > 0 else True
