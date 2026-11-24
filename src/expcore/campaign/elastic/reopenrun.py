# Provenance: new
"""An operator's reopening of one run that a study-plugin defect ended as harness_error, with the evidence for it.

A harness error is terminal: the run's state stays so (`attempts`: "a run that is done or harness_error stays so"). One
case is not the run's own: an attempt was set aside and charged nothing, as an infrastructure failure of an uncharged
kind (`UNCHARGED_KINDS`: the model API refused the key, HTTP 401 or 403, `key_rejected`; or the budget held the
request, `budget_hold`), the store ran that attempt again under the same attempt number (`Store.begin_attempt`: an
uncharged attempt is run again under the number it had), and the plugin's instrument failed at its start, a
`LedgerError` of the plugin's own, which ended the run as harness_error. `reopen-run` puts such a run back to pending,
and nothing else.

It is refused, writing nothing, unless all of these hold, read in one transaction on the ledger's own write lock (as
`pause` and `drain` are written, beside a running controller):

1. lease N exists, is `counted`, is the latest lease of its run (the run's `lease_id`, and no later lease of it), and
   charged nothing (its `charged_added` and its completion's `charged` are both 0: an earlier lease of the run may
   have charged, lease N may not);
2. the run's state is `harness_error`;
3. the archive at the given path (a plain tar, or a `.tar.zst` read as the tar it holds) is a regular file, never a
   link, and its SHA-256 is the one the lease's completion names: it is the archive the ledger verified
   (`leasearchive`);
4. that archive's `out/state.sqlite` holds attempt rows for the run in which every row is uncharged, the last (highest
   id) is a harness error whose detail starts with `LedgerError`, and an earlier row is an infrastructure failure of an
   uncharged kind (`UNCHARGED_KINDS`) with the last row's attempt number (the same-number run again);
5. no lease of the run is in flight (`issued`, `sent`) or expired with its holder not proven ended;
6. the run has an attempt left: its charged attempts are below `max_attempts`. A pending run with none would break the
   rule that such a run is `infra_exhausted`, and no lease could take it.

and, as `pause` and `drain` are, while no live controller fails to say it honours operator writes (`awareness`).

The archive is read before the write lock is taken (it may be large, and a controller shares the lock): conditions 1, 2,
5 and 6 are decided once to learn the completion's hash, the archive is read against it, and then the lock is taken and
conditions 1, 2, 5 and 6, the completion's hash and the controller's declaration are decided again on what the file says
then, before the change: a controller of an older core may have taken the ledger while the archive was read, and it
would lease the run it knows nothing of. A dry run decides them all again at its end, in a read transaction, so it never
says yes to what a real call would refuse. The completion of a counted lease never changes in a ledger the core wrote,
but the transaction does not rely on it.

The change is the run's state set to `pending` (its charged attempts, its latest lease and every lease row stay as
they were, so the next lease's `attempt_base` is the run's charged count and the lease is issued as any other) and one
event of kind `operator_reopen`: the run key, the lease, the archive's SHA-256 and form, the attempt rows' summary (id,
attempt, state, infrastructure kind, charged), the run's charged count, the reason and the actor. It is the ledger's
own log: no table or column is added, and a core that does not know the kind reads the ledger as it did (the events
are read by kind, and the merge's replay ignores kinds it does not name). The reopening is once: the run is `pending`
afterwards, so a second call is refused by condition 2. With `dry_run` every condition is decided and nothing is
written.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import awareness, schema
from expcore.campaign.elastic.ceilings import text_of
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.leasearchive import ArchiveEvidence, AttemptRow, read_evidence, refused
from expcore.campaign.elastic.records import ReopenRefused

REOPEN = "operator_reopen"  # the event kind: not `reopen`, which the ledger logs when a settle lifts an exhausted run
_LIVE_OR_UNPROVEN = (
    "SELECT l.id, l.state FROM leases l JOIN federations f ON f.label = l.federation WHERE l.run_key=?"
    " AND (l.state IN ('issued', 'sent') OR (l.state = 'expired' AND f.ended_at IS NULL)) ORDER BY l.id"
)


@dataclass(frozen=True)
class Reopened:
    """What a reopening did, or with a dry run what it would do: the run, the lease, the archive's SHA-256 and form, the
    attempt rows it showed, the run's charged attempts (unchanged), the event's number (None for a dry run) and the
    controller's state (`awareness`)."""

    run_key: str
    lease_id: int
    archive_sha256: str
    archive_form: str
    attempts: tuple[AttemptRow, ...]
    run_charged: int
    event_seq: int | None
    controller: str


