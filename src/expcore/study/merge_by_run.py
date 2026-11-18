# Provenance: new
"""`python -m expcore merge OUT STORE_OUT... --ledger LEDGER`: an elastic campaign merged by run (#462 step 4, C45).

The study is the one above `OUT` (or `--study`). The merge snapshots the ledger it is given (`campaign.ledger_read`),
reads every lease store's `out/` the way today's merge reads a shard's (its lock held as a reader, its store backed up
from a private copy, its `NOT-FINAL.json` refused), checks them against the ledger and the plan
(`campaign.merge_by_run.check_by_run`), replays the records of each run's counted lease in a private copy as `check`
does (`study.merge._replay`), and writes the batch. Nothing is written to `OUT` unless every check passes, and no
store given is ever written.

**A lost federation's store.** A store whose lease the ledger holds as lost or expired may still hold a running
attempt: its federation was lost mid-run, and its store came back from the shared results directory as it was then.
Today's merge refuses a store with a running attempt. This one settles it in its own private copy: the store and its
records are copied, the copy is marked copied back and its attempts' directories pointed at the copied records
(`snapshot.rebase_run_dirs`), and `recover` makes each running attempt interrupted and uncharged, charged at its bound
since its call log is never trusted on the copy's word (C05). The settled copy is the evidence the batch keeps: its
spend counts, its records are never analysed (by the ledger's verdict on the lease, never by what an attempt says). A
store of any other lease with a running attempt is refused.
"""

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.elastic.config import micro_up
from expcore.campaign.lease_facts import LeaseStore, lease_store_of_bytes, record_leases
from expcore.campaign.ledger_read import LEDGER_SNAPSHOT, LedgerFacts, LedgerRefused, read_ledger, take_ledger_snapshot
from expcore.campaign.lock import CampaignLocked, LockMissing, LockUnavailable, ReaderLock
from expcore.campaign.merge import RUNS_DIR, MergeRefused, copy_runs, make_writable, refuse_non_empty, sha256_of
from expcore.campaign.merge_by_run import check_by_run, require_records
from expcore.campaign.merged_by_run import lay_out_by_run
from expcore.campaign.recovery import recover
from expcore.campaign.scheduler import STATE_FILE
from expcore.campaign.shard import META_SHARD, NOT_FINAL_FILE
from expcore.campaign.snapshot import AttemptsRunning, SnapshotError, rebase_run_dirs, take_snapshot
from expcore.campaign.store import Store
from expcore.campaign.storecopy import StoreChanged, copy_store
from expcore.errors import study_kinds
from expcore.provenance.codestamp import require_known
from expcore.provenance.core_check import require_core
from expcore.records.record import is_record_name
from expcore.study.config import StudyConfig, find_study_root, load_study
from expcore.study.merge import REPLAY_DIR, _refuse_inside_a_shard, _replay, refuse_earlier_form
from expcore.study.pipeline import campaign_environment, environment_like
from expcore.study.selection_start import pricing_of

_SETTLED_LEASES = ("lost", "expired")  # a lease whose store may hold a running attempt, settled in its copy


@dataclass(frozen=True)
class MergedByRun:
    """What a merge by run wrote: the batch's directory, its runs and how many leases it kept as evidence."""

    out: Path
    runs: int
    evidence: int


def merge_by_run(
    out: str | os.PathLike[str],
    store_outs: Sequence[str | os.PathLike[str]],
    *,
    ledger: str | os.PathLike[str],
    study_root: str | os.PathLike[str] | None = None,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
) -> MergedByRun:
    """Merge the lease stores whose `out/` directories are `store_outs` into the batch `out`, under the ledger at
    `ledger`; see the module docstring. Raises MergeRefused naming the lease or run and the reason."""
    out = Path(out)
    study = load_study(find_study_root(study_root if study_root is not None else out))
    with study_kinds(study.kinds):  # a replayed record may end in one of the study's own kinds
        return _merge(study, out, [Path(path) for path in store_outs], Path(ledger), allow_dirty, code_root)


