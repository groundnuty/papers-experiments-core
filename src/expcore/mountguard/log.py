# Provenance: new
"""Reading the mount watcher's log (`expcore_onedata.sandbox.mountwatch`): intact or unknown and why, the accesses in
the agent's window, and whether the cell's isolation was proven.

The log is JSON lines, each numbered by the watcher (`seq` 0, 1, 2, ...) with its clock readings (`wall`, `mono`):

- `start`: `format` "expcore-mountwatch/1", the host's `nonce`, the watcher's `pid`, `uid` and `nspid_depth` (how many
  PID namespaces deep it runs), the kernel's `ptrace_scope` (Yama's, None without Yama), `watches`, whether its
  inotify watches on the token's directory (`dir`) and on the token file (`token`) were set, and `mount_seen`, whether
  it found the run's FUSE mount in its own mount table (by which it knows the run's cells);
- `inotify`: one event, its `target` watch, its `mask` names and its `entry` (`self`, the watched object itself;
  `token`, the token file's entry in the directory; `other`, any other entry, never named);
- `overflow`: the kernel's event queue overflowed, so events were lost;
- `oneclient`: oneclient's process as the host named it, `seen` when the watcher could read it, with its depth;
- `armed`, then `disarmed`: the agent's window. The watcher opens it itself once both watches have seen the host
  remove the token file (the canary), which the host does before any cell starts, and closes it on its stop, which
  the host sends once every cell has ended;
- `tracer`: a sample of oneclient's `TracerPid` changed (`attached` true or false); `sampling` `stopped` when
  oneclient's status could no longer be read (`why`: `gone` when the process ended, `unreadable` otherwise);
- `proof`: one isolation proof from outside (`label`: `cell` for a cell of the run's, `host_process` for a process
  of the node's PID namespace that holds the run's mount where the host has none, `close` for a cell still alive when
  the window closed; `ok`, `problems`, the `cell` it read: `pid`, `start_time`, `nspid_depth`);
- `end`: the lines before it (`lines`), oneclient's samples taken (`samples`), the polls of the node's processes
  (`polls`) and those that could not list them (`polls_failed`), and the lines it could not write (`unwritten`).

**The log is intact only when all of these hold:** it is present and not past the size limit; every line is a JSON
object of its kind; the first is the start line of this run's nonce with both watches set; `seq` runs from 0 without a
gap and `mono` never goes back; the last line is the end line, its count is the lines before it and nothing was left
unwritten; the canary is there (before `armed`, the token file's watch saw it removed and the directory's watch saw
its entry removed: the host's removal once the mount was up, which shows both watches live); oneclient was named and
seen before `armed`; `armed` and `disarmed` are each there once, in that order; no overflow; no proof outside the
window; and oneclient's sampling never became unreadable before the window closed. Otherwise it is unknown, with
every reason found. **A missing or damaged log is never read as one without accesses**: what it shows is kept as a
lower bound.

**An access** is any inotify event inside the window (between `armed` and `disarmed`, or the log's end when it never
closed): nothing of the host's touches the token's directory then, and inotify does not say which process caused an
event, so every one counts as the agent's. A tracer is counted each time one attaches to oneclient inside the window,
and once more when one was already attached as it opened.

**The isolation is proven** when the watcher found the run's mount, every poll of the node's processes could list
them, the window holds at least one proof, and every proof in it passes. Each cell is its own `apptainer exec` with a
PID namespace of its own; the watcher proves every one it finds alive at a poll, so the run's proof rests on the cells
it caught (at least the start's canary cell, which holds on for a second) and on every cell being started by the same
command line. A proof passes only when it says `ok` and lists no problem.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

FORMAT = "expcore-mountwatch/1"
MAX_BYTES = 64 * 1024 * 1024  # a log past this is not read: unknown (`log_too_large`)
TARGETS = ("token", "dir")
# Each watch's count of accesses, as the record keeps it: a key `token`, or one ending in `_token`, is a secret's to the
# record's redaction (`expcore.records.redact`), and a count is none.
ACCESS_KEYS = {"token": "token_file", "dir": "token_dir"}
ENTRIES = ("self", "token", "other")
MASK_NAMES = frozenset(
    {
        "IN_ACCESS", "IN_MODIFY", "IN_ATTRIB", "IN_CLOSE_WRITE", "IN_CLOSE_NOWRITE", "IN_OPEN", "IN_MOVED_FROM",
        "IN_MOVED_TO", "IN_CREATE", "IN_DELETE", "IN_DELETE_SELF", "IN_MOVE_SELF", "IN_UNMOUNT", "IN_IGNORED",
        "IN_ISDIR",
    }
)  # fmt: skip
LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")
PROBLEM = re.compile(r"[a-z][a-z0-9_]{0,63}")
STOPPED_GONE, STOPPED_UNREADABLE = "gone", "unreadable"
END_COUNTS = ("lines", "samples", "polls", "polls_failed", "unwritten")

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
UNWRITTEN = "unwritten_lines"
WATCH_NOT_SET = "watch_not_set"
NO_CANARY = "no_canary"
NOT_ARMED = "not_armed"
NOT_DISARMED = "not_disarmed"
EVENTS_LOST = "events_lost"
ONECLIENT_UNWATCHED = "oneclient_unwatched"
SAMPLING_FAILED = "sampling_failed"
PROOF_OUTSIDE_WINDOW = "proof_outside_window"

# Why the isolation is not proven; each is a reason in the snapshot's `unproven`, with the failed proofs' own problems.
NO_PROOF = "no_proof"
PROOF_FAILED = "proof_failed"
MOUNT_UNSEEN = "mount_unseen"
POLL_FAILED = "cell_poll_failed"
LOG_INCOMPLETE = "log_incomplete"  # the log was cut, damaged or never closed: what it lacks may be a failed proof


@dataclass(frozen=True)
class Reading:
    """What a log shows: why it is not intact (empty when it is), the accesses inside the window by watch (on the token
    file, `token_file`, and on its directory, `token_dir`), the tracers attached to oneclient inside it, the proofs
    read in it, why the isolation is not proven (empty when it is), and the kernel's ptrace scope."""

    problems: tuple[str, ...]
    accesses: Mapping[str, int] = field(default_factory=lambda: dict.fromkeys(ACCESS_KEYS.values(), 0))
    traced: int = 0
    proofs: int = 0
    isolation_problems: tuple[str, ...] = (NO_PROOF,)
    ptrace_scope: int | None = None

    @property
    def intact(self) -> bool:
        return not self.problems


