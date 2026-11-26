# Provenance: new
"""An operator's recovery of one lost run, on a working copy of a campaign's sealed final ledger.

A run the campaign ended as `infra_exhausted` (its charged attempts reached `max_attempts`) or as `harness_error` (a
sandbox that failed at its start) is terminal: the ledger never leases it again. When the study's owner approves running
such a run once the campaign has closed, the recovery is made on a working copy of the sealed final ledger, never on the
final itself, and the campaign's own controller then leases the recovered run to a fresh federation. `recover-run` puts
one such run back to pending with a charged count the operator chooses, so that the next lease's `attempt_base` is that
count, and nothing else.

It is refused, writing nothing, unless all of these hold, read in one transaction on the ledger's own write lock (as
`pause`, `drain` and `reopen-run` are written, beside a running controller):

1. the run exists and its state is `infra_exhausted` or `harness_error`;
2. no lease of the run is in flight (`issued`, `sent`), `reported`, or `expired` with its holder not proven ended;
3. the charged count N is a whole number with 0 <= N < `max_attempts`, and not above the run's charged count now: the
   verb keeps or lowers the count, never raises it;
4. the sealed final is a regular file, never a link, and not the working copy itself (another device and inode); it has
   no `-wal` with bytes in it beside it (it is sealed: the file alone is the ledger); it is a ledger of the same schema
   with the same campaign fingerprint as the working copy; and it holds the run with the same state and the same
   charged count as the working copy does now, so the copy has not diverged for this run. Its SHA-256 is computed and
   recorded. It is opened read-only and immutable (a URI with `mode=ro&immutable=1`), never written, and no `-wal` or
   `-shm` is made beside it, so it also reads from a directory that cannot be written;

and, as `pause` and `drain` are, while no live controller fails to say it honours operator writes (`awareness`).

The sealed final is read before the write lock is taken (it may be large, and a controller shares the lock): conditions
1 to 3 are decided once so that a refusal costs no reading, the sealed final is read, and then the lock is taken and
conditions 1 to 4 and the controller's declaration are decided again on what the files say then, before the change. A
dry run decides them all again at its end, in a read transaction, so it never says yes to what a real call would refuse.

The change is the run's state set to `pending` and its charged count set to N: its latest lease and every lease row
stay as they were, so the next lease is issued as any other, with `attempt_base` N, and the run is not ended again while
N is below `max_attempts`. One event of kind `operator_recover` records the run key, its prior state and charged count,
N, the rule, the run's lease ids (sorted), the sealed final's SHA-256, the reason and the actor. The rule is `declared`
when N is the run's own count (the operator declares the run recovered as it stands) and `waiver` when N is below it
(the operator waives the difference). A waiver leaves the run's count below what its leases were counted for
(`charged_added`), which is the operator's, recorded, and which a later settle of one of those leases moves by that
lease's own difference (`attempts.set_charged`). The log is the ledger's own: no table or column is added, and a core
that does not know the kind reads the ledger as it did. The recovery is once: the run is `pending` afterwards, so a
second call is refused by condition 1. With `dry_run` every condition is decided and nothing is written.
"""

import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from expcore.campaign.elastic import awareness, schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import RecoverRefused

RECOVER = "operator_recover"  # the event kind
RECOVERABLE = ("infra_exhausted", "harness_error")
DECLARED, WAIVER = "declared", "waiver"  # the rule: N is the run's own count, or below it
# reopen-run's query for a lease in flight or expired with its holder not proven ended, and a lease reported too: its
# completion is read and its archive not yet verified, so its count may still be set
_IN_THE_WAY = (
    "SELECT l.id, l.state FROM leases l JOIN federations f ON f.label = l.federation WHERE l.run_key=?"
    " AND (l.state IN ('issued', 'sent', 'reported') OR (l.state = 'expired' AND f.ended_at IS NULL)) ORDER BY l.id"
)


@dataclass(frozen=True)
class Recovered:
    """What a recovery did, or with a dry run what it would do: the run, its prior state and charged count, its new
    charged count and the rule, its lease ids, the sealed final's SHA-256, the event's number (None for a dry run) and
    the controller's state (`awareness`)."""

    run_key: str
    prior_state: str
    prior_charged: int
    charged: int
    rule: str
    leases: tuple[int, ...]
    source_sha256: str
    event_seq: int | None
    controller: str


@dataclass(frozen=True)
class Source:
    """What the sealed final says: its SHA-256, its campaign fingerprint and the run's state and charged count."""

    sha256: str
    fingerprint: str
    state: str
    charged: int


@dataclass(frozen=True)
class _Facts:
    """What the ledger says of the run, once conditions 1 to 3 hold."""

    run_key: str
    prior_state: str
    prior_charged: int
    leases: tuple[int, ...]


def refused(number: int, name: str, why: str) -> RecoverRefused:
    """The refusal of condition `number` (named `name`), for the caller to raise."""
    return RecoverRefused(f"condition {number} ({name}): {why}")


