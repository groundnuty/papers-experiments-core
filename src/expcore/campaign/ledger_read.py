# Provenance: new
"""The elastic ledger as a merge by run reads it (#462 step 4, C43): a snapshot, its facts, and its rule recomputed.

A merge by run never opens the ledger as the dispatcher does (`elastic.Ledger.open` takes the dispatcher's lock and
writes beside the file). It copies the file it is given, with its -wal, into a private directory
(`storecopy.store_copy`), makes one self-contained snapshot of it with SQLite's online backup in the rollback-journal
mode (`take_ledger_snapshot`, as `snapshot.take_snapshot` copies a store), and reads every fact from the bytes of that
snapshot (`read_ledger`), which the merged batch keeps and binds by its SHA-256.

**The tables are not trusted on their own word.** `read_ledger` replays the counting rule from the ledger's events, in
their global sequence, and refuses a ledger whose tables say anything else:

- `issue` makes a lease its run's latest, and lease ids rise with the sequence (the fencing token);
- `report` counts exactly when its lease is its run's latest and the run is not done; any other is superseded;
- `counted` applies today's status rule across leases (`done`, `harness_error`, `infra_exhausted` once the charged
  attempts of the run's counted leases reach `max_attempts`, else pending), with the charged attempts of the lease's
  completion;
- `lost` of a counting lease, `expire`, a `return` of a live lease and `cancel` put the run back to pending;
- a lease is settled (counted, superseded or lost) once: a second settlement of it is refused.

Each run's state, latest lease and charged attempts, and each lease's final state (counted, superseded, lost) must be
what the events give. A run's **counted lease** is then its latest lease when the run is terminal (done,
infra_exhausted or harness_error); that lease's state is `counted`. A lease counted earlier, whose run went back to
pending and was leased again, is not the run's counted lease.
"""

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.schema import SCHEMA
from expcore.campaign.storecopy import StoreChanged, store_copy

LEDGER_SNAPSHOT = "ledger-snapshot.sqlite"
TERMINAL = ("done", "infra_exhausted", "harness_error")
_LIVE = ("issued", "sent")
_SETTLED = ("counted", "superseded", "lost")


class LedgerRefused(RuntimeError):
    """The ledger cannot be read, or its tables are not what its events give; the message names what."""


@dataclass(frozen=True)
class LeaseRow:
    """One lease as the ledger holds it."""

    lease_id: int
    run_key: str
    federation: str
    site: str
    model: str
    state: str
    verdict: str | None
    grant_micro: int
    charge_micro: int
    completion: Mapping[str, Any] | None


@dataclass(frozen=True)
class RunRow:
    """One run as the ledger holds it: its state, its latest lease and the charged attempts of its counted leases."""

    run_key: str
    seq: int
    model: str
    state: str
    lease_id: int | None
    charged: int


@dataclass(frozen=True)
class LedgerFacts:
    """What a merge by run reads of the ledger, from the snapshot whose SHA-256 is `sha256`."""

    sha256: str
    config: LedgerConfig
    environment: str | None
    runs: Mapping[str, RunRow]
    leases: Mapping[int, LeaseRow]

    def counted(self, run_key: str) -> int | None:
        """The run's counted lease: its latest when the run is terminal; None for a run that is not."""
        run = self.runs[run_key]
        return run.lease_id if run.state in TERMINAL else None


