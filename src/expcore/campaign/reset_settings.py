# Provenance: new
"""The platform's settings that are in no fingerprint, recorded at a start and compared at a resume (C35).

A platform has settings that change how it does its work and not what an agent sees: how a reset finds what it
removes, how long it waits, how many requests it has in flight. They stay out of the campaign fingerprint, so a resume
under other ones is not refused, and the runs that follow then reset or place differently with nothing in the store to
say so. The platform gives the core those settings as a flat mapping of names to plain values (`StudyPlugin.
reset_settings`), after its defaults, and the core never reads one:

- a start records the mapping in the store's meta table, as canonical JSON, under `reset_settings`;
- a resume compares the mapping it is given with the record, key by key. Equal: nothing is written and nothing said.
  Different: the record is replaced, one event `reset_settings_changed` holds each changed key with its old and new
  value (a side that lacks the key has no entry for it), and one warning in the log names each of them. A resume is
  never refused for it;
- a store made before the settings were recorded has no record: the resume records the current settings, writes one
  event `reset_settings_unknown_before` and says once that the settings of the earlier starts are unknown.

A mapping that is empty and a store with no record are left as they were, so a study whose platform supplies none has
the store and the export it always had. The comparison is made in the start's transaction after the fingerprint check,
so a start that is refused records nothing and says nothing. The export carries what the store recorded as one more key
(`export_run_state`), which the report shows among its infrastructure notes.
"""

import json
import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from expcore.campaign.attempts import ResumeRefused
from expcore.campaign.store import Store

META_KEY = "reset_settings"
CHANGED = "reset_settings_changed"  # the event of a resume that found a setting changed
UNKNOWN_BEFORE = "reset_settings_unknown_before"  # the event of the first resume of a store with no record
EXPORT_KEY = "reset_settings"  # the key of the export

log = logging.getLogger(__name__)


class _Absent:
    """The value on the side of a comparison that does not have the key."""

    def __repr__(self) -> str:
        return "absent"


ABSENT = _Absent()


@dataclass(frozen=True)
class Change:
    """One setting that differs between the record and the start: its key and its value on each side."""

    key: str
    old: Any = ABSENT
    new: Any = ABSENT

    def entry(self) -> dict[str, Any]:
        """The change as the event and the export keep it: the key, and each value that is there."""
        entry: dict[str, Any] = {"key": self.key}
        if self.old is not ABSENT:
            entry["old"] = self.old
        if self.new is not ABSENT:
            entry["new"] = self.new
        return entry

    def describe(self) -> str:
        """One line for the log: the key, then the old and the new value as JSON, `absent` where there is none."""
        return f"{self.key}: {_shown(self.old)} -> {_shown(self.new)}"


@dataclass(frozen=True)
class Noted:
    """What a start found: the settings that changed since the record, and whether the record was missing."""

    changes: tuple[Change, ...] = ()
    unknown_before: bool = False


class _Reads(Protocol):
    """What the export reads: a store, or the snapshot of one."""

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]: ...


def _shown(value: Any) -> str:
    return "absent" if value is ABSENT else json.dumps(value)


def checked(settings: object) -> dict[str, Any]:
    """Return a copy of settings the core can record and compare; ValueError names the first one it cannot.

    The mapping is flat: names mapped to text, numbers, booleans or None. Nothing nested is taken, so a record can
    always be compared key by key and shown beside its old value, and a platform that has a structure to watch gives
    its parts as keys of their own. No value may be a NaN or an infinity, which JSON does not carry. Names and values
    travel with the export and the replication package, so a platform leaves out a secret and any name that
    identifies a machine or a private space.
    """
    if not isinstance(settings, Mapping):
        raise ValueError(f"must be a mapping of names to plain values, not a {type(settings).__name__}")
    out: dict[str, Any] = {}
    for key, value in settings.items():
        if not isinstance(key, str):
            raise ValueError(f"a key must be text, not {key!r}")
        if not key:
            raise ValueError("a key must not be empty")
        plain = value is None or isinstance(value, str | int) or (isinstance(value, float) and math.isfinite(value))
        if not plain:
            raise ValueError(f"{key!r} must be text, a finite number, a boolean or None, not {_describe_type(value)}")
        out[key] = value
    return out


def _describe_type(value: object) -> str:
    if isinstance(value, float):
        return "a number that is not finite"
    return f"a {type(value).__name__}"


def canonical(settings: Mapping[str, Any]) -> str:
    """The text a store keeps a mapping as: JSON with sorted keys and no spaces."""
    return json.dumps(checked(settings), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _same(old: Any, new: Any) -> bool:
    """Whether two values are one setting: `2` and `2.0` are, `True` and `1` are not."""
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool) and old == new
    return bool(old == new)


