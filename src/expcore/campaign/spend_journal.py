# Provenance: new
"""The spend journal: what a dispatcher spent, appended to a file that outlives its host (O62).

A campaign's store can live on a host's memory filesystem (an HPC node's tmpfs, `deploy/hpc-federation/shards.py`),
copied to a shared filesystem only now and then. A kill of the host loses what the store gained since its last copy,
and with it what was spent since: the attempts that began, their top-ups, admission's probe sends. The budget would
then count less than was billed. With `expcore run --spend-journal FILE`, the dispatcher appends a line to FILE for
every change of what was spent, once the store has committed it and before the spend it allows:

- `attempt`: an attempt's row as it stands once it began, once it took a top-up and once it ended: its id, run key,
  model, the dispatcher's pid, when it began, its state, the bound it was reserved at (`reserved_capped`, C27), its
  counts, its top-ups, and how many `call` lines of it the journal holds so far (`calls_sent`, `calls_closed`; None
  for an attempt that keeps no call log), which the reader checks the call lines against;
- `call` (C05, `campaign.interrupted_spend`): one line per request an attempt sends, before it goes out
  (`phase` `sent`), and one per reply that closes a request (`phase` `closed`, its `entry`: the reply's counts
  `[input, output, cache writes, cache reads]`, or null for a reply billed and not read), each with the attempt's `id`
  and `run_key`. A request whose last send failed is closed by the `attempt` line of that send's top-up itself
  (`closes`: `"failed"`), so a write cut short loses the top-up and the close together, never one without the
  other. Each call is a line of its own, so the journal grows linearly in the calls; the reader sums them by attempt
  id into the attempt's call log;
- `probe`: a charge of admission's probes (C26): the id of the event the store wrote with it, the model and the
  dollars (a settlement is below 0);
- `unheld`: a model taken off the reply maximum (C27), which raises the bound its attempts are charged.

Every line carries the campaign's fingerprint hash (`campaign`) and the shard's whole spec as the store pins it
(`shard`: rule, index, count and label as canonical JSON, null for a campaign that is not sharded), so a journal of
another campaign or shard, or of the same label in another partition, is refused where it is read
(`campaign.lost_spend`).

**Written after the commit, before the spend.** The store collects a transaction's lines and writes them only once it
has committed (`Store.transact`): a transaction that rolls back leaves no line, so nothing that never happened is
read as spent. A line that cannot be written (the filesystem is unreachable, or full) raises `SpendJournalError`
before the spend the change allows: the attempt does not start, the top-up and the probe are not sent, and the
dispatcher stops (C17). Every later write raises too, so nothing spends unjournaled after a failure. A change the
store committed whose line was not written allowed no spend; an attempt left so is settled as any interrupted one,
by its call log or at its bound. Each write is flushed and fsynced; the file is created new with a header line and
its directory fsynced. A line cut short by a kill is the file's last, and reads as not written: its commit happened,
but nothing was spent on it.
"""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = "expcore-spend-journal/1"
SUFFIX = ".jsonl"
HEADER, ATTEMPT, PROBE, UNHELD, CALL = "header", "attempt", "probe", "unheld", "call"
SENT, CLOSED = "sent", "closed"  # a call line's phase
ROW_FIELDS = (
    "id",
    "run_key",
    "model",
    "pid",
    "started_utc",
    "state",
    "reserved_capped",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "turn_usage",
    "usage_complete",
    "replies_checked",
    "topups",
    "topup_usd",
    "calls_sent",
)
ATTEMPT_FIELDS = (*ROW_FIELDS, "calls_closed")  # calls_closed: the closed requests of the row's call log
PROBE_FIELDS = ("event_id", "model", "usd")


class SpendJournalError(RuntimeError):
    """A journal that cannot be written, or read as one; the message names the file and what is wrong."""


