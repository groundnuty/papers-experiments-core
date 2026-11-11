# Provenance: new
"""The platform's instrument of the bytes a run's agent read through its data path (O22).

`ServedBytes(sandbox)` is an `Instrument` (namespace `served_bytes`) a study's executor builds for each run, once
`SandboxPlatform.launch` has yielded the run's sandbox, and runs with the others: `start_instruments` before
`record_run`, then `sandbox.collect(...)`, then `finish_instruments`. `finish` records the sandbox's count
(`DockerSandbox.served_bytes`), so a scorer reads it at `record.instruments["served_bytes"]` and replay serves the
recorded value; nothing is measured again. Its snapshot:

  {"version": 1, "data_path": "rest", "listing": "complete", "run_start_marked": true, "served_bytes": 51234,
   "content_bytes": 40960,
   "listing_bytes": 10240, "other_bytes": 34, "gate_answer_bytes": 0,
   "files": {"<file id>": {"path": "/api/v3/oneprovider/data/<file id>/content", "requests": 3,
                           "ranges": [[0, 32768], [36864, 40960]], "range_bytes": 36864, "ranges_cut": false,
                           "body_bytes": 40960}},
   "files_total": 1, "files_cut": false,
   "listings": {"<directory id>": {"path": "/api/v3/oneprovider/data/<directory id>/children", "requests": 1,
                                   "body_bytes": 10240}},
   "listings_total": 1, "listings_cut": false,
   "caps": {"max_files": 1000, "max_directories": 1000, "max_ranges_per_file": 64},
   "before_run_start": {"requests": 2, "served_bytes": 340, "content_bytes": 0, "listing_bytes": 300,
                        "other_bytes": 40, "gate_answer_bytes": 0}, "unknown": []}

What each count holds is in `gatelog` (the bytes of the platform's answers the gate's socket accepted for the agent,
by what they were; the gate's own answers apart; and the same per file whose content it served and per directory it
listed, keyed by the id the request's path names, each range `[start, stop]` with `stop` excluded). A REST data path is
counted from the log of the gate the collection stopped, so `finish` before `collect` records every count unknown
(`gate_not_collected`); a mount's reads go through oneclient, which no gate sees, so its counts are unknown
(`mount_not_through_gate`); a run with no data path read nothing through one, zero, with empty tables. Every count and
every table is None whenever `unknown` names a reason, and unknown is never zero.

`caps` records how much the tables were allowed to list (`[sandbox.served_bytes]` of platform.toml). A table a cap cut
says so (`files_cut`, `listings_cut`, a row's `ranges_cut`), and then a file without a row may have been read: read a
snapshot through `lookup_file`, `lookup_bytes` and `lookup_directory`, which answer `served`, `not_served` or `unknown`
with the reasons, so a file a cap left out reads as unknown (`files_cut`), never as not served and never as 0 bytes.

`run_start_marked` says whether the executor marked where the agent's run began (`DockerSandbox.mark_run_start`, after
its own checks through the sandbox): with the mark, the counts, the tables and so every lookup hold only the answers
after it, and those before it are counted in `before_run_start`; without it every answer is the run's and
`before_run_start` is None (O21). A snapshot recorded before these two keys existed lacks them: not recorded.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

NAMESPACE = "served_bytes"
VERSION = 1
SERVED, NOT_SERVED, UNKNOWN = "served", "not_served", "unknown"  # what a lookup answers
RANGES_CUT, RANGES_UNKNOWN = "ranges_cut", "ranges_unknown"  # why a byte range of a file with a row is unknown


class CountsServed(Protocol):
    """A sandbox that counts the bytes its agent read through its data path (`DockerSandbox`)."""

    def served_bytes(self) -> dict[str, Any]: ...


class ServedBytes:
    """The `Instrument` of the bytes one run's agent read through its data path: one per run, holding its sandbox."""

    namespace = NAMESPACE

    def __init__(self, sandbox: CountsServed) -> None:
        self._sandbox = sandbox

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the gate's log begins with the sandbox."""

    def finish(self) -> dict[str, Any]:
        """The count, as the sandbox's collection left it."""
        return {"version": VERSION, **self._sandbox.served_bytes()}


