# Provenance: new
"""The read-log instrument: what a run's canonical scripts logged as read (namespace `read_log`).

`ReadLog(source, nonce, expected_peer_uid)` is an `Instrument` a study's executor builds for each run that has the
channel. `source.read_log()` returns the bytes of the collector's log, read once and kept by the sandbox after the
collector stopped, or None when there is none; `nonce` is the value the launcher gave the run's collector, so a log of
another run is never read as this one's; `expected_peer_uid` the uid the backend's collector was told to accept. The
executor stops the collector after the agent's cell and before `finish_instruments`: a collector still running has
written no end line, and its log reads as unknown (`not_closed`).

The snapshot, under `record.instruments["read_log"]`:

    {"version": 1, "log": "intact" | "unknown", "unknown": [<reasons>], "canary": true,
     "counts": {"reads": 41, "malformed": 0, "unwritten": 0} | None, "records": 41,
     "side_sha256": "<SHA-256 of the log's bytes>" | None, "side_bytes": 8123 | None}

`counts` is exact when the log is intact and None when it is unknown: **unknown is never zero**, and a run whose log is
unknown reads as unknown reads, never as fewer. `records` is the number of reads the readable part shows, a lower bound
when the log is unknown. `side_sha256` names the log as the side file the study declares (`hash_field =
"instruments.read_log.side_sha256"`, `paths.SIDE_HASH_FIELD`): the executor puts the log through
`replay.side_file_placement.stage_harness_side_file`, which hashes the copy it stages, and the record must name that
hash. A log that is missing, or past the side-file cap, has no side file (None). The unknown reasons are
`readlog.reader`'s, `no_canary` among them.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from expcore_onedata.readlog.paths import NAMESPACE as NAMESPACE
from expcore_onedata.readlog.reader import Reading

VERSION = 1
INTACT, UNKNOWN = "intact", "unknown"


class ReadLogSource(Protocol):
    """Where the run's collector log is: its bytes (read once, after the collector stopped), or None."""

    def read_log(self) -> bytes | None: ...


def snapshot(reading: Reading, data: bytes | None) -> dict[str, Any]:
    """The instrument's snapshot of one log reading (see the module's text); `data` is the log's bytes, for the hash."""
    raise NotImplementedError


class ReadLog:
    """The `Instrument` of one run's read log: one per run, holding the source of its collector's log."""

    namespace = NAMESPACE

    def __init__(self, source: ReadLogSource, nonce: str, expected_peer_uid: int) -> None:
        raise NotImplementedError

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the collector's log begins before the agent's cell."""
        raise NotImplementedError

    def finish(self) -> dict[str, Any]:
        """The snapshot of the collector's log, read once, after the collector has stopped."""
        raise NotImplementedError
