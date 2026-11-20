# Provenance: new
"""The node worker (#462 step 3, C44): one long-running process in a federation's held job, one lease at a time.

`python -m expcore.campaign.elastic.worker --federation LABEL --channel DIR --study DIR --work DIR` watches the
channel's inbox (`elastic.channel`) by listing it, and for each lease, in lease-id order:

1. **Takes it, or refuses it.** A lease file for another federation, a tombstone, or a lease id not above the last one
   this federation's worker took (`worker/state.json`, kept across restarts) is never run: a replayed lease is refused
   and nothing is written for it. A lease is taken by creating `worker/running.json` (only if no file has the name),
   so two workers on one channel never run two leases at once, and the last id is written before the run starts.
2. **Runs it** as `expcore run <copy> --lease <file> --spend-journal <place>/spend.jsonl` (`ProcessRunner`), in a copy
   of the study of its own (a lease store is always new, and the kernel keeps its store in `<study>/out`), under
   `<work>/lease-<id>/`, and writes a heartbeat (`outbox/heartbeat.json`: the lease id and a counter from 1) when it
   takes the lease and every `BEAT_S` while the run lives. The lease's *place* is `<channel>/places/lease-<id>/`, on the
   shared file system: the spend journal's fsynced appends survive the loss of the node, and the store's `out/` is
   snapshotted there while the run lives (`elastic.snapshots`), so a controller can settle a lost lease from what
   survived. The worker removes neither (C46).
3. **Reports it.** From the lease store (never from the exit status): with at least one attempt, the store's `out/` is
   archived to `archives/lease-<id>.out.tar` and a completion names the lease, its run, the store, the archive's
   SHA-256, the last attempt's state, this store's attempts, the highest attempt number it used and its charged attempts
   (the lease's base not included), the infrastructure streak its attempts end with and the streak's last kind
   (`attempts.infra_streak`, C58), and its spend as the budget gate counts it (`selection_ledger.spend_of`), in
   micro-dollars rounded up (the grant, its bound, when that is unknown). A lease that began no attempt (refused by the
   kernel, or held before any attempt) is returned, with what its store spent. Both name what the store saw that the
   lease's declared set does not declare (`undeclared_fingerprints`, C48; a fingerprint, or null: a reply with none),
   which pauses the model in the ledger. A run that ended with an attempt still
   `running` is reported by neither: the worker stops with `running.json` in place, so no other lease runs here, and the
   ledger expires the lease.
4. Waits for the next. After a drain (`inbox/drain.json`, the login watchdog's), it returns any lease not yet taken,
   writes `outbox/exited.json` and stops.

Live-unproven: the worker was run against a temp-dir channel only; its launch inside the held job is step 6's.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from expcore.campaign import out_snapshot
from expcore.campaign.attempts import infra_streak
from expcore.campaign.deployments import UNDECLARED_META
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.config import micro_up, usd_of
from expcore.campaign.elastic.snapshots import SNAPSHOT_S, SNAPSHOTS, LeaseSnapshots, Take
from expcore.campaign.run_lease import LEASE_JOURNAL, LeaseError, RunLease, read_lease

BEAT_S = 30.0
POLL_S = 5.0
STATE = "state.json"
RUNNING = "running.json"
STATE_FORMAT = "expcore-elastic-worker/1"
EXIT_DRAINED, EXIT_REFUSED, EXIT_STOPPED = 0, 1, 70


class WorkerError(RuntimeError):
    """The worker cannot go on: it stops, leaving `running.json` when a lease's end could not be reported."""


class Occupied(WorkerError):
    """The channel has a lease running, or one that ended unreported: no worker starts on it."""


class Handle(Protocol):
    def wait(self, timeout: float) -> int | None:
        """The run's exit status, or None when it still runs after `timeout` seconds."""


class Runner(Protocol):
    def start(self, study: Path, lease_file: Path, log: Path, journal: Path) -> Handle:
        """Run the lease on the study copy, its spend journal written to `journal` (a new file)."""


class _Process:
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def wait(self, timeout: float) -> int | None:
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


