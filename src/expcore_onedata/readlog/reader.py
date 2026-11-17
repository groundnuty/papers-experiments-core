# Provenance: new
"""Reading the read-log collector's log: intact, or unknown and why (a sibling of `expcore.egress.log`).

The log is JSON lines, each numbered by the collector (`seq` 0, 1, 2, ... with no gap) with its clock readings (`wall`,
`mono`, the collector's own receive time): a `start` line (`format` "expcore-readlog/1", the launcher's `nonce`, the
collector's `uid`, the `peer_uid` it was told to expect), then one `record` line per client line (`rec`, the client's
fields; `peer`, the sender's pid and uid as the kernel attests them) or a `malformed` line per datagram that was no
record (its length and SHA-256 only), then an `end` line with the counts the collector wrote.

`read_log` returns the records and the reasons the log is not intact. **A log is intact only when all of these hold:**
it is present and not past the size limit; every line is a JSON object of its kind; the first is the start line, with
this run's nonce and the expected peer uid; `seq` runs from 0 without a gap or a repeat and `mono` never goes back;
the last line is the end line and nothing follows it; its counts are the lines before it; no line is malformed, no
datagram was left unwritten and the cap was not reached; the first record is the harness's canary; and every record
came from the expected peer uid. Otherwise it is UNKNOWN, with every reason found, and the records read before the
first damaged line are kept as a lower bound. **A run with an unknown log reads as UNKNOWN reads, never as fewer
reads, and a missing or damaged log is never read as a run that read nothing.**

The peer's pid is recorded and never judged: across PID namespaces (a Docker agent's) the kernel reports 0.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore_onedata.readlog import paths

# Why a log is not intact; each is a reason in the snapshot's `unknown`.
MISSING = "log_missing"
EMPTY = "log_empty"
TOO_LARGE = "log_too_large"
UNPARSEABLE = "log_unparseable"
NO_START = "no_start_line"
NOT_THIS_RUN = "nonce_mismatch"
WRONG_PEER_RULE = "peer_uid_mismatch"  # the start line names another expected uid than the harness's
NOT_MONOTONIC = "not_monotonic"
NOT_CLOSED = "not_closed"
COUNTS_DIFFER = "end_counts_differ"
MALFORMED = "malformed_datagrams"
UNWRITTEN = "unwritten_datagrams"
CAP_REACHED = "cap_reached"
FOREIGN = "foreign_sender"
BAD_RECORD = "bad_record"
NO_CANARY = "no_canary"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUIRED = frozenset({"v", "op", "item"})
_ALLOWED = _REQUIRED | {"range", "bytes", "sha256"}
_BIG = 2**63


@dataclass(frozen=True)
class Reading:
    """The records of a log, in order (the canary's included), and why it is not intact (empty when it is).

    `entries` are the record lines whole (`seq`, `wall`, `mono`, `peer`, `rec`), for a reader that needs the collector's
    receive time of each record."""

    records: tuple[Mapping[str, Any], ...]
    problems: tuple[str, ...]
    entries: tuple[Mapping[str, Any], ...] = ()

    @property
    def intact(self) -> bool:
        return not self.problems

    @property
    def reads(self) -> tuple[Mapping[str, Any], ...]:
        """The records of `op` "read": what the scripts logged, a lower bound when the log is not intact."""
        return tuple(rec for rec in self.records if rec["op"] == "read")

    @property
    def canaries(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(rec for rec in self.records if rec["op"] == "canary")


def _whole(value: Any, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _record(rec: Any) -> bool:
    """True when `rec` is a client line as the collector parses it."""
    if not isinstance(rec, Mapping) or not _REQUIRED <= set(rec) <= _ALLOWED:
        return False
    item = rec["item"]
    if not _whole(rec["v"], paths.LINE_VERSION, paths.LINE_VERSION) or rec["op"] not in paths.OPS:
        return False
    if not isinstance(item, str) or not 1 <= len(item) <= paths.MAX_ITEM_CHARS:
        return False
    if "range" in rec:
        span = rec["range"]
        if not isinstance(span, list) or len(span) != 2 or not all(_whole(x, 0, _BIG) for x in span):
            return False
        if span[0] > span[1]:
            return False
    if "bytes" in rec and not _whole(rec["bytes"], 0, _BIG):
        return False
    return "sha256" not in rec or (isinstance(rec["sha256"], str) and _SHA256.fullmatch(rec["sha256"]) is not None)


def _lines(data: bytes) -> list[dict[str, Any]] | None:
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
        except (ValueError, RecursionError):
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
        if not _whole(line.get("seq"), index, index) or isinstance(mono, bool) or not isinstance(mono, int | float):
            return False
        if not mono >= last:
            return False
        last = mono
    return True


def _start(line: dict[str, Any], nonce: str, expected_peer_uid: int, problems: list[str]) -> None:
    """Check the start line: this run's, from a collector told to expect the harness's peer uid."""
    if line.get("event") != "start" or line.get("format") != paths.FORMAT or not _whole(line.get("uid"), 0, 2**32):
        problems.append(NO_START)
        return
    if line.get("nonce") != nonce:
        problems.append(NOT_THIS_RUN)
    if line.get("peer_uid") != expected_peer_uid or isinstance(line.get("peer_uid"), bool):
        problems.append(WRONG_PEER_RULE)


def _body(
    lines: list[dict[str, Any]], expected_peer_uid: int, problems: list[str]
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """The record lines between the start and the end line, recording what is wrong with them."""
    records, entries, malformed = [], [], 0
    for line in lines:
        event = line["event"]
        if event == "malformed":
            malformed += 1
            continue
        if event != "record" or not _record(line.get("rec")):
            problems.append(BAD_RECORD)
            break
        peer = line.get("peer")
        if not isinstance(peer, Mapping) or peer.get("uid") != expected_peer_uid or isinstance(peer.get("uid"), bool):
            problems.append(FOREIGN)
        records.append(line["rec"])
        entries.append(line)
    if malformed:
        problems.append(MALFORMED)
    return records, entries


def _end(line: dict[str, Any], lines: list[dict[str, Any]], problems: list[str]) -> None:
    """Check the end line against the lines before it."""
    counts = {e: sum(1 for x in lines if x["event"] == e) for e in ("record", "malformed")}
    if (line.get("records"), line.get("malformed")) != (counts["record"], counts["malformed"]):
        problems.append(COUNTS_DIFFER)
    if line.get("unwritten") != 0:
        problems.append(UNWRITTEN)
    if line.get("cap_reached") is not False:
        problems.append(CAP_REACHED)


def read_log(data: bytes | None, nonce: str, *, expected_peer_uid: int, max_bytes: int | None = None) -> Reading:
    """The records of the collector's log and every reason it is not intact (see the module's text).

    `nonce` is the value the launcher gave this run's collector, so a log of another run is never read as this one's;
    `expected_peer_uid` the uid the harness told the collector to expect (the backend's: the driver's on Apptainer, the
    declared agent uid on Docker); `max_bytes` the size past which a log is not read (default `paths.MAX_LOG_BYTES`)."""
    if not _whole(expected_peer_uid, 0, 2**32):
        raise ValueError("expected_peer_uid is a whole number of 0 to 2**32")
    limit = paths.MAX_LOG_BYTES if max_bytes is None else max_bytes
    if data is None:
        return Reading((), (MISSING,))
    if not data:
        return Reading((), (EMPTY,))
    if len(data) > limit:
        return Reading((), (TOO_LARGE,))
    lines = _lines(data)
    if lines is None:
        return Reading((), (UNPARSEABLE,))
    problems: list[str] = []
    if not _sequence(lines):
        problems.append(NOT_MONOTONIC)
    _start(lines[0], nonce, expected_peer_uid, problems)
    ends = [i for i, line in enumerate(lines) if line["event"] == "end"]
    closed = ends == [len(lines) - 1] and len(lines) > 1
    if not closed:
        problems.append(NOT_CLOSED)
    body = lines[1:-1] if closed else lines[1:]
    records, entries = _body([line for line in body if line["event"] != "end"], expected_peer_uid, problems)
    if closed:
        _end(lines[-1], body, problems)
    if not records or records[0]["op"] != "canary":
        problems.append(NO_CANARY)
    return Reading(tuple(records), tuple(dict.fromkeys(problems)), tuple(entries))