def _facts(db: sqlite3.Connection, config: LedgerConfig, run_key: str, charged: int) -> _Facts:
    """Decide conditions 1, 2 and 3 on the ledger as it is; raise `RecoverRefused` for the first that fails."""
    run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (run_key,)).fetchone()
    if run is None:
        raise refused(1, "the run", f"the ledger has no run {run_key}")
    if run["state"] not in RECOVERABLE:
        raise refused(1, "the run", f"run {run_key} is {run['state']}, not {' or '.join(RECOVERABLE)}")
    out = db.execute(_IN_THE_WAY, (run_key,)).fetchall()
    if out:
        shown = ", ".join(f"lease {row['id']} ({row['state']})" for row in out)
        raise refused(
            2, "no lease in flight", f"{shown}: in flight, reported, or expired with its holder not proven ended"
        )
    if not 0 <= charged < config.max_attempts:
        raise refused(
            3,
            "charged count",
            f"{charged} is not a count from 0 to {config.max_attempts - 1}, below the ledger's max_attempts "
            f"({config.max_attempts})",
        )
    if charged > run["charged"]:
        raise refused(
            3,
            "charged count",
            f"run {run_key} has {run['charged']} charged attempts and {charged} is more: the count is kept or lowered, "
            "never raised",
        )
    leases = tuple(row[0] for row in db.execute("SELECT id FROM leases WHERE run_key=? ORDER BY id", (run_key,)))
    return _Facts(run_key, run["state"], run["charged"], leases)


def _verified(db: sqlite3.Connection, config: LedgerConfig, run_key: str, charged: int, source: Source) -> _Facts:
    """Decide every ledger condition and that the sealed final read is the working copy's origin for this run."""
    facts = _facts(db, config, run_key, charged)
    if source.fingerprint != config.fingerprint:
        raise refused(
            4,
            "sealed final",
            f"its campaign fingerprint {source.fingerprint} is not the ledger's {config.fingerprint}: it is the "
            "final of another campaign",
        )
    if (source.state, source.charged) != (facts.prior_state, facts.prior_charged):
        raise refused(
            4,
            "sealed final",
            f"it holds run {run_key} as {source.state} with {source.charged} charged attempts, and the ledger as "
            f"{facts.prior_state} with {facts.prior_charged}: the copy has diverged for this run",
        )
    return facts


def _controller(path: Path) -> str:
    """The state of the controller that holds the ledger (`awareness`); refused while it is one of an older core, which
    does not say it honours operator writes and would lease on without the recovery in mind."""
    controller = awareness.controller_of(path)
    if controller == awareness.OLDER:
        raise refused(
            5,
            "controller",
            "a controller holds this ledger and does not say it honours operator writes beside it: it runs a core "
            "older than the operator's pause. Restart it on this commit or later, then recover the run (a controller "
            "only now starting reads as older for a moment: run the command again)",
        )
    return controller


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _digest(handle: BinaryIO) -> str:
    return hashlib.file_digest(handle, "sha256").hexdigest()


def _open_regular(path: Path) -> BinaryIO:
    """The sealed final at `path`, opened once and never through a symbolic link, if it is a regular file."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise refused(4, "sealed final", f"{path} is a symbolic link: it is a regular file, never followed")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise refused(
            4, "sealed final", f"{path} cannot be opened ({type(error).__name__}: {error.strerror})"
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise refused(4, "sealed final", f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _read_ledger_file(path: Path, run_key: str) -> tuple[str, str, int]:
    """The campaign fingerprint and the run's state and charged count in the ledger file at `path`, read-only and
    immutable, so that nothing is written to it or made beside it."""
    try:
        db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as error:
        raise refused(4, "sealed final", f"{path} cannot be opened as a database ({error})") from None
    try:
        found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        pinned = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (run_key,)).fetchone()
    except sqlite3.Error:
        raise refused(4, "sealed final", f"{path} is not a ledger of {schema.SCHEMA}") from None
    finally:
        db.close()
    if found is None or found[0] != schema.SCHEMA or pinned is None:
        raise refused(4, "sealed final", f"{path} is not a ledger of {schema.SCHEMA}")
    try:
        fingerprint = LedgerConfig.from_json(pinned[0]).fingerprint
    except (ValueError, TypeError, KeyError):
        raise refused(4, "sealed final", f"{path} holds no ledger configuration that can be read") from None
    if run is None:
        raise refused(4, "sealed final", f"it holds no run {run_key}")
    return fingerprint, run[0], run[1]


def read_source(path: Path, ledger: Path, run_key: str) -> Source:
    """Read the sealed final at `path` for the run `run_key` of the working copy at `ledger`: refused
    (`RecoverRefused`, condition 4) unless it is a regular file, never a link, that is not the working copy itself, has
    no `-wal` with bytes in it beside it and is a ledger of the schema that holds the run. Nothing is written to it or
    beside it, and the file read for its SHA-256 is the file read for its rows."""
    with _open_regular(Path(path)) as handle:
        opened = os.fstat(handle.fileno())
        if _identity(opened) == _identity(os.stat(ledger)):
            raise refused(
                4, "sealed final", f"{path} is the ledger file itself: the sealed final is the file it was copied from"
            )
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise refused(
                4,
                "sealed final",
                f"{wal} holds bytes: the ledger is not sealed, and its file alone is not what the ledger holds",
            )
        sha256 = _digest(handle)
        fingerprint, state, charged = _read_ledger_file(Path(path), run_key)
        now = os.stat(path)
        if _identity(now) != _identity(opened) or (now.st_size, now.st_mtime_ns) != (
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise refused(4, "sealed final", f"{path} changed while it was read: it is not sealed")
    return Source(sha256, fingerprint, state, charged)


@contextmanager
def _reading(db: sqlite3.Connection) -> Iterator[None]:
    """A read transaction, never committed: no write lock is taken and nothing is written."""
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.execute("ROLLBACK")


def _write(
    db: sqlite3.Connection, now: float, facts: _Facts, charged: int, source: Source, reason: str, actor: str
) -> int:
    """Put the run of `facts` back to pending with `charged` attempts and log it; return the event's number."""
    changed = db.execute(
        "UPDATE runs SET state='pending', charged=? WHERE run_key=? AND state=? AND charged=?",
        (charged, facts.run_key, facts.prior_state, facts.prior_charged),
    )
    if changed.rowcount != 1:
        raise refused(
            1, "the run", f"run {facts.run_key} is not {facts.prior_state} with {facts.prior_charged} charged"
        )
    detail = {  # the run key is in the log's column too, which every reader of the log goes by
        "run_key": facts.run_key,
        "prior_state": facts.prior_state,
        "prior_charged": facts.prior_charged,
        "charged": charged,
        "rule": _rule(facts, charged),
        "leases": list(facts.leases),
        "source_sha256": source.sha256,
        "reason": reason,
        "actor": actor,
    }
    cursor = db.execute(
        "INSERT INTO events(at, kind, run_key, detail) VALUES(?,?,?,?)",
        (now, RECOVER, facts.run_key, json.dumps(detail, sort_keys=True)),
    )
    return cursor.lastrowid


