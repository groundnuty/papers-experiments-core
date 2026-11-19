"""fleetprepare.py: a member's prepare for the elastic fleet's controller (README.md, "The elastic fleet's controller"),
run on the site's login node, through the site's transport, from the site's deploy directory.

    <python> -I -B deploy/hpc-federation/fleetprepare.py run --fleet DIR --member fed-K --timeout S -- ARGV...
    <python> -I -B deploy/hpc-federation/fleetprepare.py abort --fleet DIR --member fed-K

`ARGV` is the study's own command (its ingest and `hpcfed.py verify-data`, which leaves the marker the controller joins
on): the core never reads what it does. `{root}` in a word is the member's federation root and `{layout}` the fleet's
layout file, both read from the fleet's record by the member's label (a plain text replace, so other braces are kept).

- **Once.** `run` claims `<fleet>/logs/<member>.prepare.json` by an exclusive create before it starts the command and
  refuses (status 3) when the file is there: a member's prepare is started at most once, whoever asks. The record is
  `started` (with the command's process group and its start tick) until the command ends, then `ended` with the exit
  status, the outcome (`ok`, `failed`, `timeout`, `aborted`), the end and the duration. A record that says `started`
  with no live run of the controller's is a controller that died or a call that dropped: the controller reads it so.
- **Bounded here.** The command runs in a session of its own, its standard input closed and its standard output and
  error in `<fleet>/logs/<member>.prepare.log` (0600, never printed, never sent back); at `--timeout` its whole process
  group is sent TERM, then KILL 10 s later. The one line this prints is the outcome, the exit status, the times and the
  duration; its status is 0 for `ok`, 124 for a timeout and 1 otherwise.
- **abort** ends a prepare the record says `started` (its group only while the process there still has the start tick
  the record names: never another process), and records it `aborted`; a retirement does it before it collects.
"""

import argparse
import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fleetrecord import FleetError, Record, member_of, root_of  # noqa: E402

FORMAT = "expcore-hpc-prepare/1"
LOGS = "logs"  # `fleet.py`'s directory of each federation's up and down logs
KILL_GRACE_S = 10.0
EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 3, 1, 124


class Refused(RuntimeError):
    """A call this refuses; its message names no host and no path: it is what the controller logs of a failed call."""


NOT_STARTED = 127  # the exit status recorded for a command that could not be executed at all
OUTCOMES = ("ok", "failed", "timeout", "aborted")
MAX_RECORD = 1 << 16


def record_path(directory: Path, label: str) -> Path:
    return Path(directory) / LOGS / f"{label}.prepare.json"


def log_path(directory: Path, label: str) -> Path:
    return Path(directory) / LOGS / f"{label}.prepare.log"