@dataclass(frozen=True)
class _Facts:
    """What the ledger says of lease N and its run, once every ledger condition holds."""

    run_key: str
    archive_sha256: str
    run_charged: int


def _completion(lease: sqlite3.Row) -> dict[str, Any]:
    """The completion that `lease` holds, as a dict; refused (condition 3) if it holds none that names an archive."""
    try:
        completion = json.loads(lease["completion"])
        if isinstance(completion, dict) and isinstance(completion["archive_sha256"], str):
            return completion
    except (TypeError, ValueError, KeyError):
        pass
    raise refused(3, "archive", f"lease {lease['id']} holds no completion naming an archive")


def _lease_of(db: sqlite3.Connection, lease_id: int) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any]]:
    """Decide condition 1 on the ledger as it is: lease N exists, is counted, is the latest of its run, and charged
    nothing. Return the lease, its run and its completion; `ReopenRefused` otherwise."""
    lease = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if lease is None:
        raise refused(1, "the lease", f"the ledger has no lease {lease_id}")
    if lease["state"] != "counted":
        raise refused(1, "the lease", f"lease {lease_id} is {lease['state']}, not counted")
    run_key = lease["run_key"]
    latest = db.execute("SELECT MAX(id) FROM leases WHERE run_key=?", (run_key,)).fetchone()[0]
    run = db.execute("SELECT state, charged, lease_id FROM runs WHERE run_key=?", (run_key,)).fetchone()
    if latest != lease_id or run["lease_id"] != lease_id:
        raise refused(
            1,
            "the lease",
            f"lease {lease_id} is not the latest lease of its run (lease {max(latest, run['lease_id'] or 0)} is later)",
        )
    completion = _completion(lease)
    said = completion.get("charged")
    if lease["charged_added"] != 0 or isinstance(said, bool) or said != 0:
        raise refused(
            1,
            "the lease",
            f"lease {lease_id} charged its run (counted for {lease['charged_added']} attempt(s), its completion says "
            f"{said!r}): only a lease that charged nothing is reopened",
        )
    return lease, run, completion


def _facts(db: sqlite3.Connection, config: LedgerConfig, lease_id: int) -> _Facts:
    """Decide conditions 1, 2, 5 and 6 on the ledger as it is; raise `ReopenRefused` for the first that fails."""
    lease, run, completion = _lease_of(db, lease_id)
    run_key = lease["run_key"]
    if run["state"] != "harness_error":
        raise refused(2, "the run", f"run {run_key} is {run['state']}, not harness_error")
    out = db.execute(_LIVE_OR_UNPROVEN, (run_key,)).fetchall()
    if out:
        shown = ", ".join(f"lease {row['id']} ({row['state']})" for row in out)
        raise refused(5, "no lease in flight", f"{shown}: in flight, or expired with its holder not proven ended")
    if run["charged"] >= config.max_attempts:
        raise refused(
            6,
            "an attempt left",
            f"run {run_key} has {run['charged']} charged attempts, the most it may have ({config.max_attempts}): a "
            "pending run with none left cannot be leased",
        )
    return _Facts(run_key, completion["archive_sha256"], run["charged"])


def _verified(db: sqlite3.Connection, config: LedgerConfig, lease_id: int, evidence: ArchiveEvidence) -> _Facts:
    """Decide every ledger condition and that the completion names the archive the evidence was read against."""
    facts = _facts(db, config, lease_id)
    if facts.archive_sha256 != evidence.sha256:
        raise refused(
            3, "archive", f"lease {lease_id}'s completion names a different archive from the one that was read"
        )
    return facts


def _controller(path: Path) -> str:
    """The state of the controller that holds the ledger (`awareness`); refused while it is one of an older core, which
    does not say it honours operator writes and would lease on without the reopening in mind."""
    controller = awareness.controller_of(path)
    if controller == awareness.OLDER:
        raise ReopenRefused(
            "a controller holds this ledger and does not say it honours operator writes beside it: it runs a core "
            "older than the operator's pause. Restart it on this commit or later, then reopen the run (a controller "
            "only now starting reads as older for a moment: run the command again)"
        )
    return controller


@contextmanager
def _reading(db: sqlite3.Connection) -> Iterator[None]:
    """A read transaction, never committed: no write lock is taken and nothing is written."""
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.execute("ROLLBACK")