def _whole(value: Any, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _cell(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"pid", "start_time", "nspid_depth"}
        and all(_whole(value[k], 0, 2**63) for k in value)
    )


def _proof(line: Mapping[str, Any]) -> bool:
    problems = line.get("problems")
    return (
        isinstance(line.get("label"), str)
        and LABEL.fullmatch(line["label"]) is not None
        and isinstance(line.get("ok"), bool)
        and isinstance(problems, list)
        and all(isinstance(p, str) and PROBLEM.fullmatch(p) for p in problems)
        and "cell" in line
        and (line["cell"] is None or _cell(line["cell"]))
    )


def _inotify(line: Mapping[str, Any]) -> bool:
    mask = line.get("mask")
    return (
        line.get("target") in TARGETS
        and line.get("entry") in ENTRIES
        and isinstance(mask, list)
        and bool(mask)
        and all(isinstance(m, str) and m in MASK_NAMES for m in mask)
    )


def _well_formed(line: Mapping[str, Any]) -> bool:
    """Whether a line after the start line is one of its kind."""
    event = line["event"]
    if event == "inotify":
        return _inotify(line)
    if event == "proof":
        return _proof(line)
    if event == "tracer":
        return isinstance(line.get("attached"), bool)
    if event == "sampling":
        return line.get("state") == "stopped" and line.get("why") in (STOPPED_GONE, STOPPED_UNREADABLE)
    if event == "oneclient":
        depth = line.get("nspid_depth")
        return (
            _whole(line.get("pid"), 1, 2**31)
            and isinstance(line.get("seen"), bool)
            and (depth is None or _whole(depth, 1, 64))
        )
    if event == "end":
        return all(_whole(line.get(k), 0, 2**63) for k in END_COUNTS)
    return event in ("overflow", "armed", "disarmed")


def _lines(data: bytes) -> list[dict[str, Any]] | None:
    """Every line decoded, or None at the first one that is not a JSON object with an event."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n"):
        return None  # a cut last line
    out = []
    for raw in text[:-1].split("\n"):
        try:
            line = json.loads(raw, parse_constant=lambda name: math.nan)
        except ValueError:
            return None
        if not isinstance(line, dict) or not isinstance(line.get("event"), str):
            return None
        out.append(line)
    return out


def _sequence(lines: list[dict[str, Any]]) -> bool:
    """`seq` 0, 1, 2, ... and `mono` never going back."""
    last = -math.inf
    for index, line in enumerate(lines):
        mono = line.get("mono")
        if not _whole(line.get("seq"), index, index) or not isinstance(mono, int | float) or not mono >= last:
            return False
        last = mono
    return True


def _start(line: dict[str, Any], nonce: str, problems: list[str]) -> tuple[int | None, bool]:
    """The kernel's ptrace scope and whether the run's mount was seen, from a start line of this run, recording what is
    wrong with it."""
    scope = line.get("ptrace_scope")
    whole = (
        line.get("event") == "start"
        and line.get("format") == FORMAT
        and _whole(line.get("uid"), 0, 2**32)
        and _whole(line.get("nspid_depth"), 1, 64)
        and (scope is None or _whole(scope, 0, 3))
        and isinstance(line.get("watches"), Mapping)
        and isinstance(line.get("mount_seen"), bool)
    )
    if not whole:
        problems.append(NO_START)
        return None, False
    if line.get("nonce") != nonce:
        problems.append(NOT_THIS_RUN)
    if line["watches"] != dict.fromkeys(TARGETS, True):
        problems.append(WATCH_NOT_SET)
    return scope, line["mount_seen"]


def _canary(setup: list[dict[str, Any]]) -> bool:
    """Whether the lines before `armed` show both watches live: the token file removed on its own watch, and its
    entry removed on the directory's."""
    seen = {(x["target"], x["entry"], m) for x in setup if x["event"] == "inotify" for m in x["mask"]}
    return {("token", "self", "IN_DELETE_SELF"), ("dir", "token", "IN_DELETE")} <= seen


