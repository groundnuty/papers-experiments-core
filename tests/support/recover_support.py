"""A campaign's sealed final ledger holding a lost run, and the working copy an operator recovers it on.

The sealed final is the closed ledger of a campaign whose run ended `infra_exhausted` (every lease of it ended
`infra_fail` charging one attempt) or `harness_error` (a sandbox that failed at its start): it is made with the ledger's
own calls, so that the merge's replay accepts it. The working copy is a byte copy of that closed file in a directory of
its own, which is where a dispatcher's lock and a controller's declaration are taken. Names are invented.
"""

import dataclasses
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from elastic_support import FakeClock, finish, join, leased, ledger_path, lose, make, overdue, plan
from reopen_support import dump

from expcore.campaign.elastic import Ledger, attempts
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import RecoverRefused
from expcore.campaign.elastic.recoverrun import RECOVER, recover_in_file

FED = "site-a-fed-1"
WHY = "the sandbox timed out at its start, which no run of the study causes"
OPTIONAL_TABLES = ("cap_changes", "declarations", "lease_sets", "undeclared_seen", "federation_platforms")


@dataclass
class Lost:
    """A sealed final `source` holding the run `run_key` as `state` with `charged` attempts, its leases `leases`, and
    the working copy `path` of it (closed, as when no controller holds it)."""

    root: Path
    path: Path
    source: Path
    run_key: str
    state: str
    charged: int
    leases: tuple[int, ...]
    clock: FakeClock


def _seal(ledger: Ledger, root: Path) -> tuple[Path, Path]:
    """Close the campaign's ledger as its final and copy that file to the working copy; return both paths."""
    final = ledger.path
    ledger.close()
    assert not final.with_name(final.name + "-wal").exists(), "a closed ledger leaves no write-ahead log"
    copy = ledger_path(root / "work")
    copy.parent.mkdir(parents=True)
    shutil.copy2(final, copy)
    return final, copy


def lost_run(tmp_path: Path, *, state: str = "infra_exhausted", charges: tuple[int, ...] = (1, 1, 1), **config) -> Lost:
    """A campaign whose first run took one lease for each of `charges`, each ending counted, charging that many
    attempts: `infra_fail` for all of them, but for the last of a run that ends `harness_error`."""
    ledger, clock = make(tmp_path / "campaign", runs=plan(tasks=2, conditions=("api-none",)), **config)
    join(ledger, clock, FED)
    ids, run_key = [], None
    for number, charged in enumerate(charges, start=1):
        lease = leased(ledger, FED)
        run_key = run_key or lease.run_key
        assert lease.run_key == run_key, "the run is leased again as soon as it is pending"
        last = number == len(charges)
        ended = "harness_error" if state == "harness_error" and last else "infra_fail"
        assert finish(ledger, lease, state=ended, charged=charged) == "counted"
        ids.append(lease.lease_id)
        clock.advance(60)
    run = ledger.run(run_key)
    assert (run.state, run.charged) == (state, sum(charges))
    final, copy = _seal(ledger, tmp_path)
    return Lost(tmp_path, copy, final, run_key, state, sum(charges), tuple(ids), clock)


def lost_by_losses(tmp_path: Path) -> Lost:
    """A campaign with a budget whose only run lost three federations in turn, so it is `infra_exhausted` on three
    charged attempts, each the conservative one of a loss that nobody settled."""
    ledger, clock = make(
        tmp_path / "campaign", runs=plan(tasks=1, conditions=("api-none",)), max_usd=10.0, grants_usd={"m1": 1.0}
    )
    ids = []
    for number in (1, 2, 3):
        ids.append(lose(ledger, clock, f"fed-{number}").lease_id)
        overdue(ledger, clock)
    run_key = ledger.runs()[0].run_key
    assert (ledger.run(run_key).state, ledger.run(run_key).charged) == ("infra_exhausted", 3)
    final, copy = _seal(ledger, tmp_path)
    return Lost(tmp_path, copy, final, run_key, "infra_exhausted", 3, tuple(ids), clock)


def lost_and_settled(tmp_path: Path, *, state: str = "infra_exhausted") -> Lost:
    """Like `lost_by_losses`, with every loss settled from its dead store's log (one charged attempt, a spend), so that
    the run's old leases hold `charged_added` above 0 and the ledger holds a settlement row for each of them: a
    `harness_error` run is one loss, settled, and a lease that ended `harness_error` charging nothing; an
    `infra_exhausted` one is three losses."""
    ledger, clock = make(
        tmp_path / "campaign", runs=plan(tasks=1, conditions=("api-none",)), max_usd=10.0, grants_usd={"m1": 1.0}
    )
    ids = []
    for number in range(1, 4 if state == "infra_exhausted" else 2):
        lease = lose(ledger, clock, f"fed-{number}")
        ledger.settle(lease.lease_id, 100_000, "root", attempts_charged=1)
        ids.append(lease.lease_id)
    if state == "harness_error":
        join(ledger, clock, "fed-last")
        last = leased(ledger, "fed-last")
        assert finish(ledger, last, state="harness_error", charged=0) == "counted"
        ids.append(last.lease_id)
    run_key = ledger.runs()[0].run_key
    charged = 3 if state == "infra_exhausted" else 1
    assert (ledger.run(run_key).state, ledger.run(run_key).charged) == (state, charged)
    final, copy = _seal(ledger, tmp_path)
    return Lost(tmp_path, copy, final, run_key, state, charged, tuple(ids), clock)