class ProcessRunner:
    """`<prefix> <study> --lease <file> --spend-journal <journal>`, its output to the lease's log, in a session of its
    own."""

    def __init__(self, prefix: Sequence[str]) -> None:
        self.prefix = tuple(prefix)

    def start(self, study: Path, lease_file: Path, log: Path, journal: Path) -> Handle:
        with log.open("ab") as sink:
            process = subprocess.Popen(
                [*self.prefix, str(study), "--lease", str(lease_file), "--spend-journal", str(journal)],
                cwd=study.parent,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return _Process(process)


@dataclass(frozen=True)
class Outcome:
    """What a lease store says: its attempts, the highest attempt number it used, its charged attempts, the last one's
    state and the spend in whole micro-dollars, rounded up (`spend_known`: False when it is the grant, the bound)."""

    attempts: int
    attempts_started: int
    charged: int
    state: str | None
    spend_micro_usd: int
    spend_known: bool
    undeclared: tuple[str | None, ...] = ()  # what its replies showed that the lease does not declare (C48)
    infra_streak: int = 0  # the infrastructure streak its attempts end with (C58, `attempts.infra_streak`)
    infra_kind: str | None = None  # the kind of the streak's last attempt; None with no streak


def store_outcome(study: Path, lease: RunLease) -> Outcome | None:
    """Read a lease's store in the study copy; None when the kernel made no store."""
    path = study / "out" / "state.sqlite"
    if not path.is_file():
        return None
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT attempt, state, charged, infra_kind FROM attempts ORDER BY id").fetchall()
        spend = _spend(study, lambda sql, args=(): connection.execute(sql, args).fetchall())
        kept = connection.execute("SELECT value FROM meta WHERE key=?", (UNDECLARED_META,)).fetchone()
    finally:
        connection.close()
    known = spend is not None
    streak, kind = infra_streak((row["state"], row["infra_kind"]) for row in rows)
    return Outcome(
        attempts=len(rows),
        attempts_started=max((int(row["attempt"]) for row in rows), default=0),
        charged=sum(int(row["charged"]) for row in rows),
        state=rows[-1]["state"] if rows else None,
        spend_micro_usd=micro_up(spend) if spend is not None else lease.grant_micro_usd,
        spend_known=known,
        undeclared=_undeclared(None if kept is None else kept[0]),
        infra_streak=streak,
        infra_kind=kind,
    )


def _undeclared(raw: str | None) -> tuple[str | None, ...]:
    """What the store kept seen undeclared (`identity.UNDECLARED_META`), every model's, each value once. A record that
    cannot be read is an error: a lease store that saw an undeclared deployment must not be reported as one that
    did not."""
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not all(isinstance(values, list) for values in data.values()):
        raise WorkerError(f"the store's {UNDECLARED_META} is not a record of undeclared fingerprints by model")
    return tuple(dict.fromkeys(value for values in data.values() for value in values))


def _spend(study: Path, query: Callable[..., Any]) -> float | None:
    """The store's spend as its budget gate counts it; None when it cannot be told."""
    try:
        from expcore.campaign.selection_ledger import spend_of
        from expcore.study.config import load_study
        from expcore.study.selection_start import pricing_of

        total = spend_of(query, pricing_of(load_study(study))).total
    except Exception:  # an unknown spend is charged at the grant, its bound, never as nothing
        return None
    return None if total is None else float(total)


class Worker:
    """One federation's worker on its channel."""

    def __init__(
        self,
        label: str,
        channel: Path,
        study: Path,
        work: Path,
        runner: Runner,
        *,
        outcome: Callable[[Path, RunLease], Outcome | None] = store_outcome,
        clock: Callable[[], float] = time.time,
        beat_s: float = BEAT_S,
        snapshot_s: float = SNAPSHOT_S,
        take: Take = out_snapshot.take,
    ) -> None:
        self.label = label
        self.channel = Path(channel).absolute()  # the run's cwd is its lease directory: no path may be relative to ours
        self.study = Path(study).absolute()
        self.work = Path(work).absolute()
        self.runner = runner
        self.outcome = outcome
        self.clock = clock
        self.beat_s = beat_s
        self.snapshot_s = snapshot_s
        self.take = take
        self.refusals: list[str] = []
        self._noted: set[tuple[str, int]] = set()

    @property
    def inbox(self) -> Path:
        return self.channel / ch.INBOX

    @property
    def outbox(self) -> Path:
        return self.channel / ch.OUTBOX

    @property
    def _state(self) -> Path:
        return self.channel / ch.WORKER / STATE

    @property
    def _running(self) -> Path:
        return self.channel / ch.WORKER / RUNNING

    def open(self) -> None:
        """Make the channel's directories; refuse a channel where a lease is running or ended unreported, and a work
        directory inside the study (each lease copies the study into it)."""
        if self.work.resolve().is_relative_to(self.study.resolve()):
            raise WorkerError(f"federation {self.label}: the work directory is inside the study it copies")
        ch.make_dirs(self.channel)
        if RUNNING in ch.listing(self.channel / ch.WORKER):
            raise Occupied(
                f"federation {self.label}: a lease is running here, or one ended unreported (worker/{RUNNING}): "
                "this federation takes no other lease"
            )

    def last(self) -> int:
        """The last lease id this federation's worker took (0: none). A state the listing shows and that cannot be read
        is an OSError (read again at the next poll), never 0, which would take an old lease again."""
        names = ch.listing(self.channel / ch.WORKER)
        if STATE not in names:
            return 0
        record = ch.read_listed(self.channel / ch.WORKER, names, STATE)
        if record is None:
            raise OSError(f"worker/{STATE} is listed and could not be read")
        last = record.get("last_lease_id")
        if record.get("format") != STATE_FORMAT or record.get("federation") != self.label or not isinstance(last, int):
            raise WorkerError(f"federation {self.label}: worker/{STATE} is not this worker's state")
        return last

    def _refuse(self, kind: str, lease_id: int, why: str) -> None:
        if (kind, lease_id) not in self._noted:
            self._noted.add((kind, lease_id))
            self.refusals.append(f"lease {lease_id}: {why}")
            print(f"worker {self.label}: refused lease {lease_id}: {why}", file=sys.stderr)

    def _next(self) -> tuple[int, dict[str, Any]] | None:
        """The lowest lease of this federation above the last one taken; tombstones and other files are passed."""
        names = ch.listing(self.inbox)
        last = self.last()
        for lease_id in sorted(i for i in (ch.named_id(name, "lease") for name in names) if i is not None):
            record = ch.read_listed(self.inbox, names, ch.lease_name(lease_id))
            if record is None or record.get("format") != ch.LEASE_FORMAT:
                self._refuse("form", lease_id, "its file is not a lease file of the channel")
                continue
            if record.get("federation") != self.label:
                self._refuse("federation", lease_id, f"it is for federation {record.get('federation')!r}")
                continue
            if ch.is_tombstone(record):
                continue
            lease = record.get("lease")
            if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
                self._refuse("form", lease_id, "its lease does not carry the id its name does")
                continue
            if lease_id <= last or self._ended(lease_id):
                if not self._ended(lease_id):
                    self._refuse("replay", lease_id, f"its id is not above {last}, the last this worker took")
                continue
            return lease_id, record
        return None

    def _ended(self, lease_id: int) -> bool:
        names = ch.listing(self.outbox)
        return ch.completion_name(lease_id) in names or ch.return_name(lease_id) in names

    def step(self) -> str:
        """One look at the inbox: `ran`, `returned`, `idle`, `busy` or `drained`."""
        drain = ch.DRAIN in ch.listing(self.inbox)
        found = self._next()
        if found is None:
            if drain:
                ch.write_json(
                    self.outbox / ch.EXITED,
                    {
                        "format": ch.EXITED_FORMAT,
                        "federation": self.label,
                        "last_lease_id": self.last(),
                        "reason": "drained",
                        "at": self.clock(),
                    },
                )
                return "drained"
            return "idle"
        lease_id, record = found
        if not ch.create_json(self._running, {"lease_id": lease_id, "pid": os.getpid(), "host": socket.gethostname()}):
            return "busy"
        try:
            taken = lease_id <= self.last() or self._ended(lease_id)  # another worker took it since the look
        except OSError:
            self._running.unlink()  # nothing ran: the lease is looked at again at the next poll
            raise
        if taken:
            self._running.unlink()
            return "busy"
        self._record_take(lease_id)
        if drain:
            self._return(lease_id, record["lease"].get("run_key"), 0, "draining", None)
            self._running.unlink()
            return "returned"
        result = self._run(lease_id, record["lease"])
        self._running.unlink()
        return result

    def _record_take(self, lease_id: int) -> None:
        """Write the lease as the last taken. A write that fails may still have landed (its rename, before the fsync
        of its directory): the lease is then taken and runs; when the state shows the earlier lease the take is undone
        and the lease is looked at again; when the state cannot be read the worker stops, `running.json` in place."""
        state = {"format": STATE_FORMAT, "federation": self.label, "last_lease_id": lease_id}
        try:
            ch.write_json(self._state, state)
            return
        except OSError as error:
            failed = error
        try:
            shown = self.last()
        except OSError as error:
            raise WorkerError(f"lease {lease_id}: whether it was taken cannot be told: {error}") from failed
        if shown != lease_id:
            self._running.unlink()
            raise failed

    def _beat(self, lease_id: int, counter: int) -> None:
        """Write the heartbeat; a write that fails is a beat missed, never the end of a run that still lives."""
        try:
            ch.write_json(
                self.outbox / ch.HEARTBEAT,
                {
                    "format": ch.HEARTBEAT_FORMAT,
                    "federation": self.label,
                    "lease_id": lease_id,
                    "counter": counter,
                    "at": self.clock(),
                },
            )
        except OSError as error:
            print(f"worker {self.label}: lease {lease_id}'s beat {counter} was not written: {error}", file=sys.stderr)

    def _return(
        self,
        lease_id: int,
        run_key: Any,
        spend: int,
        reason: str,
        status: int | None,
        undeclared: tuple[str | None, ...] = (),
    ) -> None:
        ch.write_json(
            self.outbox / ch.return_name(lease_id),
            {
                "format": ch.RETURN_FORMAT,
                "federation": self.label,
                "lease_id": lease_id,
                "run_key": run_key,
                "spend_micro_usd": spend,
                "spend_usd": usd_of(spend),
                "reason": reason,
                "exit_status": status,
                "undeclared_fingerprints": list(undeclared),
            },
        )

    def _run(self, lease_id: int, kernel: dict[str, Any]) -> str:
        counter = 1
        self._beat(lease_id, counter)
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        place = self.work / f"lease-{lease_id:012d}"
        place.mkdir(mode=0o700)
        lease_file = place / "lease.json"
        ch.write_json(lease_file, kernel)
        try:
            lease = read_lease(lease_file)
        except LeaseError as error:
            self._return(lease_id, kernel.get("run_key"), 0, f"refused: {error}", None)
            return "returned"
        copy = place / "study"
        top = self.study.resolve()
        _copy_study(top, copy)
        kept = self.channel / ch.PLACES / ch.place_name(lease_id)  # on the shared file system: a lost node leaves it
        kept.mkdir(mode=0o700, parents=True, exist_ok=True)
        facts = {"federation": self.label, "lease": {"lease_id": lease_id, "run_key": lease.run_key}}
        snapshots = LeaseSnapshots(
            copy / "out", kept / SNAPSHOTS, place / "snapshot-scratch", facts, every_s=self.snapshot_s, take=self.take
        )
        handle = self.runner.start(copy, lease_file, place / "run.log", kept / LEASE_JOURNAL)
        snapshots.start()
        try:
            while (status := handle.wait(self.beat_s)) is None:
                counter += 1
                self._beat(lease_id, counter)
        except BaseException:
            snapshots.stop()
            raise
        snapshots.finish()  # the store its dispatcher left, before the archive and the completion that names it
        found = self.outcome(copy, lease)
        if found is None or found.attempts == 0:
            spend, undeclared = (0, ()) if found is None else (found.spend_micro_usd, found.undeclared)
            self._return(lease_id, lease.run_key, spend, "no attempt began", status, undeclared)
            return "returned"
        if found.state not in ("done", "infra_fail", "harness_error", "interrupted", "outage_discarded"):
            raise WorkerError(
                f"lease {lease_id}: its run ended (exit {status}) with its last attempt {found.state}: its end is not "
                f"reported, and worker/{RUNNING} stays so no other lease runs here"
            )
        archive = self.channel / ch.ARCHIVES / ch.archive_name(lease_id)
        sha = _archive(copy / "out", archive)
        ch.write_json(
            self.outbox / ch.completion_name(lease_id),
            {
                "format": ch.COMPLETION_FORMAT,
                "federation": self.label,
                "lease_id": lease_id,
                "run_key": lease.run_key,
                "store": str(copy / "out"),
                "archive": ch.archive_name(lease_id),
                "archive_sha256": sha,
                "state": found.state,
                "charged": found.charged,
                "attempts": found.attempts,
                "attempts_started": found.attempts_started,
                "spend_micro_usd": found.spend_micro_usd,
                "spend_usd": usd_of(found.spend_micro_usd),
                "spend_known": found.spend_known,
                "exit_status": status,
                "undeclared_fingerprints": list(found.undeclared),
                "infra_streak": found.infra_streak,
                "infra_kind": found.infra_kind,
            },
        )
        return "ran"

    def serve(self, sleep: Callable[[float], None] = time.sleep, poll_s: float = POLL_S) -> int:
        """Take leases until a drain; the exit status of the command."""
        self.open()
        while True:
            try:
                state = self.step()
            except WorkerError:
                raise
            except OSError as error:  # a listing or a write that failed: looked at again at the next poll
                if RUNNING in ch.listing(self.channel / ch.WORKER):
                    raise WorkerError(f"a lease's end could not be written: {error}") from error
                print(f"worker {self.label}: the channel could not be read now: {error}", file=sys.stderr)
                state = "idle"
            if state == "drained":
                return EXIT_DRAINED
            if state in ("idle", "busy"):
                sleep(poll_s)


def _copy_study(study: Path, copy: Path) -> None:
    """The study without its `out/` (or any compiled file), links kept as links."""

    def ignore(directory: str, names: list[str]) -> list[str]:
        skip = [name for name in names if name == "__pycache__"]
        return skip + (["out"] if Path(directory) == study and "out" in names else [])

    shutil.copytree(study, copy, symlinks=True, ignore=ignore)


def _archive(out: Path, archive: Path) -> str:
    """Write `out/` as a tar whole (a draft, fsynced, renamed), 0600; its SHA-256."""
    draft = archive.with_name(f".{archive.name}.draft")
    with contextlib.suppress(FileNotFoundError):
        draft.unlink()
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            with tarfile.open(fileobj=handle, mode="w") as tar:
                tar.add(out, arcname="out")
            handle.flush()
            os.fsync(handle.fileno())
        digest = hashlib.sha256()
        with draft.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        os.replace(draft, archive)
        ch.fsync_dir(archive.parent)  # before the completion that names it is written
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m expcore.campaign.elastic.worker", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--federation", required=True, help="this federation's label, as it joined the ledger")
    parser.add_argument("--channel", required=True, type=Path, help="the federation's channel directory")
    parser.add_argument("--study", required=True, type=Path, help="the study directory each lease runs a copy of")
    parser.add_argument("--work", required=True, type=Path, help="where each lease's copy and log go (node scratch)")
    parser.add_argument("--python", default=sys.executable, help="the interpreter with the core (default: this one)")
    parser.add_argument("--beat-s", type=float, default=BEAT_S, help="seconds between heartbeats while a run lives")
    parser.add_argument("--poll-s", type=float, default=POLL_S, help="seconds between two listings of the inbox")
    parser.add_argument(
        "--snapshot-s", type=float, default=SNAPSHOT_S, help="seconds between snapshots of a running lease's out/"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = ProcessRunner([args.python, "-I", "-B", "-m", "expcore", "run"])
    worker = Worker(
        args.federation, args.channel, args.study, args.work, runner, beat_s=args.beat_s, snapshot_s=args.snapshot_s
    )
    try:
        return worker.serve(poll_s=args.poll_s)
    except Occupied as error:
        print(f"worker {args.federation} refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except WorkerError as error:
        print(f"worker {args.federation} stopped: {error}", file=sys.stderr)
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["BEAT_S", "Occupied", "Outcome", "ProcessRunner", "Worker", "WorkerError", "main", "store_outcome"]
