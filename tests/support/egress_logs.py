"""Build egress collector logs and records for the tests of `expcore.egress`, in the collector's own format.

The format is deploy/hpc-federation/bundle/egress-collector.py's; tests/deploy/test_hpc_egress_shim.py checks that the
core reads a log the real collector wrote, so these stand-ins and the collector cannot drift apart unnoticed.
"""

import json
from types import SimpleNamespace
from typing import Any

NONCE = "00112233445566778899aabbccddeeff"
UID = 1000
GATE_PORT = 41234
CANARY = ("127.0.0.1", GATE_PORT)  # the tests' declarations name the gate as the canary's endpoint
CODE_TOOL = {
    "name": "python",
    "description": "Run Python code.",
    "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    "terminal": False,
}


def rec(op: str, address: str, port: int, *, kind: str = "tcp", pid: int = 42, fd: int = 3, result=None, dropped=0):
    """One record of the shim, as the collector writes it; an outcome's result defaults to 0."""
    if result is None and op in ("result", "async"):
        result = 0
    family = "inet6" if ":" in address else "inet"
    return {
        "op": op,
        "pid": pid,
        "fd": fd,
        "time": "1794700000.000000001",
        "type": kind,
        "family": family,
        "address": address,
        "port": port,
        "result": result,
        "dropped": dropped,
    }


def canary_records() -> list[dict[str, Any]]:
    """The launcher's canary: a connect to the gate, in the cell, before the agent ran, and its result."""
    return [rec("connect", *CANARY, pid=5, fd=3), rec("result", *CANARY, pid=5, fd=3)]


def lines(
    records,
    *,
    nonce: str = NONCE,
    end: bool = True,
    malformed: int = 0,
    unwritten: int = 0,
    uid: int = UID,
    canary: bool = True,
):
    """The collector's lines: start, the canary's two records (unless `canary` is false), one record line each,
    `malformed` malformed lines, end."""
    records = (canary_records() if canary else []) + list(records)
    out: list[dict[str, Any]] = [{"event": "start", "format": "hpcop-egress/1", "nonce": nonce, "pid": 7, "uid": uid}]
    out += [{"event": "record", "peer": {"pid": r["pid"], "uid": uid}, "rec": r} for r in records]
    out += [{"event": "malformed", "peer": {"pid": 9, "uid": uid}, "bytes": 5, "sha256": "0" * 64}] * malformed
    if end:
        out.append({"event": "end", "records": len(records), "malformed": malformed, "unwritten": unwritten})
    for seq, line in enumerate(out):
        line.update(seq=seq, wall=1794700000.0 + seq, mono=100.0 + seq)
    return out


def encode(out) -> bytes:
    return b"".join((json.dumps(line, sort_keys=True) + "\n").encode() for line in out)


def log(records, **options) -> bytes:
    """A whole log's bytes."""
    return encode(lines(records, **options))


def record(*, instruments=None, cells=(), run_key="run-1", attempt=1):
    """A stand-in for a RunRecord with what `judge` reads: the code tool, its calls and the instruments."""
    calls = [{"name": "python", "arguments": {"code": code}, "status": "ok"} for code in cells]
    return SimpleNamespace(
        run_key=run_key, attempt=attempt, tools=[CODE_TOOL], tool_calls=calls, instruments=dict(instruments or {})
    )
