# Provenance: new
"""The outage check's reads through the mount, made inside a real sandbox in mount mode (O28, issue #108).

The agent of a mount condition reads through a oneclient sidecar's FUSE mount, read-only, from its own container. So
does this check: `outage_agent` starts the sandbox package's `DockerSandbox` with its mount sidecar, and each batch of
reads here is one cell run in the agent's container as the agent's user (`DockerSandbox.exec`, a harness step).

**The cell.** Each read or listing runs in a child process of the cell with its own deadline, the outage bound: a FUSE
read of a provider that is down can hang in the kernel, and only a process apart can be given up on. The cell records,
per read: the bytes and their SHA-256 (never the bytes), the seconds on the container's own clock, the error's errno
by name and number, its exception class (Python reports a mount's ETIMEDOUT as `TimeoutError`, an `OSError` that keeps
its errno), and whether it blocked: still running at its deadline, so it was killed (and whether the kill took).

**The request** is one JSON document on one line of the cell (`REQUEST = json.loads(...)`), so a test's fake host can
read what a cell asks without parsing Python. **The facts** (`read_facts`, `listing_facts`) have the outcome vocabulary
of `outage_cells`: `served`, `partial`, `differs`, `refused` (an errno other than a timeout's), `no answer` or
`blocked` (a timeout, by its seconds against the bound as a REST timeout is, the errno kept), `blocked` with `blocked:
true` (still running at its deadline, killed); a cell whose output cannot be read measured nothing, which `outage_run`
records as a gap, never as an outcome.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from expcore_onedata.day0.outage_cells import unanswered, word
from expcore_onedata.sandbox.api import ExecResult

KILL_WAIT_S = 10.0  # how long the cell waits for a killed child that is stuck in the kernel
CELL_MARGIN_S = 30.0  # what a cell's own limit adds to its reads' deadlines
ERRNO_NAME = re.compile(r"E[A-Z0-9]{1,15}|unknown")  # errno.errorcode's names, and the cell's word for none

CHILD = r"""
import errno, hashlib, json, os, sys
op, path = sys.argv[1], sys.argv[2]


def fail(error, done):
    print(json.dumps({"error": errno.errorcode.get(error.errno or 0, "unknown"), "errno": error.errno,
                      "kind": type(error).__name__, "bytes": done}), flush=True)
    sys.exit(3)


if op == "list":
    try:
        names = sorted(os.listdir(path))
    except OSError as error:
        fail(error, 0)
    print(json.dumps({"names": names}), flush=True)
    sys.exit(0)
digest, done = hashlib.sha256(), 0
try:
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            print(json.dumps({"progress": done}), flush=True)
    finally:
        os.close(fd)
except OSError as error:
    fail(error, done)
print(json.dumps({"bytes": done, "sha256": digest.hexdigest()}), flush=True)
"""

CELL = """
import json, subprocess, sys, time
REQUEST = json.loads({request!r})
CHILD = {child!r}
results = []
for item in REQUEST["ops"]:
    began = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-c", CHILD, item["op"], item["path"]],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    blocked, killed = False, None
    try:
        out, _ = proc.communicate(timeout=REQUEST["deadline_s"])
    except subprocess.TimeoutExpired:
        blocked = True
        proc.kill()
        try:
            out, _ = proc.communicate(timeout={kill_wait!r})
            killed = True
        except subprocess.TimeoutExpired:
            out, killed = "", False
    lines = []
    for line in out.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            pass
    row = {{"key": item["key"], "seconds": round(time.perf_counter() - began, 3), "blocked": blocked}}
    if blocked:
        row.update(killed=killed, bytes=max([l.get("progress", 0) for l in lines] or [0]))
    elif lines:
        row.update({{k: v for k, v in lines[-1].items() if k != "progress"}})
    else:
        row["unreadable"] = True
    results.append(row)
