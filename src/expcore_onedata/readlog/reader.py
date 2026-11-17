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
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class Reading:
    """The records of a log, in order (the canary's included), and why it is not intact (empty when it is)."""

    records: tuple[Mapping[str, Any], ...]
    problems: tuple[str, ...]

    @property
    def intact(self) -> bool:
        raise NotImplementedError

    @property
    def reads(self) -> tuple[Mapping[str, Any], ...]:
        """The records of `op` "read": what the scripts logged, a lower bound when the log is not intact."""
        raise NotImplementedError


def read_log(data: bytes | None, nonce: str, *, expected_peer_uid: int, max_bytes: int | None = None) -> Reading:
    """The records of the collector's log and every reason it is not intact (see the module's text).

    `nonce` is the value the launcher gave this run's collector, so a log of another run is never read as this one's;
    `expected_peer_uid` the uid the harness told the collector to expect (the backend's: the driver's on Apptainer, the
    declared agent uid on Docker); `max_bytes` the size past which a log is not read (default `paths.MAX_LOG_BYTES`
    plus the end line's room)."""
    raise NotImplementedError
