# Provenance: new
"""An operator's recovery of one lost run, on a working copy of a campaign's sealed final ledger.

A run the campaign ended as `infra_exhausted` (its charged attempts reached `max_attempts`) or as `harness_error` (a
sandbox that failed at its start) is terminal: the ledger never leases it again. When the study's owner approves running
such a run once the campaign has closed, the recovery is made on a working copy of the sealed final ledger, never on the
final itself, before the campaign's own controller starts on the copy, which then leases the recovered run to a fresh
federation. `recover-run` puts one such run back to pending with a charged count the operator chooses, so that the next
lease's `attempt_base` is that count, and nothing else.

It is refused, writing nothing, unless all of these hold, decided in one transaction on the ledger's own write lock:

1. the run exists and its state is `infra_exhausted` or `harness_error`;
2. no lease of the run is in flight (`issued`, `sent`), `reported`, or `expired` with its holder not proven ended;
2b. every lease of the run has a final count: one that is `counted`, `superseded`, `returned` or `cancelled` has; one
   that is `lost` has when its settle gave its charged attempts (a settlement whose `charged` is not NULL) or the ledger
   has no budget (nothing is settled then, and a lost lease is not returned); one that is `expired` with its holder
   proven ended has only when its settle gave its charged attempts. Anything less may still change after the recovery
   set the run's count: a first settle moves it by that lease's own difference (`attempts.set_charged`), and a return
   of an expired lease takes its attempt back (`attempts.unstarted`). An overdue settle (`settle_overdue`) does not make
   a count final, as the ledger's hold (`attempts.holds`, C68) lets it, because a first settle can still arrive after
   it: the verb is stricter than the hold. In a ledger without a budget an expired lease can never be settled, so a run
   with one is not recovered;
3. the charged count N is a whole number with 0 <= N < `max_attempts`, and not above the run's charged count now: the
   verb keeps or lowers the count, never raises it;
4. the working copy is a regular file, never a link (`lstat`); the sealed final is a regular file, never a link, and
   not the working copy itself (another device and inode, a hard link included) nor one of its side files (its name
   with `-wal`, `-shm` or `-journal` in the same directory), all decided before any connection to the working copy is
   opened, which sets its journal mode; the sealed final has no `-wal` with bytes in it beside it and is in
   rollback-journal mode (bytes 18 and 19 of its header are 1, not 2 for WAL: it is sealed, the file alone is the
   ledger, and a `-wal` kept under another name of a hard link cannot hide); it is a ledger of the same schema
   with the same campaign fingerprint as the working copy; and it holds the run with the same state and the same
   charged count as the working copy does now, so the copy has not diverged for this run. Its bytes are read once into
   a private temporary copy (under `TMPDIR`), which is hashed and is the file every fact is read from, opened read-only
   and immutable (a URI with `mode=ro&immutable=1`), so the SHA-256 recorded and the rows judged are the same bytes,
   nothing is written to the sealed final or beside it, and it also reads from a directory that cannot be written. The
   temporary copy is removed on return. At the decision under the write lock the sealed final's path is looked at
   again, and refused if its device, inode, size or modification time changed or a `-wal` with bytes in it appeared;
5. no controller holds the ledger, and the probe could tell (`awareness`): refused for any live controller, one that
   says it honours operator writes included, and when the locks cannot be read. The recovery is for a working copy
   before its controller starts, so there is no controller to read it, and a controller that starts afterwards reads
   the recovered ledger.

The sealed final is read before the write lock is taken: conditions 1 to 3 are decided once so that a refusal costs no
reading, the sealed final is read, and then the lock is taken and conditions 1 to 5 are decided again on what the files
say then, before the change. A dry run decides them all again at its end, in a read transaction, so it never says yes to
what a real call would refuse.

The change is the run's state set to `pending`, its charged count set to N and its latest lease (`runs.lease_id`) set
to NULL, as for a run never leased: the pointer is the run's own row, no lease row changes, and the next issue sets it
again. With no latest lease, a completion of any earlier lease that is read afterwards is superseded (`Ledger._report`
counts a completion only for the run's latest lease), so it cannot put the recovered run back to `leased` or count it
again. The next lease is issued as any other, with `attempt_base` N, and the run is not ended again while N is below
`max_attempts`. One event of kind `operator_recover` records the run key, its prior state, charged count and latest
lease (`prior_lease_id`), N, the rule, the run's lease ids (sorted), the sealed final's SHA-256, the reason and the
actor. The rule is `declared` when N is the run's own count (the operator declares the run recovered as it stands) and
`waiver` when N is below it (the operator waives the difference). A waiver leaves the run's count below what its leases
were counted for (`charged_added`), which is the operator's, recorded; every lease's count is final (condition 2b), so
nothing moves it afterwards. The log is the ledger's own: no table or column is added, and a core that does not know the
kind reads the ledger as it did, but the merge's replay (`ledger_read`) reads the NULL pointer as consistent only on a
core that knows the kind: a reader of a core before this one accepts a recovered ledger once every recovered run has
been issued again. The recovery is once: the run is `pending` afterwards, so a second call is refused by condition 1.
With `dry_run` every condition is decided and nothing is written.
"""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from expcore.campaign.elastic import awareness, schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.records import RECOVER, RecoverRefused