@dataclass(frozen=True)
class Lookup:
    """What a snapshot says of one file, directory or byte range: `served`; `not_served`, known (the gate served none of
    it, or not every byte of a range); or `unknown`, with why in `reasons`. `row` is the file's or directory's row
    whenever it has one."""

    state: str
    reasons: tuple[str, ...] = ()
    row: Mapping[str, Any] | None = None


def _lookup(snapshot: Mapping[str, Any], table: str, key: str) -> Lookup:
    """A row of `files` or `listings`: served when it has one; without one, not served only when no cap cut the table,
    unknown (`files_cut`, `listings_cut`) when one did; unknown with the run's reasons when the run's count is."""
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("version", VERSION) != VERSION
        or not isinstance(snapshot.get("unknown"), list)
        or table not in snapshot
        or f"{table}_cut" not in snapshot
    ):
        raise ValueError(f"not a snapshot of the bytes served of version {VERSION}, with its tables ({NAMESPACE})")
    if snapshot["unknown"]:
        return Lookup(UNKNOWN, tuple(snapshot["unknown"]))
    rows, cut = snapshot[table], snapshot[f"{table}_cut"]
    if not isinstance(rows, Mapping) or not isinstance(cut, bool):
        raise ValueError(f"the snapshot's {table} table is not known, yet it names no reason")
    if key in rows:
        return Lookup(SERVED, (), rows[key])
    return Lookup(UNKNOWN, (f"{table}_cut",)) if cut else Lookup(NOT_SERVED)


def lookup_file(snapshot: Mapping[str, Any], file_id: str) -> Lookup:
    """Whether the gate served the file's content in the run whose snapshot this is (the instrument's, or the sandbox's
    `served_bytes()`). A file without a row was not served only when `files_cut` is false; once it is true, such a file
    is unknown (`files_cut`): a cap may have left it out, so it is never read as not served, nor as 0 bytes."""
    return _lookup(snapshot, "files", file_id)


def lookup_directory(snapshot: Mapping[str, Any], dir_id: str) -> Lookup:
    """Whether the gate served a listing of the directory, by the rule of `lookup_file` (`listings_cut`)."""
    return _lookup(snapshot, "listings", dir_id)


def lookup_bytes(snapshot: Mapping[str, Any], file_id: str, start: int, stop: int) -> Lookup:
    """Whether the gate served every byte of `[start, stop)` of the file, a claimed read. `served` when all lie in its
    ranges; `not_served` when one is known never served; `unknown` when the file is (`lookup_file`), when its ranges
    are (`ranges_unknown`), or when a byte lies past the end of the last range a row the cap cut lists (`ranges_cut`).
    A row keeps its lowest ranges, so every byte below that end is known, served or not."""
    if any(isinstance(n, bool) or not isinstance(n, int) for n in (start, stop)) or not 0 <= start < stop:
        raise ValueError("a byte range is two whole numbers, 0 <= start < stop")
    found = lookup_file(snapshot, file_id)
    if found.state != SERVED:
        return found
    row = found.row or {}
    ranges = row.get("ranges")
    if ranges is None:
        return Lookup(UNKNOWN, (RANGES_UNKNOWN,), row)
    known = ranges[-1][1] if row.get("ranges_cut") and ranges else math.inf  # the end of what the row can answer for
    below = min(stop, known)
    if start < known and not any(first <= start and below <= last for first, last in ranges):
        return Lookup(NOT_SERVED, (), row)  # ranges are merged, so bytes served together lie in one of them
    if stop > known:
        return Lookup(UNKNOWN, (RANGES_CUT,), row)
    return Lookup(SERVED, (), row)


__all__ = [
    "NAMESPACE",
    "NOT_SERVED",
    "RANGES_CUT",
    "RANGES_UNKNOWN",
    "SERVED",
    "UNKNOWN",
    "VERSION",
    "CountsServed",
    "Lookup",
    "ServedBytes",
    "lookup_bytes",
    "lookup_directory",
    "lookup_file",
]