def _window(lines: list[dict[str, Any]], problems: list[str]) -> tuple[int, int]:
    """The indices of `armed` and of `disarmed` (or the end), recording what is wrong with them."""
    armed = [i for i, line in enumerate(lines) if line["event"] == "armed"]
    disarmed = [i for i, line in enumerate(lines) if line["event"] == "disarmed"]
    if len(armed) != 1:
        problems.append(NOT_ARMED)
    opened = armed[0] if armed else len(lines)
    if len(disarmed) != 1 or disarmed[0] < opened:
        problems.append(NOT_DISARMED)
        return opened, len(lines)
    return opened, disarmed[0]


def _isolation(proofs: list[dict[str, Any]], mount_seen: bool, polls_failed: int) -> tuple[str, ...]:
    """Why the proofs in the window do not prove the isolation; empty when they do."""
    found: list[str] = []
    if not mount_seen:
        found.append(MOUNT_UNSEEN)
    if polls_failed:
        found.append(POLL_FAILED)
    if not proofs:
        found.append(NO_PROOF)
    failed = [p for p in proofs if not p["ok"] or p["problems"] or p["cell"] is None]
    if failed:
        found.append(PROOF_FAILED)
        found += [problem for p in failed for problem in p["problems"]]
    return tuple(dict.fromkeys(found))


def _body(lines: list[dict[str, Any]], problems: list[str]) -> list[dict[str, Any]]:
    """The lines after the start line up to the first one that is not of its kind, recording that one."""
    for index, line in enumerate(lines):
        if not _well_formed(line):
            problems.append(UNPARSEABLE)
            return lines[:index]
    return lines


def _end(lines: list[dict[str, Any]], problems: list[str]) -> tuple[list[dict[str, Any]], int]:
    """The lines before the end line and its failed polls, recording what is wrong with the end. A log that was not
    closed counts none, as the isolation is not proven from it anyway (`not_closed`)."""
    ends = [i for i, line in enumerate(lines) if line["event"] == "end"]
    last = lines[-1]
    if ends != [len(lines) - 1] or len(lines) < 2 or not _well_formed(last):
        problems.append(NOT_CLOSED)
        return [line for line in lines if line["event"] != "end"], 0
    if last["lines"] != len(lines) - 1:
        problems.append(COUNTS_DIFFER)
    if last["unwritten"] != 0:
        problems.append(UNWRITTEN)
    return lines[:-1], last["polls_failed"]


def read_log(data: bytes | None, nonce: str, *, max_bytes: int = MAX_BYTES) -> Reading:
    """What the watcher's log shows and every reason it is not intact (see the module's text)."""
    if data is None:
        return Reading((MISSING,))
    if not data:
        return Reading((EMPTY,))
    if len(data) > max_bytes:
        return Reading((TOO_LARGE,))
    decoded = _lines(data)
    if decoded is None:
        return Reading((UNPARSEABLE,))
    problems: list[str] = []
    if not _sequence(decoded):
        problems.append(NOT_MONOTONIC)
    scope, mount_seen = _start(decoded[0], nonce, problems)
    whole, polls_failed = _end(decoded, problems)
    body = _body(whole[1:], problems)
    opened, closed = _window(body, problems)
    setup, inside = body[:opened], body[opened + 1 : closed]
    if not _canary(setup):
        problems.append(NO_CANARY)
    if not any(x["event"] == "oneclient" and x["seen"] for x in setup):
        problems.append(ONECLIENT_UNWATCHED)
    if any(x["event"] == "overflow" for x in body):
        problems.append(EVENTS_LOST)
    if any(x["event"] == "sampling" and x["why"] == STOPPED_UNREADABLE for x in body[:closed]):
        problems.append(SAMPLING_FAILED)
    if any(x["event"] == "proof" for x in setup + body[closed:]):
        problems.append(PROOF_OUTSIDE_WINDOW)
    accesses = dict.fromkeys(ACCESS_KEYS.values(), 0)
    for x in inside:
        if x["event"] == "inotify":
            accesses[ACCESS_KEYS[x["target"]]] += 1
    tracers = [x["attached"] for x in setup if x["event"] == "tracer"]
    traced = int(bool(tracers) and tracers[-1]) + sum(1 for x in inside if x["event"] == "tracer" and x["attached"])
    proofs = [x for x in inside if x["event"] == "proof"]
    isolation = _isolation(proofs, mount_seen, polls_failed)
    if not isolation and set(problems) & {NOT_CLOSED, UNPARSEABLE, NOT_DISARMED}:
        isolation = (LOG_INCOMPLETE,)  # lines may be missing, a failed proof among them: never proven from it
    return Reading(tuple(dict.fromkeys(problems)), accesses, traced, len(proofs), isolation, scope)
