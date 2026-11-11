# Provenance: new
"""A merged batch read back (#154, C29): merge.json is believed only when it is what a merge of its snapshots writes.

The replay check binds merge.json by its hash, which says only that the file has not changed since the check. What the
file says (the count, each shard's label, start, shares and runs, the environment identity) must also be what the
snapshots say, or a batch edited after the merge (a shard dropped and the count lowered, two labels swapped) would be
checked, analysed and reported as a campaign it is not. So the check and the analysis each rebuild the document from
the snapshots they read and the study's plan, through the merge's own checks (`check_shards`: every index 1..N of
N ≥ 2 once, one campaign, the runs exactly the plan, one environment identity) and its own writer (`merge_document`),
and refuse a file that is not that document:

- `read_held` (the check) reads the snapshot of every shard merge.json lists, once each, compares its bytes with the
  hash the file names, and refuses a snapshot missing or changed, or anything under `shards/` the file does not list;
- `verify_queried` (the analysis) reads the snapshots `study.verified` already read and verified, through them;
- `verify_merged` refuses a snapshot that lies under another shard's index, then a document that differs, naming the
  fields that do.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.manifest import Manifest
from expcore.campaign.merge import (
    SHARDS_DIR,
    MergeRefused,
    Query,
    ShardFacts,
    check_shards,
    facts_of,
    facts_of_bytes,
    merge_document,
    shard_snapshot_path,
)
from expcore.campaign.shard import MERGE_FILE
from expcore.campaign.snapshot import SNAPSHOT_FILE

_SHOWN = 4


def snapshot_rel(index: int) -> str:
    """Where a merged batch keeps shard `index`'s snapshot, relative to the batch's directory."""
    return f"{SHARDS_DIR}/{index}/{SNAPSHOT_FILE}"


def read_held(out: Path, document: Mapping[str, Any]) -> dict[int, ShardFacts]:
    """What the snapshot of each shard merge.json lists says of itself, by index; each is read once and its bytes are
    compared with the hash the file names. Refuses a snapshot missing or changed, naming the shard, and anything under
    `shards/` the file does not list."""
    held: dict[int, ShardFacts] = {}
    for shard in document["shards"]:
        index = shard["index"]
        name, path = f"shard {index} ({shard['label']})", shard_snapshot_path(out, index)
        if not path.is_file():
            raise MergeRefused(f"{name}: its snapshot {path} is missing from the merged batch")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != shard["snapshot_sha256"]:
            raise MergeRefused(
                f"{name}: its snapshot changed since the merge ({MERGE_FILE} names another SHA-256); merge again"
            )
        held[index] = facts_of_bytes(data, snapshot_rel(index), out=out, snapshot=path)
    listed = {str(index) for index in held}
    stray = sorted(p.name for p in (out / SHARDS_DIR).iterdir() if not p.name.startswith(".") and p.name not in listed)
    if stray:
        raise MergeRefused(f"{SHARDS_DIR}/{stray[0]}: in the merged batch, but {MERGE_FILE} lists no such shard")
    return held


def verify_queried(document: Mapping[str, Any], queries: Mapping[int, Query], out: Path, plan: Manifest) -> None:
    """Rebuild merge.json from the snapshots of a merged batch already read and verified against the hashes it names,
    each read through its `query`, and refuse a document that differs (`verify_merged`)."""
    hashes = {shard["index"]: shard["snapshot_sha256"] for shard in document["shards"]}
    held = {
        index: facts_of(
            query, snapshot_rel(index), out=out, snapshot=shard_snapshot_path(out, index), sha256=hashes[index]
        )
        for index, query in queries.items()
    }
    verify_merged(document, held, plan)


def verify_merged(document: Mapping[str, Any], held: Mapping[int, ShardFacts], plan: Manifest) -> None:
    """Refuse merge.json unless it is the document a merge of the snapshots `held` (by the index of the directory each
    lies in) writes for `plan`: a snapshot under another shard's index, shards that are not one whole campaign of the
    plan, or a field that differs, each named."""
    if not held:
        raise MergeRefused(f"{SHARDS_DIR}/ holds no shard's snapshot; merge the shards again")
    for index, shard in sorted(held.items()):
        if shard.spec.index != index:
            raise MergeRefused(f"{snapshot_rel(index)}: is the snapshot of {shard.name}, not of shard {index}")
    try:
        expected = merge_document(check_shards([held[index] for index in sorted(held)], plan))
    except MergeRefused as error:
        raise MergeRefused(f"the merged batch's snapshots are not one whole campaign of the plan: {error}") from None
    differing = _differences(expected, document)
    if differing:
        shown = ", ".join(differing[:_SHOWN]) + (" and more" if len(differing) > _SHOWN else "")
        raise MergeRefused(
            f"{MERGE_FILE}: is not what a merge of the shards' snapshots writes for the study's plan; it differs in "
            f"{shown}; merge the shards again"
        )


def _differences(expected: Mapping[str, Any], found: Mapping[str, Any]) -> list[str]:
    """The fields of merge.json that are not the expected document's: a key, or a shard's field (`shard 1's label`)."""
    fields = []
    for key in sorted(set(expected) | set(found)):
        want, have = expected.get(key), found.get(key)
        if want == have:
            continue
        if key == "shards" and isinstance(have, list) and len(have) == len(want):
            fields += [
                f"shard {a['index']}'s {name}"
                for a, b in zip(want, have, strict=True)
                for name in sorted(set(a) | set(b))
                if a.get(name) != b.get(name)
            ]
        else:
            fields.append(key)
    return fields
