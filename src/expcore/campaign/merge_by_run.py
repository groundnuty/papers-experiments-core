# Provenance: new
"""The merge of an elastic campaign by run (#462 step 4, C43): the checks, merge.json `expcore-merge/2`, the layout.

`python -m expcore merge OUT STORE_OUT... --ledger LEDGER` takes the campaign's ledger and the `out/` of lease stores
(`expcore-shard/5`, one run each, `campaign.run_lease`) and writes one batch under `OUT`:

    OUT/runs/                                     the records of each run's counted lease, as one batch
    OUT/leases/<lease id>/campaign-snapshot.sqlite the snapshot of each counted lease's store
    OUT/evidence/<lease id>/campaign-snapshot.sqlite the snapshot of every other lease store given (never analysed)
    OUT/ledger-snapshot.sqlite                    the ledger, as the merge read it (`campaign.ledger_read`)
    OUT/merge.json                                `expcore-merge/2`: what was merged, and each snapshot's SHA-256

**What a merge by run refuses, naming the lease or the run and the reason** (`check_by_run`):

1. a ledger whose tables are not what its events give (`ledger_read`), or whose runs are not the study's plan;
2. two stores of one lease; a store of a lease the ledger never issued, or of a cancelled or live lease; a store
   whose pinned lease names another run, another campaign or another grant than the ledger's; a store that holds any
   run but its own;
3. stores of another campaign (the fingerprint, the core first of all, `merge.check_fingerprints`) or another
   environment (`merge.check_environment`), or than the one the ledger pinned at its first join (`environment_digest`
   of the stores' identity); a campaign whose fingerprint is not the one the ledger was made for;
4. **not exactly one counted lease per run:** a run of the plan that is not terminal in the ledger (it has no counted
   lease yet), a run whose counted lease's store is not given (its archive is the run's only record), a counted store
   whose run ended otherwise than the ledger says; a store of a counted or superseded lease that is not given (the
   ledger verified its archive, so it is evidence to keep);
5. spend: every lease's spend (its store's, as the budget gate counts it; the ledger's charge for a lease whose store
   is not given: the grant of a lost or expired one, the spend of a returned one) together above `max_usd` or a
   model's ceiling of the ledger, a store that spent more than its grant, or a spend unknown under a budget.

Then each counted store passes the replay check (`study.merge_by_run`), and its records are exactly those of its
attempts (`require_records`): none it does not account for, and the record of every attempt it holds as done.
Today's checks hold for every store given, so a store of another campaign or environment is refused even when it is
only evidence.

**Evidence.** A lease store that is not its run's counted lease is kept under `evidence/` and in merge.json's
`evidence`, with why (`reason`): `superseded` (its completion came from a lease that was no longer the run's latest),
`earlier` (it counted, and its run went back to pending and was leased again), `lost` (its archive never verified),
`expired` or `returned`. Its spend is counted; its records are never in `runs/`, never checked as the batch, never
analysed. A store of a lost or expired lease that still holds a running attempt (its federation was lost mid-run) is
settled by `recover` in the merge's private copy as an infrastructure loss (`study.merge_by_run`); its spend is then
its bound. A lost or expired lease with no store given is charged its grant, as the ledger charges it.

**merge.json** (`MERGE_BY_RUN_SCHEMA`, written and read back by `campaign.merged_by_run`) holds `fingerprint`,
`rule`, `environment_identity`, `ledger_sha256`, `runs` (how many the plan holds), `leases` (each counted lease in
plan order: `lease_id`, `run_key`, `federation`, `site`, `started_utc`, `snapshot_sha256`, `spend_usd`), `evidence`
(in lease order: the same and `reason`; `started_utc` and `snapshot_sha256` None with no store) and `spend`
(`total_usd`, `per_model_usd`, `max_usd`, `per_model_ceiling_usd`). Federations and sites appear as the operator's
labels the ledger holds. The check and the analysis rebuild it from the snapshots, the ledger snapshot and the study's
plan through these checks, and refuse a file that differs.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from expcore.campaign.elastic.config import micro_down, micro_up, usd_of
from expcore.campaign.ledger_read import LeaseRow, LedgerFacts
from expcore.campaign.manifest import Manifest, fingerprint_sha256
from expcore.campaign.merge import (
    MergeRefused,
    Query,
    check_environment,
    check_fingerprints,
    sha256_of,
)
from expcore.campaign.resume import FINGERPRINT_KEY, recorded_fingerprint
from expcore.campaign.run_lease import RULE_LEASE, RunLease
from expcore.campaign.selection_ledger import Pricing, Spend, spend_of
from expcore.campaign.shard import (
    META_ENVIRONMENT,
    META_SHARD,
    META_STARTED,
    ShardError,
    check_identity,
)
from expcore.campaign.snapshot import SNAPSHOT_FILE
from expcore.records.record import record_filename

MERGE_BY_RUN_SCHEMA = "expcore-merge/2"
LEASES_DIR = "leases"
EVIDENCE_DIR = "evidence"
_EVIDENCE_REASONS = {"counted": "earlier", "superseded": "superseded", "lost": "lost", "expired": "expired"}
_EVIDENCE_REASONS |= {"returned": "returned", "reported": "superseded"}
_KEPT = ("counted", "superseded")  # a lease whose archive the ledger verified: its store is evidence to keep


@dataclass(frozen=True)
class LeaseStore:
    """What a lease store's snapshot says of itself, read once from the copy the merge took."""

    out: Path
    snapshot: Path
    snapshot_sha256: str
    lease: RunLease
    started_utc: str
    environment: Mapping[str, str]
    fingerprint: Mapping[str, Any]
    runs: Mapping[str, Mapping[str, Any]]  # the factors of each run the store holds, by run key
    status: Mapping[str, str]  # the status of each run the store holds, by run key
    spend: Spend | None = None
    # where each attempt's record lies in a batch (`<digest>/try<N>/<record file>`), with the attempt's state
    attempts: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """How a message names the store: its lease and run, never its path."""
        return f"the store of lease {self.lease.lease_id} (run {self.lease.run_key})"


