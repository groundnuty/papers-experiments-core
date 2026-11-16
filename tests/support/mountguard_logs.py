"""Build mount-watch logs and records for the tests of `expcore.mountguard`, in the watcher's own format.

The format is `expcore_onedata.sandbox.mountwatch`'s; tests/onedata/sandbox/test_mountwatch.py checks that the core
reads a log the real watcher wrote, so these stand-ins and the watcher cannot drift apart unnoticed.

A run's log, as the host mount writes it: the start line; the canary, the host's own read of the token file (into
oneclient's environment) and its removal once the mount is up, seen by both watches; oneclient's registration; `armed`
(the agent's window opens, before its cell starts); the proofs at the cell's start and end; `disarmed` (the cell has
stopped); the end line.
"""

import json
from types import SimpleNamespace
from typing import Any

NONCE = "00112233445566778899aabbccddeeff"
UID = 1000
CELL = {"pid": 4242, "start_time": 777, "nspid_depth": 2}


def inotify(target: str, *mask: str, entry: str = "self") -> dict[str, Any]:
    """One inotify line: `target` is the watch (`dir` or `token`), `entry` what the event names."""
    return {"event": "inotify", "target": target, "mask": list(mask), "entry": entry}


def canary() -> list[dict[str, Any]]:
    """The host's read of the token file into oneclient's environment and its removal, as both watches see them."""
    return [
        inotify("token", "IN_OPEN"),
        inotify("dir", "IN_OPEN", entry="token"),
        inotify("token", "IN_ACCESS"),
        inotify("dir", "IN_ACCESS", entry="token"),
        inotify("token", "IN_CLOSE_NOWRITE"),
        inotify("dir", "IN_CLOSE_NOWRITE", entry="token"),
        inotify("token", "IN_ATTRIB"),
        inotify("token", "IN_DELETE_SELF"),
        inotify("token", "IN_IGNORED"),
        inotify("dir", "IN_DELETE", entry="token"),
    ]


def proof(label: str, *, ok: bool = True, problems=(), cell=None) -> dict[str, Any]:
    return {"event": "proof", "label": label, "ok": ok, "problems": list(problems), "cell": dict(cell or CELL)}


def lines(
    window=(),
    *,
    nonce: str = NONCE,
    uid: int = UID,
    setup=None,
    proofs=None,
    armed: bool = True,
    disarmed: bool = True,
    end: bool = True,
    after=(),
    unwritten: int = 0,
    ptrace_scope: int | None = 1,
    watches=None,
    oneclient=None,
) -> list[dict[str, Any]]:
    """A log's lines: start, the setup (the canary by default), oneclient's registration, `armed`, the start proof,
    `window` (the lines while the agent's cell runs), the end proof, `disarmed`, `after`, the end line."""
    out: list[dict[str, Any]] = [
        {
            "event": "start",
            "format": "expcore-mountwatch/1",
            "nonce": nonce,
            "pid": 7,
            "uid": uid,
            "nspid_depth": 1,
            "ptrace_scope": ptrace_scope,
            "watches": {"dir": True, "token": True} if watches is None else watches,
        }
    ]
    out += canary() if setup is None else list(setup)
    out.append(oneclient or {"event": "oneclient", "pid": 99, "seen": True, "nspid_depth": 1})
    if armed:
        out.append({"event": "armed"})
    start, finish = proofs if proofs is not None else ([proof("start")], [proof("end")])
    out += [*start, *window, *finish]
    if disarmed:
        out.append({"event": "disarmed"})
    out += list(after)
    if end:
        out.append({"event": "end", "lines": len(out), "samples": 12, "unwritten": unwritten})
    for seq, line in enumerate(out):
        line.update(seq=seq, wall=1794700000.0 + seq, mono=100.0 + seq)
    return out


def encode(out) -> bytes:
    return b"".join((json.dumps(line, sort_keys=True) + "\n").encode() for line in out)


def log(window=(), **options) -> bytes:
    """A whole log's bytes."""
    return encode(lines(window, **options))


def record(*, instruments=None, run_key="run-1", attempt=1):
    """A stand-in for a RunRecord with what `judge` reads."""
    return SimpleNamespace(run_key=run_key, attempt=attempt, instruments=dict(instruments or {}))
