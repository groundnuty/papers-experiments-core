# Provenance: new
"""The egress instrument: one run's outbound connections, by class, from the collector's log (namespace `egress`).

`EgressLog(source, declared, nonce)` is an `Instrument` a platform's executor builds for each run whose network it
cannot restrict (an HPC batch node). `source.egress_log()` returns the bytes of the collector's log, or None when there
is none; `declared` is the run's `Declaration`; `nonce` the value the launcher gave the run's collector, so a log of
another run is never read as this one's. The executor stops the collector after the agent's cell and before
`finish_instruments`, as it stops the data gate before the gate's count: a collector still running has written no end
line, and its log reads as unknown (`not_closed`).

The snapshot, under `record.instruments["egress"]`:

    {"version": 1, "log": "intact" | "unknown", "unknown": [<reasons>], "declared": {...}, "canary": true,
     "counts": {"gate": 3, "loopback": 1, "outside": 1} | None, "records": 12,
     "outside": [{"address": "<an address>", "port": 443, "family": "inet", "type": "tcp", "attempts": 1,
                  "results": {"115": 1}}],
     "outside_cut": false, "outside_complete": true}

**The canary.** The log's first record must be the launcher's canary connect to the declared endpoint
(`Declaration.canary`), made in the cell under the shim before the agent ran; without it the log is unknown
(`no_canary`), since a shim that never loaded leaves a log with a start, an end and nothing between, which would
otherwise read as a run that connected nowhere. `canary` says whether it was there; the canary's records are left out
of `records`, `counts` and `outside`, which hold the agent's own.

`counts` are the destinations asked for (connect, and a send to a new address) by class, exact when the log is intact
and None when it is unknown: unknown is never zero. `outside` lists each outside destination with its attempts and the
results the log gives for it (0, or the errno, by count; an `async` result without a peer is attached to its socket's
last connect). When the log is unknown it lists what the readable part shows, a lower bound, and `outside_complete` is
false; at most `MAX_OUTSIDE` destinations are listed, and `outside_cut` says when more were seen. The `unknown` reasons
are `expcore.egress.log`'s. **A missing or damaged log is `"log": "unknown"`, never intact with zero outside.**
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from expcore.egress.declare import CLASSES, OUTSIDE, Declaration, address_of
from expcore.egress.log import ATTEMPTS, OUTCOMES, Reading, read_log

NAMESPACE = "egress"
VERSION = 1
INTACT, UNKNOWN = "intact", "unknown"
MAX_OUTSIDE = 1000
NO_CANARY = "no_canary"  # a log that could be read does not open with the canary's connect: was the shim loaded?


class EgressSource(Protocol):
    """Where the run's collector log is: its bytes, or None when there is none."""

    def egress_log(self) -> bytes | None: ...


def _key(rec: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return (str(address_of(rec["address"])), rec["port"], rec["family"], rec["type"])


def _outcomes(records: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int, str, str], Counter[str]]:
    """The results each destination got: an outcome with an address is its own; one without, its socket's connect's."""
    results: dict[tuple[str, int, str, str], Counter[str]] = {}
    last: dict[tuple[int, int], tuple[str, int, str, str]] = {}
    for rec in records:
        if rec["op"] == "connect":
            last[(rec["pid"], rec["fd"])] = _key(rec)
        elif rec["op"] in ("result", "async"):
            key = _key(rec) if rec["address"] != "-" else last.get((rec["pid"], rec["fd"]))
            if key is not None:
                results.setdefault(key, Counter())[str(rec["result"])] += 1
    return results


def _canary(reading: Reading, declared: Declaration) -> tuple[bool, list[Mapping[str, Any]], list[str]]:
    """Whether the log opens with the canary's connect; the records after it; and the log's problems, `no_canary`
    added when a log that could be read does not open with it."""
    records, problems = list(reading.records), list(reading.problems)
    first = records[0] if records else None
    seen = first is not None and first["op"] == "connect" and declared.is_canary(first["address"], first["port"])
    if not seen:
        if reading.intact or records:
            problems.append(NO_CANARY)
        return False, records, problems
    socket, own = (first["pid"], first["fd"]), []
    for rec in records[1:]:  # the canary's outcomes go with it, up to its socket's next connect
        if socket is not None and (rec["pid"], rec["fd"]) == socket:
            if rec["op"] in OUTCOMES:
                continue
            socket = None
        own.append(rec)
    return True, own, problems


def snapshot(reading: Reading, declared: Declaration) -> dict[str, Any]:
    """The instrument's snapshot of one log reading under one declaration (see the module's text)."""
    canary, records, problems = _canary(reading, declared)
    attempts = [rec for rec in records if rec["op"] in ATTEMPTS]
    classes = [declared.classify(rec["address"], rec["port"]) for rec in attempts]
    seen: Counter[tuple[str, int, str, str]] = Counter(
        _key(rec) for rec, cls in zip(attempts, classes, strict=True) if cls == OUTSIDE
    )
    results = _outcomes(records)
    listed = sorted(seen)[:MAX_OUTSIDE]
    outside = [
        {
            "address": address,
            "port": port,
            "family": family,
            "type": kind,
            "attempts": seen[(address, port, family, kind)],
            "results": dict(sorted(results.get((address, port, family, kind), Counter()).items())),
        }
        for address, port, family, kind in listed
    ]
    intact = not problems
    return {
        "version": VERSION,
        "log": INTACT if intact else UNKNOWN,
        "unknown": problems,
        "declared": declared.to_json(),
        "canary": canary,
        "counts": {cls: classes.count(cls) for cls in CLASSES} if intact else None,
        "records": len(records),
        "outside": outside,
        "outside_cut": len(seen) > MAX_OUTSIDE,
        "outside_complete": intact and len(seen) <= MAX_OUTSIDE,
    }


class EgressLog:
    """The `Instrument` of one run's outbound connections: one per run, holding the source of its collector's log."""

    namespace = NAMESPACE

    def __init__(self, source: EgressSource, declared: Declaration, nonce: str) -> None:
        if not isinstance(declared, Declaration):
            raise TypeError("declared must be a Declaration")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("the run's collector nonce is a non-empty text")
        self._source, self._declared, self._nonce = source, declared, nonce

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the collector's log begins before the agent's cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the collector's log, read once, after the collector has stopped."""
        return snapshot(read_log(self._source.egress_log(), self._nonce), self._declared)
