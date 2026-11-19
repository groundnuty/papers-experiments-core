# Provenance: new
"""merge.json `expcore-merge/2`, the layout of a batch merged by run, and the batch read back (#462 step 4, C45).

`by_run_document` writes merge.json for a merge by run that passed `campaign.merge_by_run.check_by_run`, and
`lay_out_by_run` the batch: `runs/` from each run's counted lease, `leases/<id>/` and `evidence/<id>/` for the
snapshots, beside the ledger snapshot. The check and the analysis believe merge.json only when it is what such a merge
of the batch's snapshots and its ledger writes for the study's plan (`verify_by_run`, as `campaign.merged` does for a
merge of shards): every snapshot and the ledger are read once and compared with the hash merge.json names, anything
under `leases/` or `evidence/` that it does not list is refused, and the document is rebuilt through the same checks.
merge.json also carries the ledger's declaration log (`declarations`, C48: each model's fingerprint or "none", when,
by whom and why) when the ledger keeps one, so the analysis can see when each deployment was admitted.
"""

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore.campaign.by_run_layout import EVIDENCE_DIR, LEASES_DIR, MERGE_BY_RUN_SCHEMA, snapshot_rel
from expcore.campaign.elastic.config import usd_of
from expcore.campaign.elastic.records import Declaration
from expcore.campaign.lease_facts import LeaseStore, lease_store_of, lease_store_of_bytes
from expcore.campaign.ledger_read import LEDGER_SNAPSHOT, LeaseRow, LedgerFacts, LedgerRefused, read_ledger
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.merge import RUNS_DIR, MergeRefused, Query
from expcore.campaign.merge_by_run import ByRun, LostRun, check_by_run, spend_micro, store_micro
from expcore.campaign.run_lease import RULE_LEASE
from expcore.campaign.selection_ledger import Pricing
from expcore.campaign.shard import MERGE_FILE

_DOCUMENT_KEYS = {"schema", "fingerprint", "rule", "environment_identity", "ledger_sha256", "runs", "leases"}
_DOCUMENT_KEYS |= {"evidence", "spend", "exhausted"}
DECLARATIONS_KEY = "declarations"  # the ledger's declaration log (C48), in a merge of a ledger that keeps one


# --- merge.json -----------------------------------------------------------------------------------------------------


def by_run_document(merged: ByRun) -> dict[str, Any]:
    """merge.json's content for a merge by run that passed `check_by_run`."""
    ledger, first = merged.ledger, merged.counted[0]
    totals = spend_micro(ledger, merged.counted, merged.evidence)
    settled = set(ledger.settlements)
    logged = {} if ledger.declarations is None else {DECLARATIONS_KEY: [_declared(d) for d in ledger.declarations]}
    return {
        **logged,
        "schema": MERGE_BY_RUN_SCHEMA,
        "fingerprint": fingerprint_sha256(first.fingerprint),
        "rule": RULE_LEASE,
        "environment_identity": dict(first.environment),
        "ledger_sha256": ledger.sha256,
        "runs": len(merged.counted) + len(merged.lost),
        "leases": [_entry(ledger.leases[store.lease.lease_id], store, settled) for store in merged.counted],
        "evidence": [{**_entry(item.lease, item.store, settled), "reason": item.reason} for item in merged.evidence],
        "exhausted": [_exhausted(lost) for lost in merged.lost],
        "spend": {
            "total_usd": _usd(totals.get(None)),
            "per_model_usd": {model: _usd(value) for model, value in sorted(totals.items(), key=_model_key) if model},
            "max_usd": _usd(ledger.ceilings.max_micro),
            "per_model_ceiling_usd": {
                model: usd_of(micro) for model, micro in sorted(ledger.ceilings.per_model_micro.items())
            },
            "locked_usd": usd_of(ledger.locked_micro),  # held as grants by unsettled leases at the snapshot
        },
    }