print(json.dumps({{"results": results}}))
"""


def cell(ops: Sequence[Mapping[str, str]], deadline_s: float) -> str:
    """The cell's source: each op (`key`, `op` of `read` or `list`, and the `path` inside the agent) in turn."""
    request = json.dumps({"deadline_s": deadline_s, "ops": [dict(op) for op in ops]})
    return CELL.format(request=request, child=CHILD, kill_wait=KILL_WAIT_S)


def cell_timeout_s(ops: int, deadline_s: float) -> float:
    """The cell's own limit: every op blocking to its deadline and its kill's wait, with a margin."""
    return ops * (deadline_s + KILL_WAIT_S + 2) + CELL_MARGIN_S


def results(done: ExecResult) -> dict[str, dict[str, Any]] | str:
    """Each op's row by its key, or why the cell's output says nothing (its exit status, whether it timed out)."""
    lines = done.stdout.strip().splitlines()
    try:
        rows = json.loads(lines[-1])["results"]
        return {row["key"]: row for row in rows}
    except (ValueError, IndexError, KeyError, TypeError):
        return f"the mount cell printed no result (exit {done.exit_code}, timed out: {done.timed_out})"


def _base(row: Mapping[str, Any], bound_s: float) -> dict[str, Any]:
    return {"seconds": row.get("seconds"), "bound_s": bound_s, "blocked": bool(row.get("blocked"))}


def _error(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row's error as it may be recorded: an errno's name and number and a class name, each of its shape (O12)."""
    number = row.get("errno")
    return {
        "errno": word(row.get("error"), ERRNO_NAME),
        "errno_number": number if isinstance(number, int) and not isinstance(number, bool) else None,
        "error_kind": word(row.get("kind")),
    }


def _timed_out(row: Mapping[str, Any]) -> bool:
    """Whether an error is a timeout: Python reports a mount's ETIMEDOUT as `TimeoutError`, keeping the errno."""
    return row.get("kind") == "TimeoutError" or row.get("error") == "ETIMEDOUT"


def _failed(row: Mapping[str, Any], bound_s: float, *, received: int = 0) -> str:
    """A failed op's outcome, by the rule of `outage_cells.failure_facts`: a timeout got no answer (`no answer`, or
    `blocked` by its seconds), any other errno is a refusal, and bytes before an error that did not block are
    `partial`."""
    outcome = unanswered(row.get("seconds") or 0.0, bound_s) if _timed_out(row) else "refused"
    return "partial" if received and outcome != "blocked" else outcome


def read_facts(row: Mapping[str, Any] | None, size: int, sha256: str, bound_s: float) -> dict[str, Any] | None:
    """A read's facts from its row; None when the row says nothing (the cell could not read its child)."""
    if row is None or row.get("unreadable"):
        return None
    facts = {**_base(row, bound_s), "bytes": row.get("bytes", 0), "expected_bytes": size}
    if row.get("blocked"):
        return {**facts, "outcome": "blocked", "killed": row.get("killed")}
    if "error" in row:
        return {**facts, **_error(row), "outcome": _failed(row, bound_s, received=facts["bytes"])}
    matches = row.get("sha256") == sha256 and row.get("bytes") == size
    return {**facts, "hash_matches": matches, "outcome": "served" if matches else "differs"}


def listing_facts(row: Mapping[str, Any] | None, expected: Sequence[str], bound_s: float) -> dict[str, Any] | None:
    """A listing's facts from its row, its names held against the names expected; None when the row says nothing."""
    if row is None or row.get("unreadable"):
        return None
    facts = {**_base(row, bound_s), "expected_entries": len(expected)}
    if row.get("blocked"):
        return {**facts, "outcome": "blocked", "killed": row.get("killed")}
    if "error" in row:
        return {**facts, **_error(row), "outcome": _failed(row, bound_s)}
    names = list(row.get("names") or [])
    missing = [name for name in expected if name not in names]
    unexpected = [name for name in names if name not in expected]
    outcome = "served" if not (missing or unexpected) else "partial" if not unexpected else "differs"
    return {**facts, "outcome": outcome, "entries": len(names), "missing": len(missing), "unexpected": len(unexpected)}


__all__ = ["cell", "cell_timeout_s", "listing_facts", "read_facts", "results"]