def _rule(facts: _Facts, charged: int) -> str:
    return DECLARED if charged == facts.prior_charged else WAIVER


def _config_of(db: sqlite3.Connection, path: Path) -> LedgerConfig:
    pinned = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
    if pinned is None:
        raise ValueError(f"{path} holds no ledger configuration")
    return LedgerConfig.from_json(pinned[0])


def recover_in_file(
    path: Path,
    run_key: str,
    charged: int,
    source: Path,
    reason: str,
    actor: str,
    *,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
) -> Recovered:
    """Recover the run `run_key` in the working copy of a ledger at `path`, without its dispatcher lock, for an operator
    while a controller runs (the module's docstring), given the sealed final it was copied from at `source`. With
    `dry_run` decide every condition, again at the end as a real call does, and write nothing. Refused
    (`RecoverRefused`, nothing written) as the docstring says, and while a live controller does not declare that it
    honours operator writes. `FileNotFoundError` for a ledger that is not there, `ValueError` for a run key that says
    nothing, a charged count that is not a whole number, or a reason or an actor that says nothing."""
    if not isinstance(run_key, str) or not run_key.strip():
        raise ValueError(f"a run is named by its run key, got {run_key!r}")
    if isinstance(charged, bool) or not isinstance(charged, int):
        raise ValueError(f"a charged count is a whole number, got {charged!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if not path.is_file():  # a file that is not there is a usage error before it is a controller's business
        raise FileNotFoundError(f"{path}: no ledger there")
    _controller(path)  # at once, so an older controller is refused before the sealed final is read
    db = schema.connect(path)
    try:
        config = _config_of(db, path)
        with _reading(db):  # the first decision, so that a refusal of the ledger's own conditions costs no reading
            _facts(db, config, run_key, charged)
        evidence = read_source(source, path, run_key)  # no write lock is held while the sealed final is read
        if dry_run:  # what a real call decides last, in a transaction that writes nothing
            with _reading(db):
                controller, facts = _controller(path), _verified(db, config, run_key, charged, evidence)
            seq = None
        else:
            with schema.transaction(db):  # the write lock is held: no controller can issue until this commits
                controller, facts = _controller(path), _verified(db, config, run_key, charged, evidence)
                seq = _write(db, clock(), facts, charged, evidence, reason, actor)
        return Recovered(
            run_key,
            facts.prior_state,
            facts.prior_charged,
            charged,
            _rule(facts, charged),
            facts.leases,
            evidence.sha256,
            seq,
            controller,
        )
    finally:
        db.close()


def described(done: Recovered) -> str:
    """What a recovery (or a dry run) shows an operator: the run, its state and charged count before and after, the
    rule and the event's number."""
    lead = "would recover" if done.event_seq is None else "recovered"
    tail = "" if done.event_seq is None else f", event #{done.event_seq}"
    return (
        f"{lead} run {done.run_key}: {done.prior_state} with {done.prior_charged} charged -> pending with "
        f"{done.charged} charged, rule {done.rule}{tail}"
    )


__all__ = ["DECLARED", "RECOVER", "WAIVER", "Recovered", "Source", "described", "read_source", "recover_in_file"]
