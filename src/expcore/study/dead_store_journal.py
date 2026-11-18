# Provenance: new
"""A dead lease store's spend read with the lease's spend journal (#462 step 6): what `dead_store_spend` falls back to
for a store the store alone cannot answer for.

A lease store that came back from a lost federation is a snapshot: an attempt running in it, or its run pending
between attempts, can miss what was spent after the snapshot. The lease run's own spend journal (`run --spend-journal`,
`run_lease.LEASE_JOURNAL`, beside the study copy) holds every attempt begun, top-up and probe charge up to the kill,
each fsynced before the spend it allows. So the read is the one a resumed start makes, in a private copy of the store
and of the journal: `recover` makes a running attempt interrupted, and `lost_spend.reconcile` counts what the journal
holds that the store lacks (the store's own journal: its rows are matched by id, run, start and pid), each lost attempt
charged by its journaled call log or its bound, and each top-up and probe at its dollars; the spend is then the budget
gate's own figure (`selection_ledger.spend_of`) over the reconciled copy.

**Charged attempts.** The journal does not say whether an attempt that ended infrastructure-failed was charged (its
kind is not journaled). One the store holds ended is its row's; an attempt the journal saw end as `infra_fail` that the
store lacks (or holds running) is counted charged, the ledger's own provisional charge being as high; any other end,
and an attempt the journal never saw end (interrupted by the loss), is not charged.

**None** (the lease stays charged its grant) for a journal that is not there, has no whole header (an empty file would
read as "nothing spent"), does not list every attempt the store holds (a short or damaged journal), is not the one the
store's own start opened (its name, campaign and whole shard spec, so another lease's journal is never read), has a
line that is not whole before the last (a last line a kill cut short was never spent and is dropped), or names another
run; for a store
a dispatcher still holds, one that changes while copied, one the study's prices do not fit, an unknown spend, and a
figure above the lease's grant (a spend the cap should have held: the ledger would record the lease's cap as failed).
Nothing is written beside the store or the journal.
"""

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

from expcore.campaign import lost_ledger
from expcore.campaign.attempts import SchemaMismatch
from expcore.campaign.elastic.config import micro_up
from expcore.campaign.lease_facts import lease_store_of_bytes
from expcore.campaign.lock import CampaignLocked, LockMissing, LockUnavailable, ReaderLock
from expcore.campaign.lost_ledger import LostSpendError
from expcore.campaign.lost_spend import reconcile
from expcore.campaign.markers import MarkerError
from expcore.campaign.merge import MergeRefused
from expcore.campaign.recovery import recover
from expcore.campaign.run_lease import LEASE_JOURNAL
from expcore.campaign.scheduler import STATE_FILE
from expcore.campaign.shard import META_SHARD
from expcore.campaign.snapshot import SnapshotError, rebase_run_dirs, take_snapshot
from expcore.campaign.spend_journal import SpendJournal, SpendJournalError, read_journal
from expcore.campaign.store import Store
from expcore.campaign.storecopy import StoreChanged, copy_store
from expcore.study.config import StudyConfig
from expcore.study.selection_start import pricing_of

_UNREADABLE = (
    StoreChanged,
    SnapshotError,
    MergeRefused,
    LostSpendError,
    SpendJournalError,
    SchemaMismatch,
    MarkerError,
    CampaignLocked,
    LockUnavailable,
    OSError,
    sqlite3.Error,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


class JournalRead(NamedTuple):
    """A dead lease's figure from its store and journal: the lease and run, the spend in dollars, and its charged
    attempts (the lease's base not included)."""

    lease_id: int
    run_key: str
    spend_usd: float
    charged: int


def read_with_journal(out: Path, study: StudyConfig, scratch: Path) -> JournalRead | None:
    """The lease store at `out` read with the journal beside its study copy, in `scratch`; None as the module says."""
    try:
        raw = (out.parent.parent / LEASE_JOURNAL).read_bytes()
    except OSError:
        return None
    private = scratch / "journal-read"
    try:
        private.mkdir(mode=0o700)
        (private / "journals").mkdir(mode=0o700)
        return _read(out, study, raw, private)
    except _UNREADABLE:
        return None


def _locked_copy(out: Path, into: Path) -> Path:
    """The store's file and -wal copied into `into`, under the store's lock as a reader (a store pulled without its lock
    file is copied all the same)."""
    try:
        with ReaderLock(out):
            return copy_store(out / STATE_FILE, into)
    except LockMissing:
        return copy_store(out / STATE_FILE, into)


def _read(out: Path, study: StudyConfig, raw: bytes, private: Path) -> JournalRead | None:
    pricing = pricing_of(study)
    copied = _locked_copy(out, private)
    rebase_run_dirs(copied, private / "runs")  # marks the copy copied back: no log of its own is believed
    store = Store(copied, study.schema)
    try:
        shard = store.get_meta(META_SHARD)
        ledger = lost_ledger.read(store)
        if shard is None or ledger is None or ledger.journal != LEASE_JOURNAL or ledger.campaign is None:
            return None  # not a lease store, or not one whose own start opened the journal beside it
        run_key = json.loads(shard).get("run_key")
        named = private / "journals" / LEASE_JOURNAL
        named.write_bytes(raw)
        journal = read_journal(named, ledger.campaign, shard)  # another lease's or campaign's journal refuses here
        held = {r["id"] for r in store.q("SELECT id FROM attempts")}
        if raw.count(b"\n") < 1 or not held <= set(journal.attempts):
            return (
                None  # no whole header, or not every attempt the store holds: a damaged or short journal reads as none
            )
        if any(entry.get("run_key") != run_key for entry in journal.attempts.values()):
            return None
        ended = {r["id"] for r in store.q("SELECT id FROM attempts WHERE state != 'running'")}
        extra = sum(  # an attempt the store lacks or holds running: only the journal says how it ended
            1 for number, entry in journal.attempts.items() if number not in ended and entry["state"] == "infra_fail"
        )
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        recover(store, now, study.campaign.scheduler.max_attempts)
        reader = SpendJournal(private / "journals" / "reader.jsonl", ledger.campaign, shard, now)
        try:
            reconcile(store, reader, now, pricing)
        finally:
            reader.close()
    finally:
        store.close()
    return _figure(out, copied, private, pricing, extra)


def _figure(out: Path, copied: Path, private: Path, pricing: Any, extra: int) -> JournalRead | None:
    """The reconciled copy's lease, run, spend and charged attempts; None when its figure cannot stand."""
    snapshot = private / "read.sqlite"
    take_snapshot(copied, snapshot)
    store = lease_store_of_bytes(snapshot.read_bytes(), out, out=out, snapshot=snapshot, pricing=pricing)
    if pricing.mismatch(store.fingerprint) is not None or store.spend is None or store.spend.total is None:
        return None
    grant = store.lease.grant_micro_usd
    if grant and micro_up(store.spend.total) > grant:
        return None
    return JournalRead(store.lease.lease_id, store.lease.run_key, store.spend.total, store.charged + extra)
