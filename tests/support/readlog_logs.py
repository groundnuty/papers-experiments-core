"""Build read-log collector logs and client lines for the tests of `expcore_onedata.readlog`, in the collector's format.

The format is `expcore_onedata/readlog/collector.py`'s; tests/onedata/readlog/test_collector.py checks that the reader
reads a log the real collector wrote, so these stand-ins and the collector cannot drift apart unnoticed.
"""

import json
from typing import Any

NONCE = "00112233445566778899aabbccddeeff"
PEER_UID = 1000
COLLECTOR_UID = 1000
SHA = "ab" * 32


def read(item: str = "file-1", **fields: Any) -> dict[str, Any]:
    """One record of a client line (`op` read), as the collector parses and writes it."""
    return {"v": 1, "op": "read", "item": item, **fields}


def canary() -> dict[str, Any]:
    return {"v": 1, "op": "canary", "item": "canary"}


def lines(
    records,
    *,
    nonce: str = NONCE,
    end: bool = True,
    malformed: int = 0,
    unwritten: int = 0,
    cap_reached: bool = False,
    peer_uid: int = PEER_UID,
    sender_uid: int | None = None,
    start_peer_uid: int | None = None,
    with_canary: bool = True,
):
    """The collector's lines: start, the canary's record (unless `with_canary` is false), one record line each,
    `malformed` malformed lines, end."""
    records = ([canary()] if with_canary else []) + list(records)
    sender = peer_uid if sender_uid is None else sender_uid
    start = {
        "event": "start",
        "format": "expcore-readlog/1",
        "nonce": nonce,
        "pid": 7,
        "uid": COLLECTOR_UID,
        "peer_uid": peer_uid if start_peer_uid is None else start_peer_uid,
        "max_bytes": 1 << 20,
    }
    out: list[dict[str, Any]] = [start]
    out += [{"event": "record", "peer": {"pid": 42, "uid": sender}, "rec": r} for r in records]
    out += [
        {"event": "malformed", "peer": {"pid": 9, "uid": sender}, "bytes": 5, "sha256": "0" * 64, "why": "not_json"}
    ] * malformed
    if end:
        out.append(
            {
                "event": "end",
                "records": len(records),
                "malformed": malformed,
                "unwritten": unwritten,
                "cap_reached": cap_reached,
            }
        )
    for seq, line in enumerate(out):
        line.update(seq=seq, wall=1794700000.0 + seq, mono=100.0 + seq)
    return out


def encode(out) -> bytes:
    return b"".join((json.dumps(line, sort_keys=True) + "\n").encode() for line in out)


def log(records, **options) -> bytes:
    """A whole log's bytes."""
    return encode(lines(records, **options))
