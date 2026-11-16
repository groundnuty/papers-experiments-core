# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/release.py, blob e4ff196acefcdef558343611ba476ab2396f5019
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Release files: someone's word that a stop may end (C13).

**A release names the stop it ends by the stop's serial.** Every stop (a model's stop or the all-stop on harness
errors, a budget hold) is given a serial in the transaction that records it (`next_serial`): `<count>.<random>`, such
as `3.k7f2qd9a`. The count is the store's, one more than the last it gave, for order and for reading. The random part
(eight base-32 characters, 40 bits from `secrets`) is there because a store can go back in time: a store copied back
from a snapshot (O62) gives the same count again to its next stop, and a line written for a stop the copy lost must not
end that one. A release file lists lines `<name> <serial>`, `m1 3.k7f2qd9a` or `ALL 3.k7f2qd9a`, and a line ends the
stop in force for its name only when it is that stop's exact line (`line_for`; runs of blanks count as one space). The
hold names the line (`told`).

**A file's modification time decides nothing.** It comes from the kernel's coarse clock (a write a millisecond after
the stop can carry a time before it), or from a file server's or another writer's clock, ahead or behind. A file
written before a stop could name that stop's serial only by guessing 40 random bits; a file written after it names it
whatever its time says. The time is kept for the record of who wrote the file and when (`_signed`), nothing more.

A line is used once: the lines that end a stop are removed from the file, the file goes with its last line, and a file
that cannot be rewritten ends nothing (the stop holds). A line whose name has no stop in force is left alone and says
nothing. A line whose name has a stop in force but that is not that stop's line (a bare name, the form before serials,
or another stop's serial) ends nothing and is returned as `Refused`, whose sentence gives the line that would end the
stop (`say_refused` says it once); it stays in the file, where it can never match, since no serial is given twice.

`take` and `written_after` are older forms, one stop per file and a standing decision. They still compare modification
times, so nothing in the core uses them.
"""

import base64
import datetime as dt
import logging
import os
import pwd
import re
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from expcore.campaign.store import Store

log = logging.getLogger(__name__)

SERIAL_KEY = "stop_serial"  # meta: the count of the last serial given
_SERIAL = re.compile(r"[0-9]+\.[a-z2-7]{8}")


def next_serial(store: Store, now: str) -> str:
    """Give a stop its serial, `<count>.<random>`: never one given before, in this store or in any copy of it (C13).

    Call it inside the transaction that records the stop, so that the count and the stop commit or roll back
    together. A count that cannot be read goes on past every event of the store: each serial was given with an event
    of its own, in its stop's transaction.
    """

    def go() -> str:
        text = store.get_meta(SERIAL_KEY)
        if text is None:
            last = 0
        elif text.isascii() and text.isdecimal():
            last = int(text)
        else:
            rows = store.q("SELECT seq FROM sqlite_sequence WHERE name='events'")
            last = int(rows[0]["seq"]) if rows else 0
        store.put_meta(SERIAL_KEY, str(last + 1), now)
        return f"{last + 1}.{base64.b32encode(secrets.token_bytes(5)).decode('ascii').lower()}"

    return store.transact(go)


def serial_of(stop: object) -> str | None:
    """Return the serial a recorded stop carries, or None when it has none that can be read (one recorded before
    serials, or a garbled one): such a stop is given a new serial before anything can end it."""
    serial = stop.get("serial") if isinstance(stop, Mapping) else None
    return serial if isinstance(serial, str) and _SERIAL.fullmatch(serial) else None


def numbered(store: Store, stops: Mapping[str, object], now: str) -> tuple[dict[str, dict], list[str]]:
    """Give each stop that has no serial (one recorded before serials) its serial, in the caller's transaction.

    Return the stops by name, each with its serial, and the names given one now, in order.
    """
    out: dict[str, dict] = {}
    given: list[str] = []
    for name, stop in sorted(stops.items()):
        record = dict(stop) if isinstance(stop, Mapping) else {}
        if serial_of(record) is None:
            record["serial"] = next_serial(store, now)
            given.append(name)
        out[name] = record
    return out, given


def line_for(name: str, serial: str) -> str:
    """Return the line of a release file that ends the stop on `name` with this serial."""
    return f"{name} {serial}"


def told(name: str, serial: str | None, where: str | None) -> str:
    """Return what a hold's sentence adds about its stop: its serial and, with a release file, the line that ends it."""
    if serial is None:
        return ""
    if where is None:
        return f" (stop {serial})"
    return f" (stop {serial}; to end it, write the line '{line_for(name, serial)}' in {where})"


