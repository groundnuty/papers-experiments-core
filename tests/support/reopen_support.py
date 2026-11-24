"""A ledger in which a plugin defect ended a run as harness_error, and the synthetic archive of the lease that ended it.

The shape is the one an operator reopens: lease 1 of the run ended infra_fail with one charged attempt; lease 2 ran
the run again (its `attempt_base` is 1), and its store holds two attempts of one number, the first refused by the model
API (`key_rejected`, uncharged) and the second, run again under the same number, a harness error whose detail starts
with the plugin's `LedgerError`. The archive is built with the worker's own `_archive`, so it is the tar a worker writes
(`out/` as `out`, a plain tar), whose SHA-256 the completion names. Names are invented.
"""

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from elastic_support import SITE, FakeClock, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import Completion, Ledger, SiteReading
from expcore.campaign.elastic import worker as elastic_worker
from expcore.campaign.elastic.reopenrun import reopen_in_file

FED = "site-a-fed-1"
OTHER_RUN = "task=task-9|model=m1|condition=api-none"
LEDGER_ERROR = "LedgerError 0123456789ab: the instrument could not record its start"
REFUSED_DETAIL = "HTTP 403 PermissionDeniedError"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_STORE_SQL = """CREATE TABLE attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_key TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL,
  charged INTEGER NOT NULL DEFAULT 0, started_utc TEXT, ended_utc TEXT, wall_s REAL, pid INTEGER,
  infra_kind TEXT, detail TEXT, termination TEXT, input_tokens INTEGER, output_tokens INTEGER, run_dir TEXT)"""


@dataclass(frozen=True)
class Row:
    """One attempt row of a lease store. `run_key` None: the run the lease is for."""

    attempt: int
    state: str
    charged: int = 0
    infra_kind: str | None = None
    detail: str | None = None
    run_key: str | None = None


REFUSED = Row(1, "infra_fail", 0, "key_rejected", REFUSED_DETAIL)
DEFECT = Row(1, "harness_error", 0, None, LEDGER_ERROR)
INCIDENT = (REFUSED, DEFECT)


def _insert(db: sqlite3.Connection, run_key: str, rows) -> None:
    for row in rows:
        db.execute(
            "INSERT INTO attempts(run_key, attempt, state, charged, infra_kind, detail, termination)"
            " VALUES(?,?,?,?,?,?,?)",
            (row.run_key or run_key, row.attempt, row.state, row.charged, row.infra_kind, row.detail, row.state),
        )