def _merge(
    study: StudyConfig, out: Path, outs: list[Path], ledger: Path, allow_dirty: bool, code_root: Any
) -> MergedByRun:
    _refuse_inside_a_shard(out, outs)
    refuse_non_empty(out)
    code = require_known("merge", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    require_core("merge", allow_dirty=allow_dirty)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / f".{out.name}.merge-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    try:
        facts = _ledger_of(ledger, stage / LEDGER_SNAPSHOT)
        pricing = pricing_of(study)
        stores = [_read_store(path, stage, n, facts, study, pricing) for n, path in enumerate(outs)]
        mismatch = pricing.mismatch(stores[0].fingerprint) if stores else None
        if mismatch is not None:
            raise MergeRefused(f"the study's prices are not the campaign's: {mismatch}")
        merged = check_by_run(facts, stores, study.manifest, pricing)
        refuse_earlier_form(merged.counted)
        env = environment_like(study, campaign_environment(merged.counted[0].fingerprint))
        copies = stage / REPLAY_DIR
        copies.mkdir(mode=0o700)
        staged = [_replay(study, store, code, env, copies / str(n)) for n, store in enumerate(merged.counted)]
        for store in staged:  # its records are exactly those of its attempts: none stray, none missing
            require_records([store], _records_in(store.out / RUNS_DIR))
        lay_out_by_run(stage, merged, staged)
        shutil.rmtree(copies)
        try:
            os.replace(stage, out)
        except OSError as error:
            raise MergeRefused(f"{out}: cannot be written ({type(error).__name__}: {error})") from None
    finally:
        if stage.exists():
            make_writable(stage)
        shutil.rmtree(stage, ignore_errors=True)
    return MergedByRun(out, len(merged.counted), len(merged.evidence))


def _records_in(runs: Path) -> dict[str, int | None]:
    """Every record file under a `runs/` directory, by its path relative to it, with the lease id it names."""
    found = [str(path.relative_to(runs)) for path in sorted(runs.rglob("*")) if is_record_name(path.name)]
    return record_leases(runs, found)


def _ledger_of(source: Path, snapshot: Path) -> LedgerFacts:
    """The ledger's snapshot, taken into the batch's staging directory, and its facts."""
    try:
        take_ledger_snapshot(source, snapshot)
        return read_ledger(snapshot.read_bytes(), sha256_of(snapshot))
    except LedgerRefused as error:
        raise MergeRefused(str(error)) from None


def _read_store(out: Path, stage: Path, n: int, ledger: LedgerFacts, study: StudyConfig, pricing: Any) -> LeaseStore:
    """Snapshot the lease store whose `out/` is `out` into the staging directory and read it; a lost or expired
    lease's store with a running attempt is settled in a private copy first (module docstring)."""
    scratch = stage / f"read-{n}.sqlite"
    try:
        with ReaderLock(out):
            try:
                take_snapshot(out / STATE_FILE, scratch)
            except AttemptsRunning:
                _settle(out, stage / f"settle-{n}", scratch, ledger, study)
    except (CampaignLocked, LockMissing, LockUnavailable) as error:
        raise MergeRefused(f"{out}: {error}; a lease store is merged when its run has ended") from None
    except SnapshotError as error:
        raise MergeRefused(str(error)) from None
    store = lease_store_of_bytes(scratch.read_bytes(), out, out=out, snapshot=scratch, pricing=pricing)
    if os.path.lexists(out / NOT_FINAL_FILE):
        raise MergeRefused(f"{store.name}: its results are marked not final ({NOT_FINAL_FILE})")
    return store


def _settle(out: Path, private: Path, scratch: Path, ledger: LedgerFacts, study: StudyConfig) -> None:
    """Settle a store with running attempts in a private copy (`_settled_copy`) when the ledger holds its lease as
    lost or expired, and refuse any other."""
    lease_id, interrupted = _settled_copy(out, private, scratch, study.schema, ledger.config.max_attempts)
    row = ledger.leases.get(lease_id) if lease_id is not None else None
    if row is None or row.state not in _SETTLED_LEASES:
        held = "not a lease the ledger issued" if row is None else f"held by the ledger as {row.state}"
        raise MergeRefused(
            f"{out}: its store still has a running attempt, and its lease is {held}: only a lost or expired "
            "lease's store is settled in the merge's copy"
        )
    print(
        f"merge: lease {lease_id} ({row.state} in the ledger): {interrupted} running attempt(s) settled as "
        "interrupted in the merge's private copy, an infrastructure loss charged at its bound; its store is not "
        "written",
        file=sys.stderr,
    )


def _settled_copy(out: Path, private: Path, scratch: Path, schema: Any, max_attempts: int) -> tuple[int | None, int]:
    """Copy the store at `out` and its records into the new directory `private`, mark the copy copied back and point
    its attempts at the copied records (`snapshot.rebase_run_dirs`: its call logs are then never trusted on its own
    word), settle it with `recover`, and snapshot it to `scratch`; return its pinned lease id and how many running
    attempts became interrupted. The store at `out` is never written. Raises SnapshotError or StoreChanged."""
    private.mkdir(mode=0o700)
    try:
        copied = copy_store(out / STATE_FILE, private)
        runs = private / RUNS_DIR
        if (out / RUNS_DIR).is_dir():
            copy_runs(out / RUNS_DIR, runs)
        rebase_run_dirs(copied, runs)
        store = Store(copied, schema)
        try:
            now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            settled = recover(store, now, max_attempts)
            lease_id = _pinned_lease_id(store)
        finally:
            store.close()
        take_snapshot(copied, scratch)
    finally:
        shutil.rmtree(private, ignore_errors=True)
    return lease_id, settled.interrupted


@dataclass(frozen=True)
class DeadStoreSpend:
    """What a dead lease store spent, read as a merge by run reads it: `spend_micro` (whole micro-dollars, as the
    ledger counts them) and its charged attempts (`charged_attempts`), with its lease and run. `settled_running` is how
    many attempts were still running in the store and were settled in a private copy at their bound: a store that
    came back from a federation lost mid-run is a snapshot of it, and an attempt begun after that snapshot is not in
    it (its spend journal holds it, which this read does not count). A store that ended (0) holds all it spent."""

    lease_id: int
    run_key: str
    spend_micro: int
    charged_attempts: int
    settled_running: int


def dead_store_spend(out: str | os.PathLike[str], study: StudyConfig) -> DeadStoreSpend | None:
    """What the dead lease store whose `out/` is `out` spent, for a controller that settles its lost lease in the
    ledger (`elastic.Ledger.settle(lease_id, spend_micro, source, attempts_charged=...)`); None when it cannot be read.

    The store is read as a merge by run reads a lost lease's store: copied into a private temporary directory and
    snapshotted, a store with running attempts first settled there by `recover` (`_settled_copy`), and its spend
    counted by the budget gate's own figure (`selection_ledger.spend_of`) with the study's prices. Nothing is written
    beside it. Its lock is held as a reader when it has one (a store pulled without its lock file is read all the
    same, since no dispatcher of it is alive); a store a dispatcher still holds is no dead store, and gives None. So
    do: no store, not a lease store, a store that changes while it is copied, one whose spend is unknown (a model with
    no price), or one that cannot be read. A merge by run later refuses a settled value its store does not give."""
    out = Path(out)
    with tempfile.TemporaryDirectory(prefix="expcore-dead-store-") as scratch:
        snapshot = Path(scratch) / "read.sqlite"
        try:
            interrupted = _read_dead(out, Path(scratch) / "settle", snapshot, study)
            store = lease_store_of_bytes(
                snapshot.read_bytes(), out, out=out, snapshot=snapshot, pricing=pricing_of(study)
            )
        except (CampaignLocked, LockUnavailable, SnapshotError, StoreChanged, MergeRefused, OSError, sqlite3.Error):
            return None
    spent = None if store.spend is None else store.spend.total
    if spent is None:
        return None
    return DeadStoreSpend(store.lease.lease_id, store.lease.run_key, micro_up(spent), store.charged, interrupted)


def _read_dead(out: Path, private: Path, snapshot: Path, study: StudyConfig) -> int:
    """Snapshot a dead store to `snapshot`, settling a running attempt in a private copy; return how many."""

    def read() -> int:
        try:
            take_snapshot(out / STATE_FILE, snapshot)
        except AttemptsRunning:
            return _settled_copy(out, private, snapshot, study.schema, study.campaign.scheduler.max_attempts)[1]
        return 0

    try:
        with ReaderLock(out):
            return read()
    except LockMissing:
        return read()


def _pinned_lease_id(store: Store) -> int | None:
    """The lease id a store pins in its shard spec; None for a store of any other form."""
    try:
        pinned = json.loads(store.get_meta(META_SHARD) or "null")
    except ValueError:
        return None
    value = pinned.get("lease_id") if isinstance(pinned, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["DeadStoreSpend", "MergedByRun", "dead_store_spend", "merge_by_run"]