def changes_between(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[Change, ...]:
    """The keys whose value differs, in key order; a key only one side has is a change from or to `absent`."""
    found = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key, ABSENT), new.get(key, ABSENT)
        if before is ABSENT or after is ABSENT or not _same(before, after):
            found.append(Change(key, before, after))
    return tuple(found)


def _recorded(text: str) -> dict[str, Any]:
    """The record a store holds; ValueError when it is not a mapping of plain values."""
    try:
        return checked(json.loads(text))
    except (TypeError, ValueError) as error:
        raise ValueError(f"the recorded settings cannot be read ({error})") from None


def note_within(store: Store, settings: Mapping[str, Any] | None, *, started: bool, now: str) -> Noted:
    """Record the settings at a first start, compare them with the record at a resume, and return what was found.

    Call it inside the start's transaction, after the fingerprint was checked: the record, the event and the start
    stand or fall together. `started` is whether the store had a start before this one, which tells a store with no
    record that was made before settings were recorded from one that is new. `settings` None or empty is a platform
    that supplies none: against no record nothing happens, against a record every recorded key is a change to `absent`.
    """
    current = checked({} if settings is None else settings)
    row = store.db.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    if row is None:
        if not current:
            return Noted()
        _record(store, current, now)
        if not started:
            return Noted()
        detail = (
            "the store recorded no platform settings before this start (it was made before they were recorded): the "
            "settings of its earlier starts are unknown, and the current ones are recorded now"
        )
        store.db.execute(
            "INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, UNKNOWN_BEFORE, None, detail)
        )
        return Noted(unknown_before=True)
    try:
        recorded = _recorded(row["value"])
    except ValueError as error:
        raise ResumeRefused(f"the store's {META_KEY}: {error}") from None
    changes = changes_between(recorded, current)
    if not changes:
        return Noted()
    _record(store, current, now)
    detail = json.dumps({"changes": [change.entry() for change in changes]}, separators=(",", ":"), allow_nan=False)
    store.db.execute("INSERT INTO events(ts,kind,model,detail) VALUES(?,?,?,?)", (now, CHANGED, None, detail))
    return Noted(changes)


def _record(store: Store, settings: Mapping[str, Any], now: str) -> None:
    store.db.execute(
        "INSERT INTO meta(key,value,set_utc) VALUES(?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_utc=excluded.set_utc",
        (META_KEY, canonical(settings), now),
    )


def say(noted: Noted) -> None:
    """Warn in the log of what a start found; a start that found nothing says nothing."""
    if noted.unknown_before:
        log.warning(
            "this store recorded no platform settings at its earlier starts (it was made before they were "
            "recorded), so the settings its earlier runs ran under are unknown; the current ones are recorded now, "
            "and later starts are compared with them"
        )
    if noted.changes:
        lines = "\n  ".join(change.describe() for change in noted.changes)
        log.warning(
            "the platform's settings that are in no fingerprint are not the ones recorded at the last start; runs "
            "from here on use the new values (old -> new):\n  %s",
            lines,
        )


def exported(store: _Reads) -> dict[str, Any] | None:
    """What the store recorded, for the export: each change and each start of unknown earlier settings, in the order
    they happened; None for a store that recorded no settings, whose export has no such key.

    Raises ValueError for a record or an event that cannot be read, so the export names it and never guesses.
    """
    held = store.q("SELECT value FROM meta WHERE key=?", (META_KEY,))
    if not held:
        return None
    _recorded(held[0]["value"])
    out: dict[str, list[dict[str, Any]]] = {"changes": [], "unknown_before": []}
    for row in store.q(
        "SELECT ts, kind, detail FROM events WHERE kind IN (?,?) ORDER BY id", (CHANGED, UNKNOWN_BEFORE)
    ):
        if row["kind"] == UNKNOWN_BEFORE:
            out["unknown_before"].append({"at": row["ts"]})
            continue
        try:
            entries = json.loads(row["detail"])["changes"]
            out["changes"] += [{"at": row["ts"], **checked_entry(entry)} for entry in entries]
        except (TypeError, KeyError, ValueError):
            raise ValueError(f"an event {CHANGED} at {row['ts']} cannot be read") from None
    return out


def checked_entry(entry: object) -> dict[str, Any]:
    """An event's change: a key with an old value, a new value or both; ValueError for anything else."""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("key"), str) or not (entry.keys() & {"old", "new"}):
        raise ValueError("a change names its key and has an old or a new value")
    values = checked({k: v for k, v in entry.items() if k in ("old", "new")})
    return {"key": entry["key"], **values}