def reopen(
    db: sqlite3.Connection,
    config: LedgerConfig,
    now: float,
    lease_id: int,
    evidence: ArchiveEvidence,
    reason: str,
    actor: str,
) -> int:
    """Reopen the run of lease `lease_id` in the caller's write transaction, given the archive's evidence, and record
    it; return the event's number. Every ledger condition is decided here, on what the ledger says now (`ReopenRefused`,
    nothing written), and the evidence must have been read against the hash the lease's completion names."""
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    return _write(db, now, _verified(db, config, lease_id, evidence), lease_id, evidence, reason, actor)


def _write(
    db: sqlite3.Connection, now: float, facts: _Facts, lease_id: int, evidence: ArchiveEvidence, reason: str, actor: str
) -> int:
    """Put the run of `facts` back to pending and log it; return the event's number."""
    changed = db.execute("UPDATE runs SET state='pending' WHERE run_key=? AND state='harness_error'", (facts.run_key,))
    if changed.rowcount != 1:
        raise refused(2, "the run", f"run {facts.run_key} is not harness_error")
    schema.add_event(
        db,
        now,
        REOPEN,
        run_key=facts.run_key,
        lease_id=lease_id,
        archive_sha256=evidence.sha256,
        archive_form=evidence.form,
        attempts=[row.summary() for row in evidence.attempts],
        run_charged=facts.run_charged,
        reason=reason,
        actor=actor,
    )
    return db.execute("SELECT max(seq) FROM events").fetchone()[0]


def _config_of(db: sqlite3.Connection, path: Path) -> LedgerConfig:
    pinned = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
    if pinned is None:
        raise ValueError(f"{path} holds no ledger configuration")
    return LedgerConfig.from_json(pinned[0])


def reopen_in_file(
    path: Path,
    lease_id: int,
    archive: Path,
    reason: str,
    actor: str,
    *,
    dry_run: bool = False,
    clock: Callable[[], float] = time.time,
) -> Reopened:
    """Reopen the run of lease `lease_id` in the ledger file at `path`, without its dispatcher lock, for an operator
    while a controller runs (the module's docstring). With `dry_run` decide every condition, again at the end as a real
    call does, and write nothing. Refused (`ReopenRefused`, nothing written) as the docstring says, and while a live
    controller does not declare that it honours operator writes. `FileNotFoundError` for a ledger that is not there,
    `ValueError` for a lease that is not a whole number from 1 or a reason or an actor that says nothing."""
    if isinstance(lease_id, bool) or not isinstance(lease_id, int) or lease_id < 1:
        raise ValueError(f"a lease is a whole number from 1, got {lease_id!r}")
    reason, actor = text_of("reason", reason), text_of("actor", actor)
    if not path.is_file():  # a file that is not there is a usage error before it is a controller's business
        raise FileNotFoundError(f"{path}: no ledger there")
    _controller(path)  # at once, so an older controller is refused before a large archive is read
    db = schema.connect(path)
    try:
        config = _config_of(db, path)
        with _reading(db):  # the first decision, to learn the hash: no write lock is held while the archive is read
            facts = _facts(db, config, lease_id)
        evidence = read_evidence(archive, facts.run_key, facts.archive_sha256)
        if dry_run:  # what a real call decides last, in a transaction that writes nothing
            with _reading(db):
                controller, facts = _controller(path), _verified(db, config, lease_id, evidence)
            seq = None
        else:
            with schema.transaction(db):  # the write lock is held: no controller can issue until this commits
                controller, facts = _controller(path), _verified(db, config, lease_id, evidence)
                seq = _write(db, clock(), facts, lease_id, evidence, reason, actor)
        return Reopened(
            facts.run_key,
            lease_id,
            evidence.sha256,
            evidence.form,
            evidence.attempts,
            facts.run_charged,
            seq,
            controller,
        )
    finally:
        db.close()


def described(done: Reopened) -> list[str]:
    """What a reopening (or a dry run) shows an operator: the run and lease, the archive, and the attempt rows that
    justified it."""
    lead = "would reopen" if done.event_seq is None else "reopened"
    tail = "" if done.event_seq is None else f" (event #{done.event_seq})"
    lines = [
        f"{lead} run {done.run_key}: harness_error -> pending, its {done.run_charged} charged attempt(s) unchanged"
        f"{tail}",
        f"lease {done.lease_id}, archive {done.archive_form} with SHA-256 {done.archive_sha256}",
        "attempt rows of that lease's store:",
    ]
    lines += [
        f"  row {row.row_id}: attempt {row.attempt} {row.state} {row.infra_kind or '-'} charged {row.charged}"
        for row in done.attempts
    ]
    return lines


__all__ = ["REOPEN", "Reopened", "described", "reopen", "reopen_in_file"]