class SpendJournal:
    """One start's journal: created new, appended line by line, each flushed and fsynced before it returns."""

    def __init__(self, path: Path, campaign: str, shard: str | None, now: str) -> None:
        """Create the file with its header; `SpendJournalError` when it exists already or cannot be written."""
        self.path, self.campaign, self.shard = Path(path), campaign, shard
        self.broken: str | None = None  # why a write failed: every later write fails too
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC
        try:
            self._fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            raise SpendJournalError(
                f"{self.path.name}: a journal of that name exists; each start writes its own"
            ) from None
        except OSError as error:
            raise SpendJournalError(f"{self.path.name}: cannot be created ({type(error).__name__})") from None
        self._write([{"kind": HEADER, "format": FORMAT, "started_utc": now, "pid": os.getpid()}])
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            self.broken = type(error).__name__
            raise SpendJournalError(f"{self.path.name}: its directory cannot be synced ({self.broken})") from None

    @property
    def name(self) -> str:
        return self.path.name

    def write(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """Append the entries, each one line with the campaign and the shard, and fsync; raise when they are not."""
        if self.broken is not None:
            raise SpendJournalError(f"{self.name}: an earlier write failed ({self.broken}); nothing more is spent")
        self._write(entries)

    def _write(self, entries: Sequence[Mapping[str, Any]]) -> None:
        text = "".join(
            json.dumps({**entry, "campaign": self.campaign, "shard": self.shard}, sort_keys=True) + "\n"
            for entry in entries
        )
        try:
            data = text.encode("utf-8")
            while data:
                written = os.write(self._fd, data)
                data = data[written:]
            os.fsync(self._fd)
        except OSError as error:
            self.broken = type(error).__name__
            raise SpendJournalError(f"{self.name}: cannot be written ({self.broken}); nothing more is spent") from None

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def attempt_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """The `attempt` line of an attempt's row as the store holds it (the model joined from its run), with the closed
    requests of its call log in place of the log (`calls_closed`; None without a log or with one that cannot be
    read, which no call line then matches)."""
    return {"kind": ATTEMPT, **{name: row[name] for name in ROW_FIELDS}, "calls_closed": _closed(row["calls_usage"])}


def _closed(usage: Any) -> int | None:
    try:
        entries = json.loads(usage) if isinstance(usage, str) else None
    except ValueError:
        return None
    return len(entries) if isinstance(entries, list) else None


def call_entry(attempt_id: int, run_key: str, phase: str, entry: Any = None) -> dict[str, Any]:
    """A `call` line: a request of the attempt before it goes out (`SENT`), or how one closed (`CLOSED`, `entry`)."""
    line = {"kind": CALL, "id": attempt_id, "run_key": run_key, "phase": phase}
    return line if phase == SENT else {**line, "entry": entry}


@dataclass(frozen=True)
class CallLines:
    """An attempt's call lines as read: the requests sent and how each closed one ended, in order; `consistent`
    False when its attempt lines name other counts than its call lines, or it keeps no log and has call lines."""

    sent: int = 0
    closed: tuple[Any, ...] = ()
    consistent: bool = True


@dataclass(frozen=True)
class Journal:
    """A journal as read: its name, each attempt's last line by id (in the order they first appear), the probes' lines,
    the models it took off the reply maximum, and each logged attempt's call lines by id (C05)."""

    name: str
    attempts: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    probes: Sequence[Mapping[str, Any]] = ()
    unheld: frozenset[str] = frozenset()
    calls: Mapping[int, CallLines] = field(default_factory=dict)


def journals_in(directory: Path, *, but: str | None = None) -> list[Path]:
    """The journals in `directory` by name, the one named `but` left out; `SpendJournalError` when it cannot be
    listed."""
    try:
        found = sorted(p for p in Path(directory).iterdir() if p.name.endswith(SUFFIX) and not p.name.startswith("."))
    except OSError as error:
        raise SpendJournalError(f"the journal directory cannot be listed ({type(error).__name__})") from None
    return [p for p in found if p.name != but]


def read_journal(path: Path, campaign: str, shard: str | None) -> Journal:
    """Read a journal, every line of which must be of `campaign` and `shard`.

    A last line that is not whole is read as not written (a kill cut it short, before the spend it would allow). Any
    other line that is not a journal line, and any line of another campaign or shard, is `SpendJournalError` naming
    the file, the line and what is wrong: a journal is never read in part or skipped."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise SpendJournalError(f"{path.name}: cannot be read ({type(error).__name__})") from None
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        lines.pop()  # cut short by a kill: never whole, never spent
    attempts: dict[int, Mapping[str, Any]] = {}
    calls: dict[int, CallLines] = {}
    probes, unheld = [], set()
    if not lines:  # its header was cut short: the start that made it died before anything was spent
        return Journal(path.name)
    for number, line in enumerate(lines, 1):
        entry = _entry(path, number, line)
        if entry.get("campaign") != campaign:
            raise SpendJournalError(
                f"{path.name}: line {number} is of another campaign (fingerprint {entry.get('campaign')}, not "
                f"{campaign}); a journal directory holds one campaign's journals"
            )
        if entry.get("shard") != shard:
            raise SpendJournalError(
                f"{path.name}: line {number} is of shard {entry.get('shard')!r}, not {shard!r}; a journal directory "
                "holds one shard's journals"
            )
        if number == 1:
            if entry.get("kind") != HEADER or entry.get("format") != FORMAT:
                raise SpendJournalError(f"{path.name}: its first line is not a {FORMAT} header")
            continue
        kind = entry.get("kind")
        if kind == ATTEMPT:
            attempt_id = _whole(path, number, entry, "id")
            if attempt_id not in attempts and entry.get("calls_sent") is not None:
                calls[attempt_id] = CallLines()  # the attempt keeps a call log
            attempts[attempt_id] = entry
            if "closes" in entry:  # a top-up's line that closes its request too: never one without the other
                closing = {"phase": CLOSED, "entry": entry["closes"]}
                calls[attempt_id] = _called(path, number, calls.get(attempt_id), closing)
            if attempt_id in calls:
                calls[attempt_id] = _referenced(calls[attempt_id], entry)
        elif kind == CALL:
            attempt_id = _whole(path, number, entry, "id")
            begun = attempts.get(attempt_id)
            if begun is None:
                raise SpendJournalError(f"{path.name}: line {number} is a call line of an attempt it never began")
            if entry.get("run_key") != begun["run_key"]:
                raise SpendJournalError(f"{path.name}: line {number} is a call line of another run than its attempt")
            calls[attempt_id] = _called(path, number, calls.get(attempt_id), entry)
        elif kind == PROBE:
            _whole(path, number, entry, "event_id")
            probes.append(entry)
        elif kind == UNHELD and isinstance(entry.get("model"), str):
            unheld.add(entry["model"])
        else:
            raise SpendJournalError(f"{path.name}: line {number} has no kind this core writes ({kind!r})")
    return Journal(path.name, attempts, tuple(probes), frozenset(unheld), calls)


def _referenced(lines: CallLines, entry: Mapping[str, Any]) -> CallLines:
    """The call lines checked against the counts an attempt line names: any difference makes them inconsistent."""
    named = (entry.get("calls_sent"), entry.get("calls_closed"))
    agree = named == (lines.sent, len(lines.closed))
    return lines if agree else CallLines(lines.sent, lines.closed, False)


def _called(path: Path, number: int, lines: CallLines | None, entry: Mapping[str, Any]) -> CallLines:
    """The call lines with one more: a request sent, or one closed; a call line of an attempt that keeps no log, or
    a reply before its request, makes them inconsistent."""
    phase = entry.get("phase")
    if phase not in (SENT, CLOSED) or (phase == CLOSED and "entry" not in entry):
        raise SpendJournalError(f"{path.name}: line {number} is a call line with no phase this core writes")
    base = CallLines(consistent=False) if lines is None else lines
    if phase == SENT:
        return CallLines(base.sent + 1, base.closed, base.consistent)
    closed = (*base.closed, entry["entry"])
    return CallLines(base.sent, closed, base.consistent and len(closed) <= base.sent)


def _entry(path: Path, number: int, line: bytes) -> Mapping[str, Any]:
    try:
        entry = json.loads(line)
    except ValueError:
        entry = None
    if not isinstance(entry, dict):
        raise SpendJournalError(f"{path.name}: line {number} is not a journal line")
    return entry


def _whole(path: Path, number: int, entry: Mapping[str, Any], key: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpendJournalError(f"{path.name}: line {number} has no whole-number {key}")
    return value