def take_ledger_snapshot(source: Path, destination: Path) -> None:
    """Copy the ledger at `source` (with its -wal) privately and write one self-contained snapshot of it at
    `destination`, checked for integrity and flushed; refuse a file that cannot be copied or changes meanwhile."""
    if not source.is_file():
        raise LedgerRefused(f"{source}: no ledger here")
    scratch = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    scratch.unlink(missing_ok=True)
    try:
        with store_copy(source) as copied:
            origin = sqlite3.connect(copied)
            copy = sqlite3.connect(scratch)
            try:
                origin.backup(copy)
                copy.execute("PRAGMA journal_mode = DELETE")
                verdict = copy.execute("PRAGMA integrity_check").fetchall()
            finally:
                copy.close()
                origin.close()
        if [tuple(row) for row in verdict] != [("ok",)]:
            raise LedgerRefused(f"{source}: the copy of the ledger failed its integrity check")
        with open(scratch, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(scratch, destination)
    except (sqlite3.Error, OSError, StoreChanged) as error:
        raise LedgerRefused(f"{source}: cannot copy the ledger ({type(error).__name__}: {error})") from None
    finally:
        scratch.unlink(missing_ok=True)


def read_ledger(data: bytes, sha256: str, where: object = LEDGER_SNAPSHOT) -> LedgerFacts:
    """The facts of the ledger whose snapshot's bytes are `data`, after its rule is recomputed from its events."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        try:
            db.deserialize(data)
            meta = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}
            if meta.get("schema") != SCHEMA:
                raise LedgerRefused(f"{where}: not a ledger of {SCHEMA}")
            config = LedgerConfig.from_json(meta["config"])
            runs = {row["run_key"]: _run_row(row) for row in db.execute("SELECT * FROM runs ORDER BY seq")}
            leases = {row["id"]: _lease_row(row) for row in db.execute("SELECT * FROM leases ORDER BY id")}
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY seq")]
        except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
            raise LedgerRefused(f"{where}: cannot read the ledger ({type(error).__name__}: {error})") from None
    finally:
        db.close()
    facts = LedgerFacts(sha256, config, meta.get("environment"), runs, leases)
    _replay(facts, events, where)
    return facts


def _run_row(row: Any) -> RunRow:
    return RunRow(row["run_key"], row["seq"], row["model"], row["state"], row["lease_id"], row["charged"])


def _lease_row(row: Any) -> LeaseRow:
    completion = None if row["completion"] is None else json.loads(row["completion"])
    return LeaseRow(
        row["id"],
        row["run_key"],
        row["federation"],
        row["site"],
        row["model"],
        row["state"],
        row["verdict"],
        row["grant_micro"],
        row["charge_micro"],
        completion,
    )


class _Replay:
    """The counting rule, applied to the events in their sequence."""

    def __init__(self, facts: LedgerFacts, where: object) -> None:
        self.facts, self.where = facts, where
        self.state = dict.fromkeys(facts.runs, "pending")
        self.latest: dict[str, int] = {}
        self.charged = dict.fromkeys(facts.runs, 0)
        self.verdicts: dict[int, str] = {}
        self.settled: dict[int, str] = {}
        self.last_issued = 0

    def refuse(self, event: Mapping[str, Any], why: str) -> LedgerRefused:
        return LedgerRefused(
            f"{self.where}: event {event['seq']} ({event['kind']} of lease {event['lease_id']}) {why}: the ledger's "
            "tables are not what its events give"
        )

    def apply(self, event: Mapping[str, Any]) -> None:
        kind, lease_id, run = event["kind"], event["lease_id"], event["run_key"]
        if run is not None and run not in self.state:
            raise self.refuse(event, f"names run {run}, which the ledger does not hold")
        try:
            detail = json.loads(event["detail"]) if event["detail"] else {}
        except ValueError:
            raise self.refuse(event, "holds a detail that is not JSON") from None
        if kind in ("counted", "superseded", "lost") and lease_id in self.settled:
            raise self.refuse(event, f"settles a lease already settled as {self.settled[lease_id]}")
        if kind == "issue":
            if lease_id <= self.last_issued:
                raise self.refuse(event, "issues a lease id not above every earlier one")
            self.last_issued = self.latest[run] = lease_id
            self.state[run] = "leased"
        elif kind == "report":
            self._report(event, lease_id, run, detail)
        elif kind in ("counted", "superseded"):
            self._settle(event, kind, lease_id, run)
        elif kind == "lost":
            self.settled[lease_id] = "lost"
            if self.verdicts.get(lease_id) == "counts":
                self.state[run] = "pending"
        elif kind in ("expire", "cancel") or (kind == "return" and detail.get("was") in _LIVE):
            self.state[run] = "pending"

    def _report(self, event: Mapping[str, Any], lease_id: int, run: str, detail: Mapping[str, Any]) -> None:
        counts = self.latest.get(run) == lease_id and self.state[run] != "done"
        expected = "counts" if counts else "superseded"
        if detail.get("verdict") != expected:
            raise self.refuse(event, f"records the verdict {detail.get('verdict')!r} where the rule gives {expected!r}")
        self.verdicts[lease_id] = expected
        if counts:
            self.state[run] = "leased"

    def _settle(self, event: Mapping[str, Any], kind: str, lease_id: int, run: str) -> None:
        wanted = "counts" if kind == "counted" else "superseded"
        if self.verdicts.get(lease_id) != wanted:
            raise self.refuse(event, f"settles a lease whose completion was not read as {wanted!r}")
        self.settled[lease_id] = kind
        if kind == "counted":
            completion = self.facts.leases[lease_id].completion or {}
            self.charged[run] += int(completion.get("charged", 0))
            self.state[run] = self._status(str(completion.get("state")), self.charged[run])

    def _status(self, state: str, charged: int) -> str:
        """Today's status rule across leases (`elastic.Ledger._status_after`)."""
        if state in ("done", "harness_error"):
            return state
        if state == "infra_fail" and charged >= self.facts.config.max_attempts:
            return "infra_exhausted"
        return "pending"


def _replay(facts: LedgerFacts, events: list[Mapping[str, Any]], where: object) -> None:
    """Refuse a ledger whose runs and settled leases are not what its events give."""
    replay = _Replay(facts, where)
    for event in events:
        if event["lease_id"] is not None and event["lease_id"] not in facts.leases:
            raise replay.refuse(event, "names a lease the ledger does not hold")
        replay.apply(event)
    for key, run in facts.runs.items():
        given = (replay.state[key], replay.latest.get(key), replay.charged[key])
        if (run.state, run.lease_id, run.charged) != given:
            raise LedgerRefused(
                f"{where}: run {key} is {run.state} on lease {run.lease_id} with {run.charged} charged attempts, but "
                f"its events give {given[0]} on lease {given[1]} with {given[2]}: the ledger's tables are not what "
                "its events give"
            )
    for lease in facts.leases.values():
        if (lease.state if lease.state in _SETTLED else None) != replay.settled.get(lease.lease_id):
            raise LedgerRefused(
                f"{where}: lease {lease.lease_id} is {lease.state}, but its events settle it as "
                f"{replay.settled.get(lease.lease_id) or 'nothing'}: the ledger's tables are not what its events give"
            )