def add_overdue(path: Path, lease_id: int, run_key: str) -> None:
    """Log by hand that the settle of a lease is overdue, as a controller's sweep does for a lost lease of a pending
    run (a run that is exhausted is never swept for it)."""
    db = sqlite3.connect(path)
    try:
        db.execute(
            "INSERT INTO events(at, kind, run_key, lease_id) VALUES(?,?,?,?)",
            (1.0, attempts.OVERDUE, run_key, lease_id),
        )
        db.commit()
    finally:
        db.close()


def settle_spend_only(found: Lost, lease_id: int) -> None:
    """Settle a lost lease of the working copy with its spend alone: the settlement holds no charged attempts."""
    ledger = open_ledger(found)
    try:
        ledger.settle(lease_id, 100_000, "root")
    finally:
        ledger.close()


def charged_added_of(path: Path) -> dict[int, int]:
    """What each lease of the ledger file was counted for (`charged_added`), by lease id."""
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT id, charged_added FROM leases ORDER BY id").fetchall())
    finally:
        db.close()


def assert_only_the_run_and_one_event_changed(found: Lost, before: dict, after: dict) -> None:
    """What a recovery writes and nothing more: every table but `runs`, `events` and `sqlite_sequence` is as it was
    (every lease row, `charged_added` included, and every settlement among them); one row of `runs` differs, the run's,
    and not in its key, factors, set or latest lease; and the log holds each earlier row as it was, and one more."""
    assert {table for table in after if after[table] != before[table]} == {"runs", "events", "sqlite_sequence"}
    assert after["leases"] == before["leases"]
    assert after["settlements"] == before["settlements"]
    assert after["events"][:-1] == before["events"]
    assert len(after["events"]) == len(before["events"]) + 1
    changed = [(old, new) for old, new in zip(before["runs"], after["runs"], strict=True) if old != new]
    assert len(changed) == 1
    old, new = changed[0]
    assert old[0] == new[0] == found.run_key
    assert old[:5] == new[:5]  # the run's key, factors and set are as they were
    assert old[7] == new[7]  # its latest lease is as it was


def open_ledger(found: Lost) -> Ledger:
    """The working copy opened by a dispatcher, on the lost run's clock."""
    return Ledger.open(found.path, clock=found.clock)


def recover_found(found: Lost, **changes):
    """The command on the lost run (`run_key`, `charged`, `source` and `dry_run` may be changed), on the found clock."""
    arguments = {"run_key": found.run_key, "charged": 0, "source": found.source, "dry_run": False}
    arguments.update(changes)
    return recover_in_file(
        found.path,
        arguments["run_key"],
        arguments["charged"],
        arguments["source"],
        WHY,
        "op-1",
        dry_run=arguments["dry_run"],
        clock=found.clock,
    )


def assert_refused(found: Lost, number: int, phrase: str, **changes) -> str:
    """The command is refused naming condition `number`, and every table of the working copy is as it was, and so is
    the sealed final, when it is a file."""
    before, source_before = dump(found.path), _bytes_of(changes.get("source", found.source))
    with pytest.raises(RecoverRefused) as refusal:
        recover_found(found, **changes)
    message = str(refusal.value)
    assert message.startswith(f"condition {number} ("), message
    assert phrase in message, message
    assert dump(found.path) == before
    assert _bytes_of(changes.get("source", found.source)) == source_before
    return message


def _bytes_of(path: Path) -> bytes | None:
    path = Path(path)
    return path.read_bytes() if path.is_file() else None


def events_of(path: Path) -> list[sqlite3.Row]:
    """The `operator_recover` events of the ledger file, in order."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM events WHERE kind=? ORDER BY seq", (RECOVER,)).fetchall()
    finally:
        db.close()


def event_detail(path: Path) -> dict:
    """The detail of the one `operator_recover` event of the ledger file."""
    (event,) = events_of(path)
    return json.loads(event["detail"])


def another_fingerprint(path: Path) -> None:
    """Give the ledger file at `path` the pinned configuration of another campaign."""
    db = sqlite3.connect(path)
    try:
        pinned = LedgerConfig.from_json(db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        other = dataclasses.replace(pinned, fingerprint=hashlib.sha256(b"another campaign").hexdigest())
        db.execute("UPDATE meta SET value=? WHERE key='config'", (other.to_json(),))
        db.commit()
    finally:
        db.close()


def drop_optional_tables(path: Path) -> None:
    """Make the ledger file an older one: without the tables that are created when first needed."""
    db = sqlite3.connect(path)
    try:
        for table in OPTIONAL_TABLES:
            db.execute(f"DROP TABLE {table}")
        db.commit()
    finally:
        db.close()