@dataclass(frozen=True)
class Refused:
    """A line of a release file whose name has a stop in force, and that is not that stop's line: it ends nothing."""

    line: str
    name: str
    serial: str  # of the stop in force

    def sentence(self, what: str) -> str:
        """Say why the line ends nothing and which line would end the stop, `what` naming the kind of stop."""
        why = (
            "a release line names the stop it ends by its serial, as '<name> <serial>'"
            if self.line == self.name
            else "it is not the line of the stop in force"
        )
        return (
            f"the release line '{self.line}' ends nothing: {why}. The {what} on {self.name} now in force is stop "
            f"{self.serial}: the line '{line_for(self.name, self.serial)}' ends it"
        )


def _signed(st: os.stat_result) -> str:
    """Return who owns a file and when it was written, for the record of a release."""
    try:
        who = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        who = f"uid {st.st_uid}"
    return f"{who} at {dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def epoch_of(utc: object) -> float:
    """Return a stored time as epoch seconds, for display; one that cannot be read is 0."""
    try:
        return dt.datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def written_after(path: str | Path, since: float) -> str | None:
    """Return who wrote the file and when, if it exists and was written strictly after `since`; it stays where it is."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return _signed(st) if st.st_mtime > since else None


def take(path: str | Path, stopped_at: float) -> str | None:
    """Use up a file that ends one stop: its existence is the word.

    Return who and when (with its first line, if it says why) and remove it; None when there is no such file, it is
    not newer than the stop, or it cannot be removed.
    """
    p = Path(path)
    try:
        st = p.stat()
        if st.st_mtime <= stopped_at:
            return None
        said = _signed(st)
        first = next(iter(p.read_text(errors="replace").splitlines()), "").strip()
        p.unlink()
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("could not use up the release file %s: the stop holds", p)
        return None
    return f"{said}: {first[:200]}" if first else said


def take_listed(path: str | Path, stops: Mapping[str, str]) -> tuple[dict[str, str], list[Refused]]:
    """Use up the lines of a release file that end the stops in force.

    `stops` maps each name with a stop in force to that stop's serial. Return {name: who and when} for the stops whose
    line the file lists, with those lines removed from it (the file goes with its last line), and the lines it lists
    for a name in `stops` that are not that stop's line. Nothing is ended when the file cannot be rewritten.
    """
    p = Path(path)
    if not stops:
        return {}, []
    wanted = {line_for(name, serial): name for name, serial in stops.items()}
    refused: list[Refused] = []
    try:
        st = p.stat()
        lines = [" ".join(x.split()) for x in p.read_text(errors="replace").splitlines() if x.strip()]
        refused = [
            Refused(x, x.split()[0], stops[x.split()[0]])
            for x in dict.fromkeys(lines)
            if x not in wanted and x.split()[0] in stops
        ]
        taken = {wanted[x] for x in lines if x in wanted}
        if not taken:
            return {}, refused
        left = [x for x in lines if x not in wanted]
        if left:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text("\n".join(left) + "\n")
            os.utime(tmp, (st.st_atime, st.st_mtime))  # the lines left keep the time they were written, for the record
            os.replace(tmp, p)
        else:
            p.unlink()
    except FileNotFoundError:
        return {}, []
    except OSError:
        log.exception("could not use up the release file %s: the stops hold", p)
        return {}, refused
    return dict.fromkeys(taken, _signed(st)), refused


def say_refused(
    store: Store, path: str | Path, refused: Collection[Refused], said: set[Refused], kind: str, what: str, now: str
) -> None:
    """Say each refused line once per process: a warning naming the file, and an event without its path (the store
    can be published). `said` holds the lines said already; a line is said again when the stop in force changes."""
    new = [r for r in refused if r not in said]
    if not new:
        return
    for r in new:
        log.warning("%s: %s", path, r.sentence(what))
    store.put_meta_many({}, now, [(kind, None if r.name == "ALL" else r.name, r.sentence(what)) for r in new])
    said.update(new)
