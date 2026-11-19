# Provenance: new
"""Several controllers, one per ledger, that cooperate through read-only reads of each other's ledgers (C53).

Scheduling only: nothing here writes a ledger, a lease, a settle or a record. A sibling ledger is read with SQLite's
`mode=ro`, a short busy timeout, and never its dispatch lock (`read_only_db`).

- **Shared caps** (`SharedCaps`). A model named in `shared_caps` takes a lease only while this ledger's cap slots of it
  plus every sibling's stay below the shared cap. Slots are what each ledger's own cap counts (`LedgerViews.slots`):
  leases in flight, and expired ones whose holder is not yet proven ended. `SharedLedger` is the ledger with that limit
  folded into its caps in force (`caps()`), which `Ledger.issue` and the controller's target already read. Before each
  issue the siblings are read again, so an overshoot needs two controllers to issue one model within one read-to-commit
  window: at most one lease per controller issuing at that moment. A sibling that cannot be read makes every shared
  model full (no lease of it) until a read succeeds. So does a sibling whose file does not exist yet.
- **Waiting** (`waiting_of`). A ledger waits for capacity while it could issue more leases now than it has federations
  free to take them. That is `LedgerView.leases_wanted()` (each model of its current set with runs pending, within its
  cap in force less its slots and within its budget and ceilings), less its federations that are joined, not drained,
  not proven ended, before their recorded job end, and hold no lease with a slot. Its controller's Adds still on
  their way are not in its ledger, so they are not counted: a ledger whose Adds are coming up reads as waiting.
"""

import dataclasses
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from expcore.campaign.elastic import schema
from expcore.campaign.elastic.config import LedgerConfig
from expcore.campaign.elastic.controlinputs import ledger_view
from expcore.campaign.elastic.ledger import Ledger
from expcore.campaign.elastic.records import Lease, Wait
from expcore.campaign.elastic.views import LedgerReader, LedgerViews

READ_TIMEOUT_S = 2.0
UNREADABLE = (OSError, sqlite3.Error, ValueError, KeyError)  # what a read of another ledger may raise: it is unread


def identity(path: Path) -> str:
    """One ledger file however it is named."""
    return os.path.realpath(path)


def read_only_db(path: Path, timeout_s: float = READ_TIMEOUT_S) -> sqlite3.Connection:
    """The ledger file at `path` opened read-only (`mode=ro`, nothing it does can write the file), with a short busy
    timeout and no dispatch lock. ValueError for a file that is not a ledger."""
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no ledger there")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout_s, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        found = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if found is None or found[0] != schema.SCHEMA:
            raise ValueError(f"{path} is not a ledger of {schema.SCHEMA}")
    except BaseException:
        db.close()
        raise
    return db


def slots_of(path: Path, timeout_s: float = READ_TIMEOUT_S) -> dict[str, int]:
    """Each model's cap slots in the ledger at `path`, exactly as its own cap counts them (`LedgerViews.slots`)."""
    db = read_only_db(path, timeout_s)
    try:
        return dict(db.execute("SELECT model, COUNT(*) FROM leases WHERE slot=1 GROUP BY model").fetchall())
    finally:
        db.close()


class ReadOnlyLedger(LedgerViews):
    """Another controller's ledger, read through `read_only_db`: its views, never a write."""

    def __init__(self, path: Path, timeout_s: float = READ_TIMEOUT_S) -> None:
        self.db = read_only_db(path, timeout_s)
        try:
            pinned = self._meta("config")
            if pinned is None:
                raise ValueError(f"{path} holds no ledger configuration")
            self.config = LedgerConfig.from_json(pinned)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()