RECOVERABLE = ("infra_exhausted", "harness_error")
DECLARED, WAIVER = "declared", "waiver"  # the rule: N is the run's own count, or below it
CHUNK = 1 << 20
# reopen-run's query for a lease in flight or expired with its holder not proven ended, and a lease reported too: its
# completion is read and its archive not yet verified, so its count may still be set
_IN_THE_WAY = (
    "SELECT l.id, l.state FROM leases l JOIN federations f ON f.label = l.federation WHERE l.run_key=?"
    " AND (l.state IN ('issued', 'sent', 'reported') OR (l.state = 'expired' AND f.ended_at IS NULL)) ORDER BY l.id"
)
# the lost or expired leases of the run that no settle gave its charged attempts: a count that may still move (2b)
_UNSETTLED = (
    "SELECT l.id, l.state FROM leases l WHERE l.run_key=? AND l.state IN ('lost', 'expired')"
    " AND NOT EXISTS (SELECT 1 FROM settlements s WHERE s.lease_id = l.id AND s.charged IS NOT NULL) ORDER BY l.id"
)


@dataclass(frozen=True)
class Recovered:
    """What a recovery did, or with a dry run what it would do: the run, its prior state, charged count and latest
    lease, its new charged count and the rule, its lease ids, the sealed final's SHA-256 and the event's number (None
    for a dry run)."""

    run_key: str
    prior_state: str
    prior_charged: int
    prior_lease_id: int | None
    charged: int
    rule: str
    leases: tuple[int, ...]
    source_sha256: str
    event_seq: int | None


@dataclass(frozen=True)
class Source:
    """What the sealed final said when it was read: its path, the SHA-256 of the bytes that were read, its campaign
    fingerprint, the run's state and charged count, and the file's device, inode, size and modification time."""

    path: Path
    sha256: str
    fingerprint: str
    state: str
    charged: int
    signature: tuple[int, int, int, int]


@dataclass(frozen=True)
class _Facts:
    """What the ledger says of the run, once conditions 1 to 3 hold (2b among them)."""

    run_key: str
    prior_state: str
    prior_charged: int
    prior_lease_id: int | None
    leases: tuple[int, ...]


def refused(number: int | str, name: str, why: str) -> RecoverRefused:
    """The refusal of condition `number` (named `name`), for the caller to raise."""
    return RecoverRefused(f"condition {number} ({name}): {why}")


