# Provenance: new
"""The replication package of a batch merged by run (C45, merge.json `expcore-merge/2`, `campaign.merged_by_run`).

Such a batch keeps, beside `runs/`, each counted lease's store snapshot (`leases/<lease id>/`), the snapshot of every
other lease store the merge was given (`evidence/<lease id>/`, never analysed as the batch, read by a declared
sensitivity), the ledger snapshot and merge.json, which names each snapshot's SHA-256 and the ledger's. The package
holds all of them (`spec.MergedByRun`); an evidence lease that left no store is listed in merge.json with no hash and
has no file. A merge of shards (`expcore-merge/1`) is packaged as before, by `identity`.

- `verify_inputs`: every build, before anything is read or rewritten, holds each snapshot to the bytes merge.json
  names (merge.json itself is held to the bytes the replay check hashed, `identity`), naming the first that is missing
  or differs by its place in the package.
- `anonymise`: an anonymised build rewrites each lease store's snapshot without the environment's sources, as the merge
  of shards' snapshots are rewritten. That changes the campaign fingerprint's SHA-256, which the merge by run binds in
  three more places, and an anonymised build re-pins exactly those: each store's pinned lease (`campaign_sha256`), the
  ledger snapshot's configuration (its `fingerprint`; no event and no other table is touched, and its events never
  name it), and merge.json, written as the merge by run writes it of the rewritten files (its `fingerprint`,
  `ledger_sha256` and snapshot hashes change, nothing else does). A place that does not hold the original hash is
  refused. The analysis rebuilds merge.json from the snapshots, the ledger and the plan and refuses one that differs,
  and any place left at the original hash, so the package's own check is what proves the re-pin whole. The original
  hash and the re-pinned one (`Repinned`) go into the package's `PACKAGE.json`, which keeps the anonymised package
  traceable to the plain one.

The merge by run's modules load the elastic ledger's, so they are imported on this branch only (C42).
"""

import copy
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.by_run_layout import is_by_run
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.merge import CHECK_KEY as MERGE_KEY
from expcore.campaign.resume import FINGERPRINT_KEY
from expcore.campaign.shard import META_SHARD
from expcore.campaign.shard_run import canonical
from expcore.package.spec import MERGE_PATH, MergedByRun, PackageError, by_run_files
from expcore.provenance.environment import without_sources

LEDGER_CONFIG = "config"  # the ledger's meta key of its pinned configuration (`elastic.LedgerConfig.to_json`)


@dataclass(frozen=True)
class Repinned:
    """The campaign fingerprint's SHA-256 as the batch merged by run binds it (`original`), and as the anonymised
    package re-pins it: the hash of the fingerprint without the environment's sources (`sha256`)."""

    original: str
    sha256: str


def require_bound(root: Path, check: Mapping[str, Any]) -> None:
    """Refuse a study whose out/merge.json is a merge by run while its replay check names no merge: that file lets the
    spec select out/leases and out/evidence (`spec.merged_by_run`), so a stale one beside an unmerged batch would put
    snapshots no check vouched for in the package. A check that names a merge binds merge.json by its hash, which the
    build compares (`identity.verify_merged_inputs`)."""
    path = Path(root) / MERGE_PATH
    if MERGE_KEY in check or path.is_symlink() or not path.is_file() or not is_by_run(path.read_bytes()):
        return
    raise PackageError(
        f"{MERGE_PATH}: a merge by run, but the replay check was made without it; run `python -m expcore check` again"
    )


def verify_inputs(files: Mapping[str, bytes], raw: bytes) -> MergedByRun:
    """Refuse a batch merged by run whose ledger snapshot, or one of whose lease store snapshots, is not the bytes
    merge.json (`raw`) names, naming the first; return what the package holds of the batch."""
    merged = by_run_files(raw)
    for path, sha256 in merged.named():
        data = files.get(path)
        if data is None or hashlib.sha256(data).hexdigest() != sha256:
            state = "is missing, though" if data is None else "differs from"
            raise PackageError(f"{path} {state} the snapshot {MERGE_PATH} names; merge by run again")
    return merged