def snapshot_rel(directory: str, lease_id: int) -> str:
    """Where a merged batch keeps a lease store's snapshot, relative to the batch's directory."""
    return f"{directory}/{lease_id}/{SNAPSHOT_FILE}"


def lease_store_of_bytes(
    data: bytes, where: object, *, out: Path, snapshot: Path, pricing: Pricing | None
) -> LeaseStore:
    """What the lease store whose snapshot's bytes are `data` says of itself (`lease_store_of`)."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        try:
            db.deserialize(data)
        except sqlite3.Error as error:
            raise MergeRefused(f"{where}: not a campaign store ({type(error).__name__}: {error})") from None
        query: Query = lambda sql, args=(): db.execute(sql, tuple(args)).fetchall()  # noqa: E731
        sha256 = hashlib.sha256(data).hexdigest()
        return lease_store_of(query, where, out=out, snapshot=snapshot, sha256=sha256, pricing=pricing)
    finally:
        db.close()


def lease_store_of(
    query: Query, where: object, *, out: Path, snapshot: Path, sha256: str, pricing: Pricing | None
) -> LeaseStore:
    """What a lease store says of itself, read through `query`; refuses a store that is not a lease store, or lacks
    its start, its environment identity or its fingerprint. Its spend is read when `pricing` is given."""
    try:
        meta = {row[0]: row[1] for row in query("SELECT key, value FROM meta")}
        lease = _lease_of(meta, where)
        started = meta.get(META_STARTED)
        if not isinstance(started, str) or not started:
            raise MergeRefused(f"{where}: its store records no {META_STARTED!r}")
        try:
            environment = check_identity(json.loads(meta.get(META_ENVIRONMENT, "null")))
        except (ShardError, ValueError) as error:
            raise MergeRefused(f"{where}: the environment identity in its store is unusable: {error}") from None
        fingerprint = recorded_fingerprint([(meta[FINGERPRINT_KEY],)] if FINGERPRINT_KEY in meta else [])
        if fingerprint is None:
            raise MergeRefused(f"{where}: its store records no campaign fingerprint")
        rows = query("SELECT run_key, factors, status FROM runs")
        runs = {row[0]: json.loads(row[1]) for row in rows}
        status = {row[0]: row[2] for row in rows}
        attempts = {
            f"{row[0]}/{PurePath(row[1]).name}/{record_filename(row[2], row[3])}": row[4]
            for row in query(
                "SELECT r.digest, a.run_dir, a.run_key, a.attempt, a.state FROM attempts a "
                "JOIN runs r ON r.run_key = a.run_key WHERE a.run_dir IS NOT NULL ORDER BY a.id"
            )
        }
        spend = None if pricing is None else spend_of(query, pricing)
    except sqlite3.Error as error:
        raise MergeRefused(f"{where}: cannot read its store ({type(error).__name__}: {error})") from None
    return LeaseStore(out, snapshot, sha256, lease, started, environment, fingerprint, runs, status, spend, attempts)


def _lease_of(meta: Mapping[str, str], where: object) -> RunLease:
    """The lease a store pins as its shard spec; refuses a store of any other form."""
    try:
        pinned = json.loads(meta[META_SHARD]) if META_SHARD in meta else None
    except ValueError:
        raise MergeRefused(f"{where}: {META_SHARD!r} in its store is not JSON") from None
    if not isinstance(pinned, Mapping) or pinned.get("rule") != RULE_LEASE:
        raise MergeRefused(
            f"{where}: not a store of a run taken on a lease (expcore-shard/5); merge it by its own form"
        )
    try:
        return RunLease.from_json(pinned)
    except ShardError as error:
        raise MergeRefused(f"{where}: the lease in its store is unusable: {error}") from None


# --- the checks ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A lease that is not its run's counted lease, and its store when one was given."""

    lease: LeaseRow
    reason: str
    store: LeaseStore | None


