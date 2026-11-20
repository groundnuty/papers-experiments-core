# Provenance: new
"""Whether a run is flagged for egress, from its record alone: the instrument's snapshot and the scan of its code.

`judge(record)` reads `record.instruments["egress"]` and scans the record's code cells. A run is flagged for each of:

- `outside`: its log is intact and lists an outside destination;
- `log_unknown`: there is no snapshot (the instrument was not run), the instrument failed (`{"ok": false, ...}`), the
  snapshot is not one this code reads, or the snapshot says the log is unknown; a missing or damaged log is never
  read as a run without outside connections;
- `static_scan`: the scan of its code cells has a hit that flags (`expcore.egress.scan`).

The scan uses the run's recorded declaration (its platform names, and whether its data path is the agent's own network
calls); a run without a readable snapshot is scanned with an empty declaration, so every URL and every network call in
its code flags. A flagged run is listed in the report, and the sensitivity analysis leaves it out.

Under the package indexes the campaign declared (`judge(..., package_indexes=...)`, read from the campaign's recorded
fingerprint), a plain `pip install` and a URL on an index's host are `package_index` hits: `package_index` says the
run has one, and it is never a flag, so neither rerun leaves the run out for it (O95).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from expcore.egress.declare import CLASSES, Declaration
from expcore.egress.instrument import INTACT, NAMESPACE, UNKNOWN, VERSION
from expcore.egress.package_index import PACKAGE_INDEX
from expcore.egress.scan import code_cells, scan_cells

OUTSIDE_FLAG, LOG_UNKNOWN, STATIC_SCAN = "outside", "log_unknown", "static_scan"
FLAGS = (OUTSIDE_FLAG, LOG_UNKNOWN, STATIC_SCAN)
NOT_RECORDED, INSTRUMENT_FAILED, NOT_A_SNAPSHOT = "not_recorded", "instrument_failed", "not_a_snapshot"
SNAPSHOT_KEYS = {"version", "log", "unknown", "declared", "canary", "counts", "records", "outside", "outside_cut"}
SNAPSHOT_KEYS |= {"outside_complete"}
# What a run without a readable snapshot is scanned with: no platform name, no gate; only loopback is not a hit.
NOTHING_DECLARED = Declaration((), ("127.0.0.1", 1))


@dataclass(frozen=True)
class RunEgress:
    """One run's egress verdict: the log's state and why it is unknown, the outside destinations it lists, the scan's
    hits, and the flags (empty for a run that is not flagged)."""

    run_key: str
    attempt: int
    condition: str
    log: str
    unknown: tuple[str, ...]
    outside: tuple[Mapping[str, Any], ...]
    scan: Mapping[str, Any]
    flags: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def package_index(self) -> bool:
        """Whether its code installed from, or named, a declared package index: reported, never a flag (O95)."""
        return bool(self.scan.get(PACKAGE_INDEX))


def _readable(data: Any) -> tuple[Mapping[str, Any] | None, Declaration | None, str | None]:
    """The snapshot and its declaration when this code reads it; else why not."""
    if data is None:
        return None, None, NOT_RECORDED
    if isinstance(data, Mapping) and data.get("ok") is False:
        return None, None, INSTRUMENT_FAILED
    if not isinstance(data, Mapping):
        return None, None, NOT_A_SNAPSHOT
    if set(data) != SNAPSHOT_KEYS or data["version"] != VERSION or data["log"] not in (INTACT, UNKNOWN):
        return None, None, NOT_A_SNAPSHOT
    try:
        declared = Declaration.from_json(data["declared"])
    except (TypeError, ValueError):
        return None, None, NOT_A_SNAPSHOT
    counts, outside, unknown = data["counts"], data["outside"], data["unknown"]
    if not isinstance(outside, list) or not isinstance(unknown, list) or (data["log"] == INTACT) == bool(unknown):
        return None, None, NOT_A_SNAPSHOT
    listed = sum_attempts(outside)
    if listed is None or not isinstance(data["outside_cut"], bool):
        return None, None, NOT_A_SNAPSHOT
    intact, canary, records, complete = data["log"] == INTACT, data["canary"], data["records"], data["outside_complete"]
    if not isinstance(canary, bool) or not isinstance(complete, bool) or not _whole(records):
        return None, None, NOT_A_SNAPSHOT
    # An intact log opened with the canary (instrument.py: no canary is a reason), and its outside list is complete
    # unless it was cut; an unknown log's list is a lower bound, never complete.
    if (intact and not canary) or complete != (intact and not data["outside_cut"]):
        return None, None, NOT_A_SNAPSHOT
    if intact and not (
        isinstance(counts, Mapping)
        and set(counts) == set(CLASSES)
        and all(_whole(n) for n in counts.values())
        and (counts["outside"] >= listed if data["outside_cut"] else counts["outside"] == listed)
        and records >= sum(counts.values())  # each counted destination is a record of its own
    ):
        return None, None, NOT_A_SNAPSHOT
    return data, declared, None


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def sum_attempts(outside: Sequence[Any]) -> int | None:
    """The attempts an outside list holds, None when an entry has no whole positive count."""
    counts = [e.get("attempts") if isinstance(e, Mapping) else None for e in outside]
    if not all(isinstance(c, int) and not isinstance(c, bool) and c > 0 for c in counts):
        return None
    return sum(counts)


def judge(record: Any, condition: str, *, package_indexes: Sequence[str] = ()) -> RunEgress:
    """The egress verdict of one analysed run (see the module's text), under the package indexes the campaign
    declared (`expcore.egress.package_index`; none by default)."""
    data, declared, why = _readable(record.instruments.get(NAMESPACE))
    if data is None:
        log, unknown, outside = UNKNOWN, (why,), ()
    else:
        log, unknown, outside = data["log"], tuple(data["unknown"]), tuple(data["outside"])
    scanned = scan_cells(code_cells(record), declared or NOTHING_DECLARED, package_indexes=package_indexes)
    found = set()
    if log != INTACT:
        found.add(LOG_UNKNOWN)
    if outside or (data is not None and log == INTACT and data["counts"]["outside"]):
        found.add(OUTSIDE_FLAG)  # a damaged log that still shows one says both
    if scanned["flagged"]:
        found.add(STATIC_SCAN)
    flags = tuple(flag for flag in FLAGS if flag in found)
    return RunEgress(record.run_key, record.attempt, condition, log, unknown, outside, scanned, flags)
