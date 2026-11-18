# Provenance: new
"""The node worker (#462 step 3, C43): one long-running process in a federation's held job, one lease at a time.

`python -m expcore.campaign.elastic.worker --federation LABEL --channel DIR --study DIR --work DIR` watches the
channel's inbox (`elastic.channel`) by listing it, and for each lease, in lease-id order:

1. **Takes it, or refuses it.** A lease file for another federation, a tombstone, or a lease id not above the last one
   this federation's worker took (`worker/state.json`, kept across restarts) is never run: a replayed lease is refused
   and nothing is written for it. A lease is taken by creating `worker/running.json` (only if no file has the name),
   so two workers on one channel never run two leases at once, and the last id is written before the run starts.
2. **Runs it** as `expcore run <copy> --lease <file>` (`ProcessRunner`), in a copy of the study of its own (a lease
   store is always new, and the kernel keeps its store in `<study>/out`), under `<work>/lease-<id>/`, and writes a
   heartbeat (`outbox/heartbeat.json`: the lease id and a counter from 1) when it takes the lease and every `BEAT_S`
   while the run lives.
3. **Reports it.** From the lease store (never from the exit status): with at least one attempt, the store's `out/` is
   archived to `archives/lease-<id>.out.tar` and a completion names the lease, its run, the store, the archive's
   SHA-256, the last attempt's state, this store's charged attempts (the lease's base not included), the highest
   attempt number it used and its spend as the budget gate counts it (`selection_ledger.spend_of`; the grant, its
   bound, when that is unknown). A lease that began no attempt (refused by the kernel, or held before any attempt) is
   returned, with what its store spent. A run that ended with an attempt still `running` is reported by neither: the
   worker stops with `running.json` in place, so no other lease runs here, and the ledger expires the lease.
4. Waits for the next. After a drain (`inbox/drain.json`, the login watchdog's), it returns any lease not yet taken,
   writes `outbox/exited.json` and stops.

Live-unproven: the worker was run against a temp-dir channel only; its launch inside the held job is step 6's.
"""

import argparse
import contextlib
import hashlib
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

from expcore.campaign.elastic import channel as ch
from expcore.campaign.run_lease import LeaseError, RunLease, read_lease

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
    def start(self, study: Path, lease_file: Path, log: Path) -> Handle: ...


class _Process:
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def wait(self, timeout: float) -> int | None:
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


class ProcessRunner:
    """`<prefix> <study> --lease <file>`, its output to the lease's log, in a session of its own."""

    def __init__(self, prefix: Sequence[str]) -> None:
        self.prefix = tuple(prefix)

    def start(self, study: Path, lease_file: Path, log: Path) -> Handle:
        with log.open("ab") as sink:
            process = subprocess.Popen(
                [*self.prefix, str(study), "--lease", str(lease_file)],
                cwd=study.parent,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return _Process(process)


@dataclass(frozen=True)
class Outcome:
    """What a lease store says: its attempts, their charged ones, the last one's state, the highest attempt number and
    the spend (`spend_known`: False when it is the grant, the bound)."""

    attempts: int
    charged: int
    state: str | None
    attempts_started: int
    spend_usd: float
    spend_known: bool


def store_outcome(study: Path, lease: RunLease) -> Outcome | None:
    """Read a lease's store in the study copy; None when the kernel made no store."""
    path = study / "out" / "state.sqlite"
    if not path.is_file():
        return None
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT attempt, state, charged FROM attempts ORDER BY id").fetchall()
        spend = _spend(study, lambda sql, args=(): connection.execute(sql, args).fetchall())
    finally:
        connection.close()
    known = spend is not None
    return Outcome(
        attempts=len(rows),
        charged=sum(int(row["charged"]) for row in rows),
        state=rows[-1]["state"] if rows else None,
        attempts_started=max((int(row["attempt"]) for row in rows), default=0),
        spend_usd=spend if known else lease.grant_usd,
        spend_known=known,
    )


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
    ) -> None:
        self.label = label
        self.channel = channel
        self.study = study
        self.work = work
        self.runner = runner
        self.outcome = outcome
        self.clock = clock
        self.beat_s = beat_s
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
        """Make the channel's directories; refuse a channel where a lease is running or ended unreported."""
        ch.make_dirs(self.channel)
        if RUNNING in ch.listing(self.channel / ch.WORKER):
            raise Occupied(
                f"federation {self.label}: a lease is running here, or one ended unreported (worker/{RUNNING}): "
                "this federation takes no other lease"
            )

    def last(self) -> int:
        """The last lease id this federation's worker took (0: none)."""
        record = ch.read_listed(self.channel / ch.WORKER, ch.listing(self.channel / ch.WORKER), STATE)
        if record is None:
            return 0
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
            if lease_id <= last:
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
        if lease_id <= self.last():  # another worker took it between the look and the take
            self._running.unlink()
            return "busy"
        ch.write_json(self._state, {"format": STATE_FORMAT, "federation": self.label, "last_lease_id": lease_id})
        if drain:
            self._return(lease_id, record["lease"].get("run_key"), 0.0, "draining", None)
            self._running.unlink()
            return "returned"
        result = self._run(lease_id, record["lease"])
        self._running.unlink()
        return result

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

    def _return(self, lease_id: int, run_key: Any, spend: float, reason: str, status: int | None) -> None:
        ch.write_json(
            self.outbox / ch.return_name(lease_id),
            {
                "format": ch.RETURN_FORMAT,
                "federation": self.label,
                "lease_id": lease_id,
                "run_key": run_key,
                "spend_usd": spend,
                "reason": reason,
                "exit_status": status,
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
            self._return(lease_id, kernel.get("run_key"), 0.0, f"refused: {error}", None)
            return "returned"
        copy = place / "study"
        top = self.study.resolve()
        _copy_study(top, copy)
        handle = self.runner.start(copy, lease_file, place / "run.log")
        while (status := handle.wait(self.beat_s)) is None:
            counter += 1
            self._beat(lease_id, counter)
        found = self.outcome(copy, lease)
        if found is None or found.attempts == 0:
            spend = 0.0 if found is None else found.spend_usd
            self._return(lease_id, lease.run_key, spend, "no attempt began", status)
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
                "spend_usd": found.spend_usd,
                "spend_known": found.spend_known,
                "exit_status": status,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = ProcessRunner([args.python, "-I", "-B", "-m", "expcore", "run"])
    worker = Worker(args.federation, args.channel, args.study, args.work, runner, beat_s=args.beat_s)
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