def _declared(found: Declaration) -> dict[str, Any]:
    """One entry of the declaration log as merge.json keeps it: its fingerprint is null for "none"."""
    return {
        "id": found.declaration_id,
        "at_utc": dt.datetime.fromtimestamp(found.at, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": found.model,
        "fingerprint": found.fingerprint,
        "actor": found.actor,
        "reason": found.reason,
    }


def _exhausted(lost: LostRun) -> dict[str, Any]:
    """A run the ledger exhausted for its lost leases: its run, condition and model, its charged attempts, every lease
    it was given and why each ended, and its last lease's federation and site, which place it."""
    last = lost.last
    return {
        "run_key": lost.run.run_key,
        "condition": lost.run.condition,
        "model": lost.run.model,
        "attempts": lost.run.charged,
        "lease_ids": [lease.lease_id for lease in lost.leases],
        "lease_id": last.lease_id,
        "federation": last.federation,
        "site": last.site,
        **_platform(last),
        "reasons": [_why_ended(lease) for lease in lost.leases],
    }


def _platform(row: LeaseRow) -> dict[str, str]:
    """The platform a lease's federation joined on (C50), as `platform`; nothing when the campaign declares none."""
    return {} if row.platform is None else {"platform": row.platform}


def _why_ended(lease: LeaseRow) -> str:
    """One lease of a run exhausted for its lost leases, said as an exhausted run's reason: a lost or expired lease is
    a federation lost; any other says its state (a superseded one's completion came after the run was leased again)."""
    what = "federation lost" if lease.state in ("lost", "expired") else lease.state
    return f"{what}: lease {lease.lease_id} at {lease.federation} ({lease.site})"


def _model_key(item: tuple[str | None, Any]) -> str:
    return item[0] or ""


def _usd(micro: int | None) -> float | None:
    return None if micro is None else usd_of(micro)


def _entry(row: LeaseRow, store: LeaseStore | None, settled: set[int]) -> dict[str, Any]:
    """A lease's entry: where it ran, its store's snapshot, what the ledger charges it (`spend_usd`), what its store
    reads (`store_spend_usd`, None without a store) and whether the ledger settled it."""
    read = None if store is None else store_micro(store, row)
    return {
        "lease_id": row.lease_id,
        "run_key": row.run_key,
        "federation": row.federation,
        "site": row.site,
        **_platform(row),
        "started_utc": None if store is None else store.started_utc,
        "snapshot_sha256": None if store is None else store.snapshot_sha256,
        "spend_usd": usd_of(row.charge_micro),
        "store_spend_usd": _usd(read),
        "settled": row.lease_id in settled,
    }


def merge_text(document: Mapping[str, Any]) -> str:
    """merge.json's text: sorted keys, indented, ending with a newline."""
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


def read_by_run(raw: bytes, where: object) -> dict[str, Any]:
    """Parse and validate the bytes of an `expcore-merge/2` merge.json; raise MergeRefused naming what is wrong."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise MergeRefused(f"{where}: not JSON") from None
    if (
        not isinstance(document, dict)
        or document.get("schema") != MERGE_BY_RUN_SCHEMA
        or set(document) - {DECLARATIONS_KEY} != _DOCUMENT_KEYS
    ):
        raise MergeRefused(f"{where}: not an {MERGE_BY_RUN_SCHEMA} merge file")
    logged = document.get(DECLARATIONS_KEY, [])
    if not isinstance(logged, list) or not all(isinstance(entry, dict) for entry in logged):
        raise MergeRefused(f"{where}: its {DECLARATIONS_KEY} is not a list of the ledger's declarations")
    leases, evidence, exhausted = document["leases"], document["evidence"], document["exhausted"]
    ok = isinstance(leases, list) and isinstance(evidence, list) and bool(leases) and isinstance(exhausted, list)
    ok = ok and all(isinstance(entry, dict) and isinstance(entry.get("run_key"), str) for entry in exhausted)
    ok = ok and all(isinstance(entry, dict) and isinstance(entry.get("lease_id"), int) for entry in leases + evidence)
    ok = ok and all(_is_sha(entry.get("snapshot_sha256")) for entry in leases)
    ok = ok and all(entry.get("snapshot_sha256") is None or _is_sha(entry["snapshot_sha256"]) for entry in evidence)
    ok = ok and _is_sha(document["ledger_sha256"])
    ids = [entry["lease_id"] for entry in leases + evidence] if ok else []
    if not ok or len(set(ids)) != len(ids):
        raise MergeRefused(f"{where}: its lists of leases are not each lease once, with its snapshot's hash")
    return document


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64


def differences(expected: Mapping[str, Any], found: Mapping[str, Any]) -> list[str]:
    """The top-level fields of merge.json that are not the expected document's."""
    return sorted(key for key in set(expected) | set(found) if expected.get(key) != found.get(key))


def ledger_path(out: Path) -> Path:
    """Where a batch merged by run keeps its ledger snapshot."""
    return out / LEDGER_SNAPSHOT


def verify_ledger_bytes(out: Path, document: Mapping[str, Any]) -> bytes:
    """The ledger snapshot's bytes, read once and compared with the hash merge.json names."""
    path = ledger_path(out)
    if not path.is_file():
        raise MergeRefused(f"{LEDGER_SNAPSHOT}: missing from the merged batch")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != document["ledger_sha256"]:
        raise MergeRefused(f"{LEDGER_SNAPSHOT}: changed since the merge (merge.json names another SHA-256)")
    return data


# --- the layout, and a merged batch read back -------------------------------------------------------------------------


def lay_out_by_run(stage: Path, merged: ByRun, staged: Sequence[LeaseStore]) -> dict[str, Any]:
    """Write the batch into `stage` (which holds the ledger snapshot already) and return merge.json's content.

    `staged` are the counted stores with `out` set to the private copy whose records were verified: each run's records
    are moved from it into `runs/`, each counted snapshot under `leases/`, each evidence snapshot under `evidence/`."""
    runs = stage / RUNS_DIR
    runs.mkdir(parents=True)
    for store in staged:
        for entry in sorted((store.out / RUNS_DIR).iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if (runs / entry.name).exists():
                raise MergeRefused(f"{store.name}: {RUNS_DIR}/{entry.name} is already there from another lease")
            os.replace(entry, runs / entry.name)
    placed = [(LEASES_DIR, store) for store in merged.counted]
    placed += [(EVIDENCE_DIR, item.store) for item in merged.evidence if item.store is not None]
    for directory, store in placed:
        target = stage / snapshot_rel(directory, store.lease.lease_id)
        target.parent.mkdir(parents=True)
        os.replace(store.snapshot, target)
    document = by_run_document(merged)
    (stage / MERGE_FILE).write_text(merge_text(document), encoding="utf-8")
    return document


def read_held_by_run(
    out: Path,
    document: Mapping[str, Any],
    pricing: Pricing | None,
    queries: Mapping[int, Query] | None = None,
) -> tuple[LedgerFacts, list[LeaseStore]]:
    """The ledger and every lease store a merged batch keeps, each snapshot read once and compared with the hash
    merge.json names; `queries` gives the counted snapshots the analysis already read and verified, by lease id.
    Refuses a snapshot missing or changed, and anything under `leases/` or `evidence/` merge.json does not list."""
    data = verify_ledger_bytes(out, document)
    try:
        ledger = read_ledger(data, document["ledger_sha256"])
    except LedgerRefused as error:
        raise MergeRefused(str(error)) from None
    stores = []
    for directory, entries in ((LEASES_DIR, document["leases"]), (EVIDENCE_DIR, document["evidence"])):
        listed = [entry for entry in entries if entry["snapshot_sha256"] is not None]
        for entry in listed:
            lease_id, sha256 = entry["lease_id"], entry["snapshot_sha256"]
            rel, path = snapshot_rel(directory, lease_id), out / snapshot_rel(directory, lease_id)
            if directory == LEASES_DIR and queries is not None and lease_id in queries:
                stores.append(lease_store_of(queries[lease_id], rel, out=out, snapshot=path, sha256=sha256,
                                             pricing=pricing))  # fmt: skip
                continue
            if not path.is_file():
                raise MergeRefused(f"{rel}: missing from the merged batch")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise MergeRefused(f"{rel}: changed since the merge ({MERGE_FILE} names another SHA-256); merge again")
            stores.append(lease_store_of_bytes(data, rel, out=out, snapshot=path, pricing=pricing))
        _refuse_stray(out / directory, {str(entry["lease_id"]) for entry in listed})
    return ledger, stores


def _refuse_stray(directory: Path, listed: set[str]) -> None:
    stray = sorted(p.name for p in directory.iterdir() if p.name not in listed) if directory.is_dir() else []
    if stray:
        raise MergeRefused(f"{directory.name}/{stray[0]}: in the merged batch, but {MERGE_FILE} lists no such lease")


def verify_by_run(
    document: Mapping[str, Any],
    out: Path,
    plan: Manifest,
    pricing: Pricing | None,
    queries: Mapping[int, Query] | None = None,
) -> list[LeaseStore]:
    """Refuse merge.json unless it is the document a merge by run of its snapshots and its ledger writes for `plan`;
    return the counted stores in plan order."""
    ledger, stores = read_held_by_run(out, document, pricing, queries)
    try:
        merged = check_by_run(ledger, stores, plan, pricing)
        expected = by_run_document(merged)
    except MergeRefused as error:
        raise MergeRefused(f"the merged batch's snapshots are not one whole campaign of the plan: {error}") from None
    differing = differences(expected, document)
    if differing:
        raise MergeRefused(
            f"{MERGE_FILE}: is not what a merge by run of its snapshots and its ledger writes for the study's plan; "
            f"it differs in {', '.join(differing)}; merge again"
        )
    return list(merged.counted)
