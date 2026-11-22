# Provenance: new
"""The replication package of a batch merged by run (C45, merge.json `expcore-merge/2`, `campaign.merged_by_run`).

Such a batch keeps, beside `runs/`, each counted lease's store snapshot (`leases/<lease id>/`), the evidence of every
other lease store the merge was given (`evidence/<lease id>/`, never analysed), the ledger snapshot and merge.json,
which names each snapshot's SHA-256 and the ledger's. The package holds merge.json, the ledger snapshot, every counted
lease's snapshot and the evidence `spec.packaged_evidence` lets in (`spec.MergedByRun`); merge.json lists every
evidence lease by hash whether or not its snapshot goes in. A merge of shards (`expcore-merge/1`) is packaged as
before, by `identity`.

- `verify_inputs`: every build, before anything is read or rewritten, holds each snapshot the package takes to the
  bytes merge.json names (merge.json itself is held to the bytes the replay check hashed, `identity`), naming the
  first that is missing or differs by its place in the package.
- `anonymise`: an anonymised build rewrites each snapshot it takes without the environment's sources, as the merge of
  shards' snapshots are rewritten. That changes the campaign fingerprint's SHA-256, which the merge by run binds in
  three places beside merge.json: the fingerprint each snapshot holds, the lease each store pins (`campaign_sha256`)
  and the ledger's configuration (`fingerprint`). Each of them moves from the old hash to the new one, the ledger
  snapshot is rewritten for it, and merge.json is written as the merge by run writes it of the rewritten files: the
  fingerprint's hash, the ledger's and each rewritten snapshot's change, nothing else does. The analysis rebuilds
  merge.json from the snapshots, the ledger and the plan and refuses one that differs, so the package's own check is
  what proves the rewrite whole.

The merge by run's modules load the elastic ledger's, so they are imported on this branch only (C42).
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import META_SHARD
from expcore.campaign.shard_run import canonical
from expcore.package.spec import MERGE_PATH, MergedByRun, PackageError, by_run_files
from expcore.provenance.environment import without_sources

LEDGER_CONFIG = "config"  # the ledger's meta key of its pinned configuration (`elastic.LedgerConfig.to_json`)


def verify_inputs(files: Mapping[str, bytes], raw: bytes) -> MergedByRun:
    """Refuse a batch merged by run whose ledger snapshot, or one of whose counted (or packaged evidence) snapshots,
    is not the bytes merge.json (`raw`) names, naming the first; return what the package holds of the batch."""
    merged = by_run_files(raw)
    for path, sha256 in merged.named():
        data = files.get(path)
        if data is None or hashlib.sha256(data).hexdigest() != sha256:
            state = "is missing, though" if data is None else "differs from"
            raise PackageError(f"{path} {state} the snapshot {MERGE_PATH} names; merge by run again")
    return merged


def anonymise(files: dict[str, bytes], scratch: Path) -> dict[str, str]:
    """Rewrite, in `files` (mutated in place: the caller's copy of the package), every snapshot of the batch merged by
    run the package holds without the environment's sources, the campaign fingerprint's hash wherever the merge binds
    it, and merge.json from the rewritten files (module docstring). Returns each old hash with its new one, the
    snapshots', the ledger's and the fingerprint's: the export lists them, so the author's is compared with them
    swapped (`identity._outputs`)."""
    from expcore.campaign.merged_by_run import merge_text  # on this branch only (C42)

    merged = by_run_files(files[MERGE_PATH])
    document: dict[str, Any] = json.loads(json.dumps(merged.document))  # a copy of every level, to rewrite
    swaps: dict[str, str] = {}
    fingerprints: dict[str, str] = {}  # the old fingerprint hash, with its new one
    for path, sha256 in merged.snapshots():
        files[path] = _store_without_sources(files[path], path, scratch, fingerprints)
        swaps[sha256] = hashlib.sha256(files[path]).hexdigest()
    old_ledger = document["ledger_sha256"]
    files[merged.ledger] = _ledger_rewritten(files[merged.ledger], merged.ledger, scratch, fingerprints)
    swaps[old_ledger] = document["ledger_sha256"] = hashlib.sha256(files[merged.ledger]).hexdigest()
    for entry in [*document["leases"], *document["evidence"]]:
        entry["snapshot_sha256"] = swaps.get(entry["snapshot_sha256"], entry["snapshot_sha256"])
    document["fingerprint"] = fingerprints.get(document["fingerprint"], document["fingerprint"])
    files[MERGE_PATH] = merge_text(document).encode("utf-8")
    return swaps | fingerprints


def _store_without_sources(data: bytes, where: str, scratch: Path, fingerprints: dict[str, str]) -> bytes:
    """A lease store's snapshot with no source in its fingerprint's environment and its pinned lease issued for the
    fingerprint so rewritten, rebuilt so that no old copy of either remains; records the old hash and the new one."""
    meta = _meta(data, where, scratch, (FINGERPRINT_KEY, META_SHARD))
    fingerprint, pinned = json.loads(meta[FINGERPRINT_KEY]), json.loads(meta[META_SHARD])
    if not isinstance(fingerprint, dict) or "environment" not in fingerprint or not isinstance(pinned, dict):
        raise PackageError(f"{where}: records no environment or no lease; run the campaign with this core")
    old = fingerprint_sha256(fingerprint)
    fingerprint["environment"] = without_sources(fingerprint["environment"])
    new = fingerprints.setdefault(old, fingerprint_sha256(fingerprint))
    if pinned.get("campaign_sha256") == old:  # another hash is the analysis's to refuse, as it was before
        pinned["campaign_sha256"] = new
    return _rewritten(scratch, {FINGERPRINT_KEY: canonical(fingerprint), META_SHARD: canonical(pinned)})


def _ledger_rewritten(data: bytes, where: str, scratch: Path, fingerprints: Mapping[str, str]) -> bytes:
    """The ledger snapshot with its configuration pinning the rewritten fingerprint's hash, in the text the ledger
    writes it in (`LedgerConfig.to_json`: sorted keys); its events never name the fingerprint."""
    config = json.loads(_meta(data, where, scratch, (LEDGER_CONFIG,))[LEDGER_CONFIG])
    if not isinstance(config, dict) or config.get("fingerprint") not in fingerprints:
        raise PackageError(f"{where}: pins another campaign fingerprint than its lease snapshots hold")
    config["fingerprint"] = fingerprints[config["fingerprint"]]
    return _rewritten(scratch, {LEDGER_CONFIG: json.dumps(config, sort_keys=True)})


def _meta(data: bytes, where: str, scratch: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """Write the snapshot's bytes to the scratch file and read the meta values `keys` name; refuse one it lacks."""
    path = scratch / "snapshot.sqlite"
    path.write_bytes(data)
    connection = sqlite3.connect(path)
    try:
        rows = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error as error:
        raise PackageError(f"{where}: cannot be read ({type(error).__name__})") from None
    finally:
        connection.close()
    missing = [key for key in keys if key not in rows]
    if missing:
        raise PackageError(f"{where}: records no {missing[0]!r}; run the campaign with this core")
    return {key: rows[key] for key in keys}


def _rewritten(scratch: Path, values: Mapping[str, str]) -> bytes:
    """The scratch snapshot with the meta `values` set, vacuumed so that no old value remains in its pages."""
    path = scratch / "snapshot.sqlite"
    connection = sqlite3.connect(path)
    try:
        for key, value in values.items():
            connection.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    data = path.read_bytes()
    path.unlink()
    return data
