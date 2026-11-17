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
     "counts": {"reads": 41, "canaries": 1} | None, "records": 41,
     "side_sha256": "<SHA-256 of the log's bytes>" | None, "side_bytes": 8123 | None}

`counts` is exact when the log is intact and None when it is unknown: **unknown is never zero**, and a run whose log is
unknown reads as unknown reads, never as fewer. `records` is the number of reads the readable part shows, a lower bound
when the log is unknown. `canary` says whether the log opens with the harness's canary. `side_sha256` names the log as
the side file the study declares (`hash_field = "instruments.read_log.side_sha256"`, `paths.SIDE_HASH_FIELD`): the
executor puts the log through `replay.side_file_placement.stage_harness_side_file`, which hashes the copy it stages,
and the record must name that hash. A log that is missing has no side file (None), and one past the side-file cap
cannot be one: it is unknown (`side_file_too_large`), with no hash. An unknown log is still the side file when it can
be, so that what was read before the damage can be looked at. The unknown reasons are `readlog.reader`'s, `no_canary`
among them.
"""

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol

from expcore.replay import side_files as side_module
from expcore_onedata.readlog.paths import NAMESPACE as NAMESPACE
from expcore_onedata.readlog.reader import Reading, read_log

VERSION = 1
INTACT, UNKNOWN = "intact", "unknown"
SIDE_TOO_LARGE = "side_file_too_large"  # the log is past MAX_SIDE_FILE_BYTES: the record could not name it as a file


class ReadLogSource(Protocol):
    """Where the run's collector log is: its bytes (read once, after the collector stopped), or None."""

    def read_log(self) -> bytes | None: ...


def snapshot(reading: Reading, data: bytes | None) -> dict[str, Any]:
    """The instrument's snapshot of one log reading (see the module's text); `data` is the log's bytes, for the hash."""
    problems = list(reading.problems)
    side_sha256 = side_bytes = None
    if data is not None:
        if len(data) > side_module.MAX_SIDE_FILE_BYTES:  # read where used, as the check and the analysis read it
            problems.append(SIDE_TOO_LARGE)
        else:
            side_sha256, side_bytes = hashlib.sha256(data).hexdigest(), len(data)
    problems = list(dict.fromkeys(problems))
    intact = not problems
    return {
        "version": VERSION,
        "log": INTACT if intact else UNKNOWN,
        "unknown": problems,
        "canary": bool(reading.records) and reading.records[0]["op"] == "canary",
        "counts": {"reads": len(reading.reads), "canaries": len(reading.canaries)} if intact else None,
        "records": len(reading.reads),
        "side_sha256": side_sha256,
        "side_bytes": side_bytes,
    }


class ReadLog:
    """The `Instrument` of one run's read log: one per run, holding the source of its collector's log."""

    namespace = NAMESPACE

    def __init__(self, source: ReadLogSource, nonce: str, expected_peer_uid: int) -> None:
        if not callable(getattr(source, "read_log", None)):
            raise TypeError("source must have a read_log() method that returns the collector's log, or None")
        if not isinstance(nonce, str):
            raise TypeError("the run's collector nonce is a text")
        if not nonce:
            raise ValueError("the run's collector nonce is a non-empty text")
        if isinstance(expected_peer_uid, bool) or not isinstance(expected_peer_uid, int):
            raise TypeError("expected_peer_uid is a whole number")
        if not 0 <= expected_peer_uid <= 2**32:
            raise ValueError("expected_peer_uid is a whole number of 0 to 2**32")
        self._source, self._nonce, self._uid = source, nonce, expected_peer_uid

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the collector's log begins before the agent's cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the collector's log, read once, after the collector has stopped."""
        data = self._source.read_log()
        if data is not None and not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError("read_log() must return bytes or None")
        data = None if data is None else bytes(data)
        return snapshot(read_log(data, self._nonce, expected_peer_uid=self._uid), data)