@dataclass(frozen=True)
class ByRun:
    """A merge by run that passed the checks: each run's counted store in plan order, and the evidence."""

    counted: tuple[LeaseStore, ...]
    evidence: tuple[Evidence, ...]
    ledger: LedgerFacts


def check_by_run(ledger: LedgerFacts, stores: Sequence[LeaseStore], plan: Manifest, pricing: Pricing | None) -> ByRun:
    """Run checks 1 to 5 of the module docstring; raise MergeRefused for the first fault."""
    if not stores:
        raise MergeRefused("a merge by run takes the store of every run's counted lease; none was given")
    by_lease = _by_lease(stores)
    for store in stores:
        _check_binding(store, ledger)
    ordered = sorted(stores, key=lambda store: store.lease.lease_id)
    check_fingerprints(ordered)
    if fingerprint_sha256(ordered[0].fingerprint) != ledger.config.fingerprint:
        raise MergeRefused(
            f"{ordered[0].name}: ran the campaign with fingerprint sha256 "
            f"{fingerprint_sha256(ordered[0].fingerprint)[:12]}, and the ledger is the one of "
            f"{ledger.config.fingerprint[:12]}"
        )
    check_environment(ordered)
    _check_ledger_environment(ordered[0], ledger)
    counted = _counted(ledger, by_lease, plan)
    evidence = _evidence(ledger, by_lease, {store.lease.lease_id for store in counted})
    _check_spend(ledger, counted, evidence)
    return ByRun(tuple(counted), tuple(evidence), ledger)