def start_tick(pid: int) -> Optional[int]:
    """The start tick of the process `pid` (`/proc/<pid>/stat`, field 22), None when there is none: with the pid, what
    names one process and never another that reuses the number."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def alive(pid: int) -> bool:
    """Whether the process `pid` is there and not a zombie (one that ended and is not yet reaped is gone)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _write(path: Path, record: Mapping[str, Any], exclusive: bool) -> None:
    """The record, whole: created exclusively (`exclusive`: the claim), or a draft of ours renamed over it."""
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    target = path if exclusive else path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(target, flags | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.replace(target, path)
    except BaseException:
        if not exclusive:
            with contextlib.suppress(OSError):
                target.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_record(directory: Path, label: str) -> Optional[Dict[str, Any]]:
    """The member's prepare record; None when there is none; ValueError for one that is not this format's (a link,
    too long, not JSON, another member's)."""
    path = record_path(directory, label)
    if path.is_symlink():
        raise ValueError("the prepare record is a link")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > MAX_RECORD:
        raise ValueError("the prepare record is too long")
    found = json.loads(raw.decode("utf-8"))
    if not isinstance(found, dict) or found.get("format") != FORMAT or found.get("member") != label:
        raise ValueError("the prepare record is not this member's")
    ended = found.get("state") == "ended"
    if found.get("state") not in ("started", "ended") or (ended and found.get("outcome") not in OUTCOMES):
        raise ValueError("the prepare record says neither started nor ended")
    return found


def status(directory: Path, label: str) -> Optional[str]:
    """What the controller reads: None (no record), `running` (started, not ended), `ok` (exit 0) or `failed` (anything
    else, a record that cannot be read as one included: the data of such a member is not to be trusted). An OSError
    other than a missing record is the caller's."""
    try:
        found = read_record(directory, label)
    except (ValueError, UnicodeDecodeError):
        return "failed"
    if found is None:
        return None
    if found["state"] == "started":
        return "running"
    return "ok" if found["outcome"] == "ok" else "failed"


def kill_group(pgid: int, process: Optional["subprocess.Popen[bytes]"] = None) -> None:
    """TERM the group, wait for its leader up to the grace, then KILL whatever is left of the group."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        if process is not None:
            if process.poll() is not None:
                break
        elif not alive(pgid):
            break
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    if process is not None:
        process.wait()


def substituted(argv: Sequence[str], root: Path, layout: str) -> List[str]:
    return [word.replace("{root}", str(root)).replace("{layout}", layout) for word in argv]


def say(line: str) -> None:
    with contextlib.suppress(OSError):  # the controller that asked may be gone: the record is what is read
        print(line, flush=True)


def run(directory: Path, label: str, timeout_s: float, argv: Sequence[str],
        clock: Callable[[], float] = time.time) -> int:  # fmt: skip
    """The member's prepare: claimed, run bounded with its output in its own log, recorded; the status is 0 only for
    a command that exited 0."""
    record = Record(directory).read()
    member = member_of(record, label)
    if member["status"] != "up":
        raise Refused("the member is not up")
    words = substituted(argv, root_of(directory, label), record["layout"]["path"])
    (directory / LOGS).mkdir(mode=0o700, exist_ok=True)
    started = clock()
    claim = {"format": FORMAT, "member": label, "state": "started", "started_at": started, "pgid": None, "start": None}
    try:
        _write(record_path(directory, label), claim, exclusive=True)
    except FileExistsError:
        raise Refused("its prepare was started once and is never started again") from None
    log = os.open(log_path(directory, label), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    code: Optional[int]
    try:
        try:
            child = subprocess.Popen(
                words, cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )  # fmt: skip
        except OSError as error:
            os.write(log, f"the command could not be started ({type(error).__name__})\n".encode())
            code, outcome = NOT_STARTED, "failed"
        else:
            try:
                _write(
                    record_path(directory, label), {**claim, "pgid": child.pid, "start": start_tick(child.pid)}, False
                )
            except BaseException:
                kill_group(child.pid, child)  # a prepare the record cannot name is not left running
                raise
            try:
                code = child.wait(timeout=timeout_s)
                outcome = "ok" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                kill_group(child.pid, child)
                code, outcome = None, "timeout"
    finally:
        os.close(log)
    ended = clock()
    result = {"outcome": outcome, "exit": code, "started_at": started, "ended_at": ended, "duration_s": ended - started}
    _write(record_path(directory, label), {**claim, **result, "state": "ended"}, False)
    say(json.dumps(result, sort_keys=True))
    return 0 if outcome == "ok" else EXIT_TIMEOUT if outcome == "timeout" else EXIT_FAILED


def abort(directory: Path, label: str, clock: Callable[[], float] = time.time) -> int:
    """End a prepare the record says started, then record it `aborted`; nothing to do for one that ended or is none."""
    try:
        found = read_record(directory, label)
    except (ValueError, UnicodeDecodeError):
        return 0  # a record that cannot be read names no process: it never holds a retirement back
    if found is None or found["state"] != "started":
        return 0
    pgid, start = found.get("pgid"), found.get("start")
    if (
        isinstance(pgid, int)
        and not isinstance(pgid, bool)
        and pgid > 1
        and start is not None
        and start_tick(pgid) == start
    ):
        kill_group(pgid)
    ended = clock()
    result = {"outcome": "aborted", "exit": None, "ended_at": ended, "duration_s": ended - float(found["started_at"])}
    _write(record_path(directory, label), {**found, **result, "state": "ended"}, False)
    return 0


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="fleetprepare.py", description=__doc__.split("\n\n")[0])
    sub = found.add_subparsers(dest="command", required=True)
    for name in ("run", "abort"):
        verb = sub.add_parser(name)
        verb.add_argument("--fleet", type=Path, required=True)
        verb.add_argument("--member", required=True)
        if name == "run":
            verb.add_argument("--timeout", type=float, required=True)
    return found


def main(argv: Optional[Sequence[str]] = None, clock: Callable[[], float] = time.time) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in words:  # the study's command is everything after the first `--`, unparsed
        cut = words.index("--")
        words, command = words[:cut], words[cut + 1 :]
    args = parser().parse_args(words)
    try:
        if not os.path.isabs(args.fleet):
            raise Refused("--fleet must be an absolute path")
        if args.command == "abort":
            return abort(args.fleet, args.member, clock)
        if not command or not all(command) or not math.isfinite(args.timeout) or args.timeout <= 0:
            raise Refused("run takes a positive --timeout and the command after `--`")
        return run(args.fleet, args.member, args.timeout, command, clock)
    except Refused as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
    except (FleetError, OSError, ValueError) as error:  # their messages may name a path: only the kind is said
        print(f"error: {args.command}: it failed ({type(error).__name__})", file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
