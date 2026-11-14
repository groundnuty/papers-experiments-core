# Provenance: new
"""The record of who held the federation shared outside a campaign (O48).

A campaign attempt holds the federation's lock shared and the fault injector holds it exclusively
(`expcore_onedata.admission`); a tool outside a campaign (a study's smoke driver, a warm-up, `verify`) holds it shared
through `expcore_onedata.tool_admission`, and writes here who it is. The record is a file of its own beside the fault
journal, `shared-holds.jsonl`, written through the journal's durable append, under its journal-wide lock: the
exclusive admission reads the journal's directory, and the journal itself keeps to injection events, so that a line of
another kind never makes it unreadable.

Two lines per hold: `shared_held`, written once the tool holds the lock and the journals allow it, with the `reason`
(the tool's own text), the writer (`holder`: its pid and boot id) and its `owner` (`liveness.owner`: the process as
the lease pool names a holder, `boot id:pid:start time`, so that a reused pid is not mistaken for it) and the start
(`since_utc`); and `shared_released`, written when the lock is let go (`until_utc`, and `how`: `released`). A hold with
no `shared_released` line is open.

The lock is the hold, the record only says who: the kernel drops a dead tool's lock with its process, so an outage run
is never kept out by one, and the record of it is ended later, as `holder_gone`:
- by the next tool's hold, for a record whose process is gone (`settle`);
- by an exclusive admission, for every open record, whatever its process says: having the lock exclusively proves that
  no tool holds it (`settle(proven=True)`), as a shared admission clears the exclusive holder's record.

A reader that is neither tells a live holder from a gone one by the process (`open_holds`: `live`). An incomplete final
line is ignored, as the injection journal's is; any other line that is not a hold event makes the record unreadable
(`ValueError`).
"""

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign import liveness
from expcore.campaign.lease import process_gone
from expcore_onedata.fault_journal import InjectionJournal, holder

HOLDS = "shared-holds.jsonl"  # beside the fault journal's injections.jsonl
HELD, RELEASED = "shared_held", "shared_released"
RELEASED_BY_ITS_TOOL, HOLDER_GONE = "released", "holder_gone"
_STAMP = re.compile(r"[0-9T:+.-]{10,40}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


_FIELDS = ("id", "reason", "holder", "owner", "since_utc")


def _is_end(line: Mapping[str, Any]) -> bool:
    """Whether a `shared_released` line says when and how the hold ended."""
    return isinstance(line.get("until_utc"), str) and isinstance(line.get("how"), str)


def _is_hold(line: Mapping[str, Any]) -> bool:
    """Whether a `shared_held` line has what a reader needs: a reason, a start and an owner that names a process."""
    owner = line.get("owner")
    return (
        isinstance(line.get("reason"), str)
        and isinstance(line.get("since_utc"), str)
        and isinstance(owner, Mapping)
        and isinstance(owner.get("process"), str)
        and "holder" in line
    )


class SharedHolds:
    """The record of the tools' shared holds in one fault journal's directory (see the module docstring)."""

    def __init__(self, directory: Path) -> None:
        self._journal = InjectionJournal(Path(directory))

    def begin(self, reason: str) -> str:
        """Record that this context's tool holds the federation shared, durably; return the hold's id."""
        token = uuid.uuid4().hex
        self._journal.append_to(
            HOLDS,
            {
                "event": HELD,
                "id": token,
                "reason": reason,
                "holder": holder(),
                "owner": liveness.owner(),
                "since_utc": _now(),
            },
        )
        return token

    def end(self, token: str, how: str = RELEASED_BY_ITS_TOOL) -> None:
        """Record that the hold `token` named is over, durably."""
        self._journal.append_to(HOLDS, {"event": RELEASED, "id": token, "until_utc": _now(), "how": how})

    def holds(self) -> list[dict[str, Any]]:
        """Every hold in the order it began: `id`, `reason`, `holder`, `owner`, `since_utc`, and `until_utc` and `how`
        (None while it is open). `ValueError` for a complete line that is not a hold event; `OSError` for a record
        that cannot be read."""
        records: dict[str, dict[str, Any]] = {}
        for number, text in enumerate(self._journal.complete_text(HOLDS).splitlines(), 1):
            where = f"line {number} of the record of the tools' holds"
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                raise ValueError(f"{where} is not JSON") from None
            key = line.get("id") if isinstance(line, dict) else None
            if not isinstance(key, str) or not isinstance(line.get("event"), str):
                raise ValueError(f"{where} is not a hold event")
            if line["event"] == HELD and _is_hold(line):
                records[key] = {**{field: line[field] for field in _FIELDS}, "until_utc": None, "how": None}
            elif line["event"] == RELEASED and key in records and _is_end(line):
                if records[key]["until_utc"] is None:  # the first end stands: a settler may have raced the tool's own
                    records[key].update(until_utc=line["until_utc"], how=line["how"])
            else:
                raise ValueError(f"{where} is not a hold event")
        return list(records.values())

    def open_holds(self) -> list[dict[str, Any]]:
        """The holds with no end, each with `live`: True when its process runs, False when it is gone, None when that
        cannot be told."""
        opened = []
        for hold in self.holds():
            if hold["until_utc"] is None:
                gone = process_gone(str(hold["owner"].get("process")))
                opened.append({**hold, "live": None if gone is None else not gone})
        return opened

    def settle(self, *, proven: bool = False) -> int:
        """End, as `holder_gone`, every open hold whose process is gone, or every open hold when `proven`: the caller
        holds the federation's lock exclusively, so no tool holds it. Return how many were ended."""
        ended = 0
        for hold in self.open_holds():
            if proven or hold["live"] is False:
                self.end(hold["id"], HOLDER_GONE)
                ended += 1
        return ended

    def waiting_on(self) -> str:
        """For a wait for the lock that ran out: how many tools hold it shared whose process is not shown gone, and
        since when the longest, in words; empty when none. Never the tool's reason, which is its own text."""
        live = [hold for hold in self.open_holds() if hold["live"] is not False]
        if not live:
            return ""
        since = min(str(hold["since_utc"]) for hold in live)
        at = f" since {since}" if _STAMP.fullmatch(since) else ""
        if len(live) == 1:
            return f"1 tool outside a campaign holds it shared{at}"
        return f"{len(live)} tools outside a campaign hold it shared{at}"