def write_store(out: Path, run_key: str, rows, wal_rows=()) -> None:
    """Write `out/` as a lease store leaves it: a store in WAL mode, its rows in it, and a record under `runs/`.
    `wal_rows` are rows that live only in the `-wal` of the files copied (a store archived while a connection was
    open, with its newest rows not checkpointed), so a copy of the main file alone would not show them."""
    scratch = out.with_name(out.name + "-live")
    scratch.mkdir(parents=True)
    db = sqlite3.connect(scratch / "state.sqlite", isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute(_STORE_SQL)
        _insert(db, run_key, rows)
        if wal_rows:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _insert(db, run_key, wal_rows)
            out.mkdir(parents=True)
            for name in ("state.sqlite", "state.sqlite-wal"):
                shutil.copy2(scratch / name, out / name)
            assert (out / "state.sqlite-wal").stat().st_size > 0, "the rows meant for the -wal reached the main file"
    finally:
        db.close()
    if not wal_rows:
        shutil.copytree(scratch, out)
    (out / "runs" / "try1").mkdir(parents=True)
    (out / "runs" / "try1" / "record.json").write_text('{"invented": true}\n', encoding="utf-8")


def make_archive(root: Path, run_key: str, rows=INCIDENT, *, wal_rows=(), name: str = "lease-000000000002.out.tar"):
    """The lease archive of a store holding `rows`, written as the worker writes one; its path and SHA-256."""
    out = root / f"work-{name}" / "out"
    write_store(out, run_key, rows, wal_rows)
    archive = root / name
    return archive, elastic_worker._archive(out, archive)


def fake_zstd_archive(tar: Path, name: str | None = None) -> Path:
    """A file that starts as a zstd stream does (its magic) and then holds the tar: the stand-in `zstd` reads it."""
    target = tar.with_name(name or tar.name + ".zst")
    target.write_bytes(ZSTD_MAGIC + tar.read_bytes())
    return target


def install_stand_in_zstd(bin_dir: Path, monkeypatch, *, exit_status: int = 0) -> None:
    """Put a `zstd` first on PATH that drops the magic and passes the rest through (or exits `exit_status` with a
    message), so the reading of a compressed archive is tested whether or not the machine has the real program."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "zstd"
    if exit_status:
        script.write_text(f"#!/bin/sh\necho 'stand-in zstd: corrupt input' >&2\nexit {exit_status}\n", encoding="utf-8")
    else:
        script.write_text("#!/bin/sh\nexec tail -c +5\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def real_zstd_archive(tar: Path) -> Path:
    """The tar compressed by the real `zstd` program, beside it as `<tar>.zst`."""
    target = tar.with_name(tar.name + ".zst")
    subprocess.run([shutil.which("zstd"), "-q", "-f", "-o", str(target), str(tar)], check=True)
    return target


@dataclass
class Incident:
    """A closed ledger whose run `run_key` ended harness_error on lease `lease_id`, and that lease's archive."""

    root: Path
    path: Path
    run_key: str
    lease_id: int
    archive: Path
    sha256: str
    clock: FakeClock


def incident(tmp_path: Path, rows=INCIDENT, *, wal_rows=(), state: str = "harness_error", tasks: int = 2) -> Incident:
    """Build the ledger and the archive: lease 1 of the first run ends infra_fail charging one attempt, lease 2 takes
    the run on (attempt base 1) and ends as `state` with a completion that names the archive of `rows`. The ledger is
    closed, as it is when no controller holds it."""
    ledger, clock = make(tmp_path, runs=plan(tasks=tasks, conditions=("api-none",)))
    join(ledger, clock, FED)
    first = leased(ledger, FED)
    assert finish(ledger, first, state="infra_fail", charged=1) == "counted"
    second = leased(ledger, FED)
    assert (second.run_key, second.attempt_base) == (first.run_key, 1)
    archive, sha = make_archive(tmp_path, second.run_key, rows, wal_rows=wal_rows)
    reading = Completion(lease_id=second.lease_id, state=state, charged=0, spend_usd=0.1, archive_sha256=sha)
    ledger.observe_site(SITE, SiteReading(completions=(reading,)))
    assert ledger.verify_archive(second.lease_id, sha) == "counted"
    ledger.close()
    return Incident(tmp_path, ledger_path(tmp_path), second.run_key, second.lease_id, archive, sha, clock)


def reopened(tmp_path: Path) -> Incident:
    """An incident whose run is already reopened by the operator's command."""
    found = incident(tmp_path)
    reopen_in_file(found.path, found.lease_id, found.archive, "an invented reason", "op-1", clock=found.clock)
    return found


def open_ledger(found: Incident) -> Ledger:
    """The incident's ledger opened by a dispatcher, on the incident's clock."""
    return Ledger.open(found.path, clock=found.clock)


def dump(path: Path) -> dict[str, list]:
    """Every table of the ledger file, `sqlite_sequence` included: what a refused command must leave as it was."""
    db = sqlite3.connect(path)
    try:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in names}
    finally:
        db.close()


def sql(path: Path, statement: str, *args) -> None:
    """One change to the ledger file made by hand, for a state the ledger's own calls cannot reach."""
    db = sqlite3.connect(path)
    try:
        db.execute(statement, args)
        db.commit()
    finally:
        db.close()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