@dataclass
class SharedCaps:
    """`shared_caps` over the sibling ledgers: what was read of them at the last `refresh`."""

    siblings: tuple[Path, ...]
    caps: Mapping[str, int]
    timeout_s: float = READ_TIMEOUT_S
    by_sibling: dict[str, dict[str, int]] = field(default_factory=dict)  # each sibling read, its slots per model
    unread: tuple[str, ...] = ("not read yet",)  # why each sibling not read at the last refresh was not

    def __post_init__(self) -> None:
        for model, cap in self.caps.items():
            if not isinstance(model, str) or not model:
                raise ValueError("shared_caps names each model by its key")
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                raise ValueError(f"shared_caps.{model} must be a whole number from 1, got {cap!r}")
        if not self.siblings or len({identity(p) for p in self.siblings}) != len(self.siblings):
            raise ValueError("siblings names at least one ledger, each once")

    def refresh(self) -> tuple[str, ...]:
        """Read every sibling again; return why each one that could not be read was not (empty: all were)."""
        found: dict[str, dict[str, int]] = {}
        unread = []
        for path in self.siblings:
            try:
                found[identity(path)] = slots_of(path, self.timeout_s)
            except UNREADABLE as error:
                unread.append(f"{path}: {type(error).__name__}: {error}")
        self.by_sibling, self.unread = found, tuple(unread)
        return self.unread

    def others(self) -> dict[str, int] | None:
        """Every sibling's slots of each shared model, summed; None unless every sibling was read."""
        if self.unread:
            return None
        return {model: sum(slots.get(model, 0) for slots in self.by_sibling.values()) for model in self.caps}

    def limit(self) -> dict[str, int]:
        """How many slots of each shared model this ledger may hold now: its shared cap less every sibling's, 0 (full)
        when a sibling was not read."""
        others = self.others()
        return {model: 0 if others is None else max(cap - others[model], 0) for model, cap in self.caps.items()}

    def tighten(self, caps: Mapping[str, int]) -> dict[str, int]:
        """The caps in force with each shared model's limit folded in: the least of the two."""
        out = dict(caps)
        for model, limit in self.limit().items():
            out[model] = limit if model not in out else min(out[model], limit)
        return out

    def state(self) -> dict:
        """What the log keeps of the last read."""
        others = self.others()
        return {"caps": dict(self.caps), "others": others, "limit": self.limit(), "unread": list(self.unread)}


class _Overlay:
    """The caps in force with the shared caps folded in, for every read of them: the lease's choice and the target."""

    shared: SharedCaps | None = None

    def caps(self) -> dict[str, int]:
        own = super().caps()  # type: ignore[misc]
        return own if self.shared is None else self.shared.tighten(own)


class SharedLedger(_Overlay, Ledger):
    """The ledger a controller with `shared_caps` holds: before each issue the siblings are read again, and the issue
    (`Ledger.issue`, unchanged) chooses under the caps in force with the shared limit folded in."""

    def issue(self, label: str) -> Lease | Wait:
        if self.shared is not None:
            self.shared.refresh()
        return super().issue(label)


class SharedReader(_Overlay, LedgerReader):
    """A dry run's read of the ledger (`LedgerReader`), with the shared limit folded into its caps."""


@dataclass(frozen=True)
class Waiting:
    """A ledger's leases wanted now and its free federations; `count` is how many runs wait for capacity."""

    wanted: int
    free: int

    @property
    def count(self) -> int:
        return max(self.wanted - self.free, 0)


def waiting_of(
    path: Path,
    now: float,
    shared: SharedCaps | None = None,
    own_slots: Mapping[str, int] | None = None,
    timeout_s: float = READ_TIMEOUT_S,
) -> Waiting:
    """How many of the ledger's runs at `path` wait for capacity now (the module's docstring), read in one read-only
    transaction. With `shared` (this controller's shared caps) read whole, each shared model of that ledger is also
    held to what the shared cap leaves of every ledger's slots (`own_slots`, this controller's own, and each
    sibling's), so a ledger held back by a shared cap alone does not read as waiting. Raises what an unreadable ledger
    raises (`UNREADABLE`)."""
    reader = ReadOnlyLedger(path, timeout_s)
    try:
        reader.db.execute("BEGIN")
        try:
            view = ledger_view(reader)
        finally:
            reader.db.execute("COMMIT")
    finally:
        reader.close()
    if shared is not None and not shared.unread:
        view = dataclasses.replace(view, caps=_held_to_shared(view.caps, view.demand.slots, path, shared, own_slots))
    free = sum(
        1
        for fed in view.federations.values()
        if fed.ended_at is None and fed.drained_at is None and now < fed.job_end_at and fed.label not in view.holding
    )
    return Waiting(wanted=view.leases_wanted(), free=free)


def _held_to_shared(
    caps: Mapping[str, int],
    slots: Mapping[str, int],
    path: Path,
    shared: SharedCaps,
    own_slots: Mapping[str, int] | None,
) -> dict[str, int]:
    """A ledger's caps with each shared model held to its own slots plus what the shared cap leaves of all slots."""
    out = dict(caps)
    counted: Iterable[Mapping[str, int]] = [*shared.by_sibling.values(), own_slots or {}]
    if identity(path) not in shared.by_sibling:
        counted = [*counted, slots]
    for model, cap in shared.caps.items():
        total = sum(each.get(model, 0) for each in counted)
        held = slots.get(model, 0) + max(cap - total, 0)
        out[model] = held if model not in out else min(out[model], held)
    return out


__all__ = [
    "READ_TIMEOUT_S",
    "UNREADABLE",
    "ReadOnlyLedger",
    "SharedCaps",
    "SharedLedger",
    "SharedReader",
    "Waiting",
    "identity",
    "read_only_db",
    "slots_of",
    "waiting_of",
]
