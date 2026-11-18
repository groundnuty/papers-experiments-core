# Provenance: new
"""What a lease store says of itself, read for a merge by run (#462 step 4, C45): its lease, run, start, environment,
fingerprint, attempts and spend (`lease_store_of`), and the lease each of its records names (`record_leases`).

A store is read from the bytes of a snapshot the reader took (`snapshot.take_snapshot`), through `query` (SQL in, rows
out), as today's merge reads a shard (`campaign.merge.facts_of`). A store of any other form than `expcore-shard/5` is
refused, naming its form.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from expcore.campaign.merge import MergeRefused, Query
from expcore.campaign.resume import FINGERPRINT_KEY, recorded_fingerprint
from expcore.campaign.run_lease import RULE_LEASE, RunLease
from expcore.campaign.selection_ledger import Pricing, Spend, spend_of
from expcore.campaign.shard import META_ENVIRONMENT, META_SHARD, META_STARTED, ShardError, check_identity
from expcore.records.record import record_filename


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
    charged: int = 0  # its charged attempts: what a settlement's count is compared with

    @property
    def complete(self) -> bool:
        """Whether the store holds all its lease spent: its run is done or infra_exhausted. A store whose run is pending
        had attempts left, and one stopped on a harness error could be requeued, so a lost federation's snapshot of
        either can miss the attempts after it."""
        return self.status.get(self.lease.run_key) in ("done", "infra_exhausted")

    @property
    def name(self) -> str:
        """How a message names the store: its lease and run, never its path."""
        return f"the store of lease {self.lease.lease_id} (run {self.lease.run_key})"


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
        charged = int(query("SELECT COALESCE(SUM(charged), 0) FROM attempts")[0][0])
        spend = None if pricing is None else spend_of(query, pricing)
    except sqlite3.Error as error:
        raise MergeRefused(f"{where}: cannot read its store ({type(error).__name__}: {error})") from None
    return LeaseStore(
        out, snapshot, sha256, lease, started, environment, fingerprint, runs, status, spend, attempts, charged
    )


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


def record_leases(runs: Path, records: Iterable[str]) -> dict[str, int | None]:
    """The lease id each record names (expcore-record/3's optional `lease_id`), by its path relative to `runs`; None for
    a record that names none (one a store recorded before records named their lease)."""
    found: dict[str, int | None] = {}
    for rel in records:
        try:
            value = json.loads((runs / rel).read_bytes()).get("lease_id")
        except (OSError, ValueError, AttributeError) as error:
            raise MergeRefused(f"{rel}: cannot be read for the lease it names ({type(error).__name__})") from None
        found[rel] = value if isinstance(value, int) and not isinstance(value, bool) else None
    return found