def environment_digest(identity: Mapping[str, str]) -> str:
    """The environment a federation joins the ledger with (`elastic.Ledger.join`): the SHA-256 of its environment
    identity's canonical JSON, the identity a lease store pins (`META_ENVIRONMENT`)."""
    text = json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_ledger_environment(store: LeaseStore, ledger: LedgerFacts) -> None:
    """3: the stores' environment identity is the one the ledger pinned at its first join."""
    if ledger.environment != environment_digest(store.environment):
        raise MergeRefused(
            f"{store.name}: ran against an environment whose identity has digest "
            f"{environment_digest(store.environment)[:12]}, and the ledger pinned "
            f"{str(ledger.environment)[:12]} at its first join: another environment than the ledger's"
        )


def require_records(stores: Sequence[LeaseStore], records: Iterable[str]) -> None:
    """The records of a batch merged by run (paths relative to its `runs/`) are exactly the counted stores': each
    lies where an attempt of one of them lies, and every attempt a store holds as done has its record. A record of an
    evidence lease, or one a store does not account for, is refused, and so is a counted run without its record."""
    expected = {rel: state for store in stores for rel, state in store.attempts.items()}
    found = set(records)
    stray = sorted(found - set(expected))
    if stray:
        raise MergeRefused(f"{stray[0]}: a record no attempt of a counted lease's store accounts for")
    missing = sorted(rel for rel, state in expected.items() if state == "done" and rel not in found)
    if missing:
        raise MergeRefused(f"{missing[0]}: the record of a counted lease's final attempt is missing")


def _by_lease(stores: Sequence[LeaseStore]) -> dict[int, LeaseStore]:
    held: dict[int, LeaseStore] = {}
    for store in stores:
        if store.lease.lease_id in held:
            raise MergeRefused(f"{store.name} is given twice: give each lease's store once")
        held[store.lease.lease_id] = store
    return held


def _check_binding(store: LeaseStore, ledger: LedgerFacts) -> None:
    """2: the store's pinned lease is one the ledger issued, for this run, campaign and grant, and it holds its run."""
    row = ledger.leases.get(store.lease.lease_id)
    if row is None:
        raise MergeRefused(f"{store.name}: the ledger never issued lease {store.lease.lease_id}")
    if row.run_key != store.lease.run_key:
        raise MergeRefused(f"{store.name}: the ledger issued lease {row.lease_id} for run {row.run_key}")
    if store.lease.campaign_sha256 != ledger.config.fingerprint:
        raise MergeRefused(f"{store.name}: its lease was issued for another campaign than the ledger's")
    if store.lease.grant_micro_usd != row.grant_micro:
        raise MergeRefused(
            f"{store.name}: its lease grants {store.lease.grant_micro_usd} micro-dollars, the ledger {row.grant_micro}"
        )
    if row.state in ("cancelled", "issued", "sent"):
        raise MergeRefused(f"{store.name}: the ledger holds lease {row.lease_id} as {row.state}, so no store is its")
    if set(store.runs) != {store.lease.run_key}:
        others = sorted(set(store.runs) - {store.lease.run_key})
        raise MergeRefused(f"{store.name}: holds {others[0] if others else 'no run'}, not exactly its own run")


def _counted(ledger: LedgerFacts, by_lease: Mapping[int, LeaseStore], plan: Manifest) -> list[LeaseStore]:
    """1 and 4: the ledger's runs are the plan's, each terminal, and its counted lease's store is given and agrees."""
    planned = {row.key.text: row.seq for row in plan.rows}
    held = {key: run.seq for key, run in ledger.runs.items()}
    if held != planned:
        extra = sorted(set(held) - set(planned)) or sorted(key for key in held if held[key] != planned.get(key))
        missing = sorted(set(planned) - set(held))
        named = f"run {extra[0]} is not the plan's" if extra else f"run {missing[0]} of the plan is not in it"
        raise MergeRefused(f"the ledger's runs are not the study's plan: {named}")
    counted = []
    for key in sorted(planned, key=planned.__getitem__):
        run = ledger.runs[key]
        lease_id = ledger.counted(key)
        if lease_id is None:
            raise MergeRefused(
                f"run {key} is {run.state} in the ledger: a campaign is merged by run once every run has its "
                "counted lease"
            )
        if ledger.leases[lease_id].state != "counted":
            raise MergeRefused(f"run {key}: its latest lease {lease_id} is {ledger.leases[lease_id].state}")
        store = by_lease.get(lease_id)
        if store is None:
            raise MergeRefused(
                f"run {key}: the store of its counted lease {lease_id} is not given; its records are the run's only "
                "ones, so the merge cannot go on without them"
            )
        if store.status.get(key) != run.state:
            raise MergeRefused(
                f"{store.name}: its run is {store.status.get(key)} in its store and {run.state} in the ledger"
            )
        counted.append(store)
    return counted


