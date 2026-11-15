# Provenance: new
"""How long the platform's check has been deferred: one record in the store, kept across restarts (C11).

A deferred check is a wait, not a failure (`expcore.campaign.healthgate`), but a wait needs a bound: a holder that
never lets the platform go would keep every run deferred for ever. The current deferral, from the first deferred
pre-run check after the last pass, is one record in the store's meta `health_deferral`:

    {"since_utc", "last_utc", "checks": deferred checks so far, "last_detail"}

or `null` when no check is deferred. A restart reads it back, so the deferral's age survives it. The gate ends it at
the first check that passes, and fails once its age is past `max_defer_s`. A record that cannot be read is told as an
event and counted again from the next deferral.
"""

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

from expcore.campaign.store import Store

DEFERRAL_META = "health_deferral"
OPERATOR_ACTION = "operator action needed"  # what a platform's detail says of a wait or a failure only a person can end
_CLAUSE_END = "; "  # a platform's detail leads with its reason, and the rest of its words follow after this


def parse_deferral(raw: str | None) -> dict[str, Any] | None:
    """The current deferral, None when there is none; raise ValueError when the value is not a deferral."""
    if not raw or raw == "null":
        return None
    record = json.loads(raw)
    if not isinstance(record, dict) or not isinstance(record.get("since_utc"), str):
        raise ValueError("not a deferral")
    seconds_between(record["since_utc"], record["since_utc"])  # a stamp that is not one is refused here
    return record


def operator_reason(detail: object) -> str | None:
    """The reason a platform's detail leads with when only an operator can end what it says (`OPERATOR_ACTION`): the
    clause before the first semicolon, None for a detail that does not lead with one. The core keeps the first 200
    characters of a detail, so a reason that leads is kept whole while the rest of the words may be cut."""
    if not isinstance(detail, str):
        return None
    clause = detail.split(_CLAUSE_END, 1)[0]
    return clause if OPERATOR_ACTION in clause else None


def seconds_between(earlier: str, later: str) -> float:
    """The seconds from one store stamp (ISO 8601, `Z` or an offset) to another."""
    start, end = dt.datetime.fromisoformat(earlier), dt.datetime.fromisoformat(later)
    return (end - start).total_seconds()


class Deferral:
    """The store's record of the current deferral, written at every deferred check."""

    def __init__(self, store: Store, clock: Callable[[], str]) -> None:
        self.store, self.clock = store, clock
        self.current: dict[str, Any] | None = None

    def load(self) -> None:
        """Read the record back at a start; one that cannot be read is an event, and the next deferral counts anew."""
        try:
            self.current = parse_deferral(self.store.get_meta(DEFERRAL_META))
        except (ValueError, TypeError):
            self.current = None
            self.store.event(self.clock(), "health_deferral_unreadable", None, f"{DEFERRAL_META} cannot be read")

    def note(self, detail: str) -> float:
        """Count one deferred check; return the deferral's age in seconds, from its first deferred check."""
        now = self.clock()
        record = self.current or {"since_utc": now, "checks": 0}
        record = {**record, "last_utc": now, "checks": int(record.get("checks", 0)) + 1, "last_detail": detail[:200]}
        self.current = record
        self.store.put_meta(DEFERRAL_META, json.dumps(record, sort_keys=True), now)
        return max(0.0, seconds_between(record["since_utc"], now))

    def end(self) -> None:
        """A check passed: no check is deferred any more."""
        if self.current is not None:
            self.current = None
            self.store.put_meta(DEFERRAL_META, "null", self.clock())


def deferral_alarms(record: dict[str, Any] | None) -> list[str]:
    """The status alarm for a check deferred now. One that leads with a reason only an operator can end
    (`operator_reason`) says so first, and does not say a run holds the platform."""
    if record is None:
        return []
    reason = operator_reason(record.get("last_detail"))
    if reason is not None:
        return [
            f"HEALTH CHECK DEFERRED since {record['since_utc']}: {reason} ({record.get('checks', 0)} deferred "
            f"check(s), last {record.get('last_utc')}: {str(record.get('last_detail'))[:160]}); no run holds the "
            "platform, and past max_defer_s the gate fails and blocks dispatch until an operator has mended it"
        ]
    return [
        f"HEALTH CHECK DEFERRED since {record['since_utc']}: runs wait for the platform, which a run holds on purpose "
        f"({record.get('checks', 0)} deferred check(s), last {record.get('last_utc')}: "
        f"{str(record.get('last_detail'))[:160]}); past max_defer_s the gate fails and blocks dispatch"
    ]