def _facts(db: sqlite3.Connection, config: LedgerConfig, run_key: str, charged: int) -> _Facts:
    """Decide conditions 1, 2, 2b and 3 on the ledger as it is; raise `RecoverRefused` for the first that fails."""
    run = db.execute("SELECT state, charged, lease_id FROM runs WHERE run_key=?", (run_key,)).fetchone()
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
    # a lost lease of a ledger without a budget is final at once: nothing is settled, and a lost lease is not returned
    open_ = [row for row in db.execute(_UNSETTLED, (run_key,)) if row["state"] == "expired" or config.max_usd]
    if open_:
        shown = ", ".join(f"lease {row['id']} ({row['state']})" for row in open_)
        raise refused(
            "2b",
            "an earlier lease's count is not final",
            f"{shown}: no settle gave its charged attempts, so a first settle, or a return of an expired lease, may "
            "still move the run's count",
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
    return _Facts(run_key, run["state"], run["charged"], run["lease_id"], leases)


def _verified(db: sqlite3.Connection, config: LedgerConfig, run_key: str, charged: int, source: Source) -> _Facts:
    """Decide every ledger condition and that the sealed final read is the working copy's origin for this run, and is
    still the file that was read."""
    facts = _facts(db, config, run_key, charged)
    _unchanged(source)
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


def _no_controller(path: Path) -> None:
    """Refuse unless the probe says that no controller holds the ledger (`awareness`): a live controller of any kind,
    and a probe that could not read the locks, are refused. The recovery is for a working copy before its controller
    starts."""
    controller = awareness.controller_of(path)
    if controller == awareness.NONE:
        return
    held = (
        "could not tell whether a controller holds this ledger"
        if controller == awareness.UNKNOWN
        else "a controller holds this ledger"
        + (" and says it honours operator writes" if controller == awareness.AWARE else "")
    )
    raise refused(
        5,
        "controller",
        f"{held}: the recovery is made on a working copy before its controller starts, so stop the controller or "
        "recover on a copy no controller holds (a controller only now starting reads as holding it for a moment: run "
        "the command again)",
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _itself(path: Path) -> RecoverRefused:
    return refused(
        4, "sealed final", f"{path} is the ledger file itself: the sealed final is the file it was copied from"
    )


SIDE_FILES = ("-wal", "-shm", "-journal")  # what SQLite keeps beside a database file, under the file's own name


def refuse_a_linked_ledger(ledger: Path) -> None:
    """Refuse (condition 4) a working copy that is a symbolic link, by `lstat`: a link can be pointed elsewhere between
    the checks and the connection, and one in another directory hides which file the controller's locks guard.
    `FileNotFoundError` for a path that is not there or is not a file."""
    try:
        info = os.lstat(ledger)
    except FileNotFoundError:
        raise FileNotFoundError(f"{ledger}: no ledger there") from None
    if stat.S_ISLNK(info.st_mode):
        raise refused(
            4, "working copy", f"{ledger} is a symbolic link: the working copy is a regular file, never followed"
        )
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError(f"{ledger}: no ledger there")


def refuse_the_ledger_itself(source: Path, ledger: Path) -> None:
    """Refuse (condition 4) a sealed final that is a link, a side file of the working copy (its `-wal`, `-shm` or
    `-journal`: the same directory and the working copy's name with that suffix, which a connection to the working copy
    would use, and could delete), or is the file of `ledger` under any name (the same device and inode, a hard link
    included), by looking at the two paths alone. It is decided before any connection to the ledger is opened, since
    opening one sets its journal mode and could make a `-wal` and `-shm` beside a sealed file."""
    if Path(source).parent.resolve() == Path(ledger).parent.resolve() and Path(source).name in {
        Path(ledger).name + suffix for suffix in SIDE_FILES
    }:
        raise refused(
            4,
            "sealed final",
            f"{source} is a side file of the working copy {ledger}: a connection to the working copy would use it, and "
            "could delete it",
        )
    try:
        info = os.lstat(source)
    except OSError as error:
        raise refused(
            4, "sealed final", f"{source} cannot be opened ({type(error).__name__}: {error.strerror})"
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise refused(4, "sealed final", f"{source} is a symbolic link: it is a regular file, never followed")
    if _identity(info) == _identity(os.stat(ledger)):  # `stat`, not `lstat`: a link to the sealed final is the ledger
        raise _itself(source)


def _wal_bytes(path: Path) -> int:
    """The size of the `-wal` beside the ledger file at `path`, 0 if there is none."""
    try:
        return Path(f"{path}-wal").stat().st_size
    except FileNotFoundError:
        return 0


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


def _copy_and_hash(handle: BinaryIO, destination: Path) -> str:
    """Copy the bytes of `handle` to `destination` in one read and return their SHA-256: the digest and the copy are of
    the same bytes."""
    digest = hashlib.sha256()
    with open(destination, "wb") as out:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
            out.write(block)
    return digest.hexdigest()


def _read_ledger_file(path: Path, run_key: str, shown: Path) -> tuple[str, str, int]:
    """The campaign fingerprint and the run's state and charged count in the ledger file at `path` (the copy of the
    sealed final at `shown`), read-only and immutable, so that nothing is written to it or made beside it."""
    try:
        db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as error:
        raise refused(4, "sealed final", f"{shown} cannot be opened as a database ({error})") from None
    try:
        found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        pinned = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        run = db.execute("SELECT state, charged FROM runs WHERE run_key=?", (run_key,)).fetchone()
    except sqlite3.Error:
        raise refused(4, "sealed final", f"{shown} is not a ledger of {schema.SCHEMA}") from None
    finally:
        db.close()
    if found is None or found[0] != schema.SCHEMA or pinned is None:
        raise refused(4, "sealed final", f"{shown} is not a ledger of {schema.SCHEMA}")
    try:
        fingerprint = LedgerConfig.from_json(pinned[0]).fingerprint
    except (ValueError, TypeError, KeyError):
        raise refused(4, "sealed final", f"{shown} holds no ledger configuration that can be read") from None
    if run is None:
        raise refused(4, "sealed final", f"it holds no run {run_key}")
    return fingerprint, run[0], run[1]


WAL_MODE = "the sealed final is in WAL mode: seal it with a backup-API copy in journal_mode=DELETE"


def _refuse_a_wal_mode_file(path: Path, shown: Path) -> None:
    """Refuse (condition 4) a ledger file whose header does not say rollback journal: bytes 18 and 19 are its write and
    read versions of the file format, 1 for a rollback journal and 2 for WAL. A file in WAL mode keeps its newest rows
    in a `-wal` that the file alone, and its hash, do not hold, and a `-wal` is found by the file's name: a hard link
    under another name would hide it from the check for bytes beside the sealed final."""
    with open(path, "rb") as handle:
        header = handle.read(20)
    versions = tuple(header[18:20])
    if versions != (1, 1):
        why = (
            WAL_MODE
            if 2 in versions
            else f"the sealed final is not in rollback-journal mode (header versions {versions})"
        )
        raise refused(4, "sealed final", f"{shown}: {why}")


def _unchanged(source: Source) -> None:
    """Refuse (condition 4) a sealed final whose path now names another file, or the same file changed (its device,
    inode, size or modification time differ from when it was read), or that has a `-wal` with bytes in it."""
    try:
        info = os.lstat(source.path)
    except OSError as error:
        raise refused(
            4, "sealed final", f"{source.path} cannot be looked at again ({type(error).__name__}: {error.strerror})"
        ) from None
    if _signature(info) != source.signature:
        raise refused(
            4,
            "sealed final",
            f"{source.path} changed after it was read (its device, inode, size or time): it is not sealed",
        )
    if _wal_bytes(source.path) > 0:
        raise refused(
            4,
            "sealed final",
            f"{source.path}-wal holds bytes: the ledger is not sealed, and its file alone is not what the ledger holds",
        )


def read_source(path: Path, ledger: Path, run_key: str) -> Source:
    """Read the sealed final at `path` for the run `run_key` of the working copy at `ledger`: refused
    (`RecoverRefused`, condition 4) unless it is a regular file, never a link, that is not the working copy itself, has
    no `-wal` with bytes in it beside it and is a ledger of the schema that holds the run. Its bytes are read once into
    a private temporary copy, hashed there and read from there, so the digest and the facts are of the same bytes; the
    copy is gone on return. Nothing is written to the sealed final or beside it."""
    with _open_regular(Path(path)) as handle:
        opened = os.fstat(handle.fileno())
        if _identity(opened) == _identity(os.stat(ledger)):
            raise _itself(path)
        if _wal_bytes(path) > 0:
            raise refused(
                4,
                "sealed final",
                f"{path}-wal holds bytes: the ledger is not sealed, and its file alone is not what the ledger holds",
            )
        with tempfile.TemporaryDirectory(prefix="expcore-recover-") as scratch:
            copy = Path(scratch) / "sealed-final.sqlite"
            sha256 = _copy_and_hash(handle, copy)
            fingerprint, state, charged = _read_ledger_file(copy, run_key, Path(path))
            _refuse_a_wal_mode_file(copy, Path(path))
    source = Source(Path(path), sha256, fingerprint, state, charged, _signature(opened))
    _unchanged(source)  # what was read is what is there: nothing wrote to it, or replaced it, while it was copied
    return source


@contextmanager
def _reading(db: sqlite3.Connection) -> Iterator[None]:
    """A read transaction, never committed: no write lock is taken and nothing is written."""
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.execute("ROLLBACK")


def _rule(facts: _Facts, charged: int) -> str:
    return DECLARED if charged == facts.prior_charged else WAIVER


def _write(
    db: sqlite3.Connection, now: float, facts: _Facts, charged: int, source: Source, reason: str, actor: str
) -> int:
    """Put the run of `facts` back to pending with `charged` attempts and no latest lease, and log it; return the
    event's number."""
    changed = db.execute(
        "UPDATE runs SET state='pending', charged=?, lease_id=NULL WHERE run_key=? AND state=? AND charged=?"
        " AND lease_id IS ?",
        (charged, facts.run_key, facts.prior_state, facts.prior_charged, facts.prior_lease_id),
    )
    if changed.rowcount != 1:
        raise refused(
            1, "the run", f"run {facts.run_key} is not {facts.prior_state} with {facts.prior_charged} charged"
        )
    detail = {  # the run key is in the log's column too, which every reader of the log goes by
        "run_key": facts.run_key,
        "prior_state": facts.prior_state,
        "prior_charged": facts.prior_charged,
        "prior_lease_id": facts.prior_lease_id,
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
    """Recover the run `run_key` in the working copy of a ledger at `path`, before its controller starts (the module's
    docstring), given the sealed final it was copied from at `source`. With `dry_run` decide every condition, again at
    the end as a real call does, and write nothing. Refused (`RecoverRefused`, nothing written) as the docstring says,
    and while any controller holds the ledger or the probe cannot tell. `FileNotFoundError` for a ledger that is not
    there, `ValueError` for a run key that says nothing, a charged count that is not a whole number, or a reason or an
    actor that says nothing."""
    if not isinstance(run_key, str) or not run_key.strip():
        raise ValueError(f"a run is named by its run key, got {run_key!r}")
    if isinstance(charged, bool) or not isinstance(charged, int):
        raise ValueError(f"a charged count is a whole number, got {charged!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    refuse_a_linked_ledger(path)  # a file that is not there is a usage error before it is a controller's business
    refuse_the_ledger_itself(Path(source), path)  # before any connection to the ledger: that sets its journal mode
    _no_controller(path)  # at once, so a held ledger is refused before the sealed final is read
    db = schema.connect(path)
    try:
        config = _config_of(db, path)
        with _reading(db):  # the first decision, so that a refusal of the ledger's own conditions costs no reading
            _facts(db, config, run_key, charged)
        evidence = read_source(source, path, run_key)  # no write lock is held while the sealed final is read
        if dry_run:  # what a real call decides last, in a transaction that writes nothing
            with _reading(db):
                _no_controller(path)
                facts = _verified(db, config, run_key, charged, evidence)
            seq = None
        else:
            with schema.transaction(db):  # the write lock is held: no controller can issue until this commits
                _no_controller(path)
                facts = _verified(db, config, run_key, charged, evidence)
                seq = _write(db, clock(), facts, charged, evidence, reason, actor)
        return Recovered(
            run_key,
            facts.prior_state,
            facts.prior_charged,
            facts.prior_lease_id,
            charged,
            _rule(facts, charged),
            facts.leases,
            evidence.sha256,
            seq,
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


__all__ = [
    "DECLARED",
    "RECOVER",
    "WAIVER",
    "Recovered",
    "Source",
    "described",
    "read_source",
    "recover_in_file",
    "refuse_a_linked_ledger",
    "refuse_the_ledger_itself",
]