def anonymise(files: dict[str, bytes], scratch: Path) -> tuple[dict[str, str], Repinned]:
    """Rewrite, in `files` (mutated in place: the caller's copy of the package), every lease store's snapshot without
    the environment's sources and re-pin the campaign fingerprint's hash in the three places (module docstring).
    Returns each old hash with its new one, the snapshots' and the ledger's (the export lists them, so the author's is
    compared with them swapped, `identity._outputs`), and the fingerprint's re-pin. The fingerprint's hash is not
    swapped: the export carries it only in its campaign fingerprint, which is derived again (`identity`), so a text that
    quotes it, a declaration's reason say, is compared as it is."""
    from expcore.campaign.merged_by_run import merge_text  # on this branch only (C42)

    merged = by_run_files(files[MERGE_PATH])
    stores = {path: _store_facts(files[path], path, scratch) for path, _ in merged.snapshots()}
    pins = {
        Repinned(fingerprint_sha256(found), fingerprint_sha256(_without_sources(found))) for found, _ in stores.values()
    }
    if len(pins) != 1:
        raise PackageError(f"{MERGE_PATH}: its lease snapshots hold more than one campaign fingerprint")
    (repinned,) = pins
    swaps: dict[str, str] = {}
    for path, sha256 in merged.snapshots():
        fingerprint, pinned = stores[path]
        values = {FINGERPRINT_KEY: _without_sources(fingerprint), META_SHARD: _repin_lease(pinned, repinned, path)}
        files[path] = _rewritten(files[path], path, scratch, {key: canonical(value) for key, value in values.items()})
        swaps[sha256] = hashlib.sha256(files[path]).hexdigest()
    config = json.loads(_meta(files[merged.ledger], merged.ledger, scratch, (LEDGER_CONFIG,))[LEDGER_CONFIG])
    config_text = json.dumps(_repin_ledger(config, repinned, merged.ledger), sort_keys=True)  # `LedgerConfig.to_json`
    files[merged.ledger] = _rewritten(files[merged.ledger], merged.ledger, scratch, {LEDGER_CONFIG: config_text})
    swaps[merged.document["ledger_sha256"]] = hashlib.sha256(files[merged.ledger]).hexdigest()
    document = _repin_merge(copy.deepcopy(dict(merged.document)), repinned)
    for entry in [*document["leases"], *document["evidence"]]:
        entry["snapshot_sha256"] = swaps.get(entry["snapshot_sha256"], entry["snapshot_sha256"])
    document["ledger_sha256"] = swaps[document["ledger_sha256"]]
    files[MERGE_PATH] = merge_text(document).encode("utf-8")
    return swaps, repinned


def _store_facts(data: bytes, where: str, scratch: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """A lease store snapshot's campaign fingerprint and the lease it pins; refuse a snapshot that lacks either."""
    meta = _meta(data, where, scratch, (FINGERPRINT_KEY, META_SHARD))
    fingerprint, pinned = json.loads(meta[FINGERPRINT_KEY]), json.loads(meta[META_SHARD])
    if not isinstance(fingerprint, dict) or "environment" not in fingerprint or not isinstance(pinned, dict):
        raise PackageError(f"{where}: records no environment or no lease; run the campaign with this core")
    return fingerprint, pinned


def _without_sources(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """The campaign fingerprint with no source in its environment: what the anonymised snapshot holds."""
    return {**fingerprint, "environment": without_sources(fingerprint["environment"])}


def _repin_lease(pinned: Mapping[str, Any], repinned: Repinned, where: str) -> dict[str, Any]:
    """The first place: the lease a store pins, issued for the re-pinned fingerprint."""
    if pinned.get("campaign_sha256") != repinned.original:
        raise PackageError(f"{where}: its lease was issued for another campaign fingerprint than its store holds")
    return {**pinned, "campaign_sha256": repinned.sha256}


def _repin_ledger(config: Mapping[str, Any], repinned: Repinned, where: str) -> dict[str, Any]:
    """The second place: the ledger's pinned configuration, made for the re-pinned fingerprint."""
    if config.get("fingerprint") != repinned.original:
        raise PackageError(f"{where}: pins another campaign fingerprint than its lease snapshots hold")
    return {**config, "fingerprint": repinned.sha256}


def _repin_merge(document: dict[str, Any], repinned: Repinned) -> dict[str, Any]:
    """The third place: merge.json's fingerprint, as the merge by run writes it of the rewritten snapshots."""
    if document["fingerprint"] != repinned.original:
        raise PackageError(f"{MERGE_PATH}: names another campaign fingerprint than its lease snapshots hold")
    return {**document, "fingerprint": repinned.sha256}


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


def _rewritten(data: bytes, where: str, scratch: Path, values: Mapping[str, str]) -> bytes:
    """The snapshot with the meta `values` set, vacuumed so that no old value remains in its pages."""
    path = scratch / "snapshot.sqlite"
    path.write_bytes(data)
    connection = sqlite3.connect(path)
    try:
        for key, value in values.items():
            if connection.execute("UPDATE meta SET value=? WHERE key=?", (value, key)).rowcount != 1:
                raise PackageError(f"{where}: records no {key!r}; run the campaign with this core")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    rewritten = path.read_bytes()
    path.unlink()
    return rewritten