def _evidence(ledger: LedgerFacts, by_lease: Mapping[int, LeaseStore], counted: set[int]) -> list[Evidence]:
    """Every lease but the counted ones and the cancelled, with its reason; a kept lease's store must be given."""
    found = []
    for lease in sorted(ledger.leases.values(), key=lambda row: row.lease_id):
        if lease.lease_id in counted or lease.state == "cancelled":
            continue
        reason = _EVIDENCE_REASONS.get(lease.state)
        if reason is None or (lease.state == "reported" and lease.verdict != "superseded"):
            raise MergeRefused(f"lease {lease.lease_id} of run {lease.run_key} is {lease.state} in the ledger")
        store = by_lease.get(lease.lease_id)
        if store is None and lease.state in _KEPT:
            raise MergeRefused(
                f"lease {lease.lease_id} of run {lease.run_key} is {lease.state} in the ledger, which verified its "
                "archive, and its store is not given: give it, as the evidence it is"
            )
        found.append(Evidence(lease, reason, store))
    return found


def _check_spend(ledger: LedgerFacts, counted: Sequence[LeaseStore], evidence: Sequence[Evidence]) -> None:
    """5: every lease's spend within its grant, and together within the ledger's `max_usd` and each ceiling."""
    totals = spend_micro(ledger, counted, evidence)
    config = ledger.config
    if config.max_usd is None:
        return
    if totals.get(None) is None or any(value is None for value in totals.values()):
        raise MergeRefused("the campaign has a budget and some lease's spend is unknown, so it cannot be checked")
    if totals[None] > micro_down(config.max_usd):
        raise MergeRefused(
            f"the leases spent USD {usd_of(totals[None])} together, above the campaign's max_usd {config.max_usd}"
        )
    for model, ceiling in sorted(config.per_model_usd.items()):
        if totals.get(model, 0) > micro_down(ceiling):
            raise MergeRefused(
                f"the leases of model {model} spent USD {usd_of(totals[model])} together, above its ceiling {ceiling}"
            )


def spend_micro(
    ledger: LedgerFacts, counted: Sequence[LeaseStore], evidence: Sequence[Evidence]
) -> dict[str | None, int | None]:
    """Each lease's spend in micro-dollars, summed in total (under None) and per model; None where one is unknown.
    A store's spend is the budget gate's figure; a lease without a store is charged what the ledger charges it."""
    leases = [(ledger.leases[store.lease.lease_id], store) for store in counted]
    leases += [(item.lease, item.store) for item in evidence]
    totals: dict[str | None, int | None] = {None: 0}
    for row, store in leases:
        micro = row.charge_micro if store is None else store_micro(store, row)
        for scope in (None, row.model):
            have = totals.get(scope, 0)
            totals[scope] = None if have is None or micro is None else have + micro
    return totals


def store_micro(store: LeaseStore, row: LeaseRow) -> int | None:
    """A store's spend in micro-dollars; refused above its grant, when the grant held it (a grant of 0 has no gate)."""
    spent = None if store.spend is None else store.spend.total
    if spent is None:
        return None
    micro = micro_up(spent)
    if row.grant_micro and micro > row.grant_micro:
        raise MergeRefused(f"{store.name}: spent USD {spent}, above its grant of USD {usd_of(row.grant_micro)}")
    return micro


__all__ = ["LeaseStore", "MergeRefused", "sha256_of"]
