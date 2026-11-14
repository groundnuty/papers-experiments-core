# Provenance: new
"""Reading the egress collector's log (deploy/hpc-federation/bundle/egress-collector.py): intact, or unknown and why.

The log is JSON lines, each numbered by the collector (`seq` 0, 1, 2, ...) with its clock readings (`wall`, `mono`):
a `start` line (`format` "hpcop-egress/1", the launcher's `nonce`, the collector's `uid`), then one `record` line per
record of the agent's shim (`rec`, the shim's fields; `peer`, the sender's pid and uid as the kernel attests them) or a
`malformed` line per datagram that was no record, then an `end` line with the counts the collector wrote.

`read_log` returns the records and the reasons the log is not intact. **A log is intact only when all of these hold:**
it is present and not past the size limit; every line is a JSON object of its kind; the first is the start line, with
this run's nonce; `seq` runs from 0 without a gap or a repeat and `mono` never goes back; the last line is the end
line and nothing follows it; its counts are the lines before it; no line is malformed, none was left unwritten, and
no record says its sender dropped one; and every record came from the collector's own uid. Otherwise it is unknown,
with every reason found, and the records read before the first damaged line are kept as a lower bound. **A missing
or damaged log is never read as one without outside connections.**
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.egress.declare import address_of

FORMAT = "hpcop-egress/1"
MAX_BYTES = 256 * 1024 * 1024  # a log past this is not read: unknown (`log_too_large`)
ATTEMPTS = ("connect", "sendto", "sendmsg", "sendmmsg")  # a destination asked for; the rest are outcomes
OUTCOMES = ("result", "async")
TYPES = ("tcp", "udp", "raw", "other")
FAMILIES = {"inet": 4, "inet6": 6}
REC_KEYS = {"op", "pid", "fd", "time", "type", "family", "address", "port", "result", "dropped"}

# Why a log is not intact; each is a reason in the snapshot's `unknown`.
MISSING = "log_missing"
EMPTY = "log_empty"
TOO_LARGE = "log_too_large"
UNPARSEABLE = "log_unparseable"
NO_START = "no_start_line"
NOT_THIS_RUN = "nonce_mismatch"
NOT_MONOTONIC = "not_monotonic"
NOT_CLOSED = "not_closed"
COUNTS_DIFFER = "end_counts_differ"
MALFORMED = "malformed_datagrams"
UNWRITTEN = "unwritten_datagrams"
DROPPED = "records_dropped"
FOREIGN = "foreign_sender"


@dataclass(frozen=True)
class Reading:
    """The records of a log, in order, and why it is not intact (empty when it is)."""

    records: tuple[Mapping[str, Any], ...]
    problems: tuple[str, ...]

    @property
    def intact(self) -> bool:
        return not self.problems


def _whole(value: Any, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _record(rec: Any) -> bool:
    """True when `rec` is a record as the collector parses it."""
    if not isinstance(rec, Mapping) or set(rec) != REC_KEYS:
        return False
    if rec["op"] not in ATTEMPTS + OUTCOMES or rec["type"] not in TYPES or rec["family"] not in FAMILIES:
        return False
    if not all(_whole(rec[k], 0, 2**63) for k in ("pid", "fd", "dropped")) or not _whole(rec["port"], 0, 65535):
        return False
    if not isinstance(rec["time"], str) or (rec["result"] is None) != (rec["op"] in ATTEMPTS):
        return False
    if rec["result"] is not None and not _whole(rec["result"], 0, 4095):
        return False
    if rec["address"] == "-":
        return rec["op"] == "async"
    try:
        return isinstance(rec["address"], str) and address_of(rec["address"]).version == FAMILIES[rec["family"]]
    except ValueError:
        return False


def _lines(data: bytes) -> list[Any] | None:
    """Every line decoded, or None at the first one that is not a JSON object."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n"):
        return None  # a cut last line
    lines = []
    for raw in text[:-1].split("\n"):
        try:
            line = json.loads(raw, parse_constant=lambda name: math.nan)
        except ValueError:
            return None
        if not isinstance(line, dict) or not isinstance(line.get("event"), str):
            return None
        lines.append(line)
    return lines


def _sequence(lines: list[dict[str, Any]]) -> bool:
    """`seq` 0, 1, 2, ... and `mono` never going back."""
    last = -math.inf
    for index, line in enumerate(lines):
        mono = line.get("mono")
        if not _whole(line.get("seq"), index, index) or not isinstance(mono, int | float) or not mono >= last:
            return False
        last = mono
    return True


def _start(line: dict[str, Any], nonce: str, problems: list[str]) -> int | None:
    """The collector's uid from a start line of this run, recording what is wrong with it."""
    if line.get("event") != "start" or line.get("format") != FORMAT or not _whole(line.get("uid"), 0, 2**32):
        problems.append(NO_START)
        return None
    if line.get("nonce") != nonce:
        problems.append(NOT_THIS_RUN)
    return line["uid"]


def _body(lines: list[dict[str, Any]], uid: int | None, problems: list[str]) -> list[Mapping[str, Any]]:
    """The records between the start and the end line, recording what is wrong with them."""
    records, malformed = [], 0
    for line in lines:
        event = line["event"]
        if event == "malformed":
            malformed += 1
            continue
        if event != "record" or not _record(line.get("rec")):
            problems.append(UNPARSEABLE)
            break
        peer = line.get("peer")
        if not isinstance(peer, Mapping) or peer.get("uid") != uid:
            problems.append(FOREIGN)
        records.append(line["rec"])
    if malformed:
        problems.append(MALFORMED)
    if any(r["dropped"] for r in records):
        problems.append(DROPPED)
    return records


def _end(line: dict[str, Any], lines: list[dict[str, Any]], problems: list[str]) -> None:
    """Check the end line against the lines before it."""
    counts = {e: sum(1 for x in lines if x["event"] == e) for e in ("record", "malformed")}
    if (line.get("records"), line.get("malformed")) != (counts["record"], counts["malformed"]):
        problems.append(COUNTS_DIFFER)
    if line.get("unwritten") != 0:
        problems.append(UNWRITTEN)


def read_log(data: bytes | None, nonce: str, *, max_bytes: int = MAX_BYTES) -> Reading:
    """The records of the collector's log and every reason it is not intact (see the module's text)."""
    if data is None:
        return Reading((), (MISSING,))
    if not data:
        return Reading((), (EMPTY,))
    if len(data) > max_bytes:
        return Reading((), (TOO_LARGE,))
    lines = _lines(data)
    if lines is None:
        return Reading((), (UNPARSEABLE,))
    problems: list[str] = []
    if not _sequence(lines):
        problems.append(NOT_MONOTONIC)
    uid = _start(lines[0], nonce, problems)
    ends = [i for i, line in enumerate(lines) if line["event"] == "end"]
    closed = ends == [len(lines) - 1] and len(lines) > 1
    if not closed:
        problems.append(NOT_CLOSED)
    body = lines[1:-1] if closed else lines[1:]
    records = _body([line for line in body if line["event"] != "end"], uid, problems)
    if closed:
        _end(lines[-1], body, problems)
    return Reading(tuple(records), tuple(dict.fromkeys(problems)))
