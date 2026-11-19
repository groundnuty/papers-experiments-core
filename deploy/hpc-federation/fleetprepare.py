"""fleetprepare.py: a member's prepare for the elastic fleet's controller (README.md, "The elastic fleet's controller"),
run on the site's login node, through the site's transport, from the site's deploy directory.

    <python> -I -B deploy/hpc-federation/fleetprepare.py run --fleet DIR --member fed-K --timeout S -- ARGV...
    <python> -I -B deploy/hpc-federation/fleetprepare.py abort --fleet DIR --member fed-K

`ARGV` is the study's own command (its ingest and `hpcfed.py verify-data`, which leaves the marker the controller joins
on): the core never reads what it does. `{root}` in a word is the member's federation root and `{layout}` the fleet's
layout file, both read from the fleet's record by the member's label (one text replace of those two names, so other
braces are kept).

- **Once, and the first end wins.** In `<fleet>/logs/`, `<member>.prepare.json` is the claim, made by an exclusive
  create before the command starts: `run` refuses (status 3) when it is there, so a member's prepare is started at most
  once, whoever asks. `<member>.prepare.proc` names the command's process group and its start tick.
  `<member>.prepare.end.json` is the end: the exit status, the outcome (`ok`, `failed`, `timeout`, `aborted`), the end
  and the duration, made by an exclusive create too, so whichever of `run` and `abort` ends it first is the record, and
  the other never replaces it. A claim with no end is `running`: with no live run of the controller's, it is a
  controller that died or a call that dropped, and the controller reads it so. The files are on the fleet's shared file
  system, so any login node reads them.
- **Bounded here.** The command runs in a session of its own, its standard input closed and its standard output and
  error in `<member>.prepare.log` (0600, never printed, never sent back). At `--timeout`, or when an end is found
  written by another (`abort`), its whole process group is sent TERM and, 10 s later, KILL; a command that ends with a
  status other than 0 has the rest of its group killed too (a worker it left behind). The KILLs go to the group while
  its leader is still unreaped, so the group's id cannot be another process's. The one line this prints is the
  outcome, the exit status, the times and the duration; its status is 0 for `ok`, 124 for a timeout and 1 otherwise.
- **abort** ends a prepare the record says is `running`: it writes the `aborted` end (the live `run` notices it within
  a second, wherever it runs, and ends its command's group), and, if the process the proc file names is alive on this
  host with the recorded start tick (a `run` that is itself gone), ends that group too: never another process. A
  retirement does it before it collects.
"""

import argparse
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fleetrecord import MEMBER, FleetError, Record, member_of, root_of  # noqa: E402

FORMAT = "expcore-hpc-prepare/1"
LOGS = "logs"  # `fleet.py`'s directory of each federation's up and down logs
KILL_GRACE_S = 10.0
POLL_S = 0.05  # how often the command is looked at
LOOK_S = 1.0  # how often a running prepare looks for an end somebody else wrote
EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 3, 1, 124
NOT_STARTED = 127  # the exit status recorded for a command that could not be executed at all
OUTCOMES = ("ok", "failed", "timeout", "aborted")
MAX_RECORD = 1 << 16
PLACEHOLDER = re.compile(r"\{(root|layout)\}")


class Refused(RuntimeError):
    """A call this refuses; its message names no host and no path: it is what the controller logs of a failed call."""


def _file(directory: Path, label: str, suffix: str) -> Path:
    return Path(directory) / LOGS / f"{label}.prepare{suffix}"


def record_path(directory: Path, label: str) -> Path:
    """The claim: made once, when the prepare starts."""
    return _file(directory, label, ".json")


def proc_path(directory: Path, label: str) -> Path:
    return _file(directory, label, ".proc")


def end_path(directory: Path, label: str) -> Path:
    """The end: made once, by whichever of `run` and `abort` ends the prepare first."""
    return _file(directory, label, ".end.json")


def log_path(directory: Path, label: str) -> Path:
    return _file(directory, label, ".log")


def alive(pid: int) -> bool:
    """Whether the process `pid` is there and not a zombie (one that ended and is not yet reaped is gone)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def start_tick(pid: int) -> Optional[int]:
    """The start tick of the process `pid` (`/proc/<pid>/stat`, field 22), None when there is none: with the pid, what
    names one process and never another that reuses the number."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _data(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


def _create(path: Path, record: Mapping[str, Any]) -> None:
    """The record, made exclusively (FileExistsError when it is there) and durable."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_data(record))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _replace(path: Path, record: Mapping[str, Any]) -> None:
    """The record, whole: a draft of ours renamed over the file."""
    draft = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_data(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise


def _read(path: Path) -> Optional[Dict[str, Any]]:
    """A record file as a dict; None when there is none; ValueError for a link, one too long or one not an object."""
    if path.is_symlink():
        raise ValueError("a prepare record is a link")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) > MAX_RECORD:
        raise ValueError("a prepare record is too long")
    found = json.loads(raw.decode("utf-8"))
    if not isinstance(found, dict):
        raise ValueError("a prepare record is not an object")
    return found


def read_record(directory: Path, label: str) -> Optional[Dict[str, Any]]:
    """The member's prepare: None when it never started; else its claim, with `state` `started` until its end is there,
    then `ended` and the end's fields. ValueError for a record that is not this format's or this member's."""
    claim = _read(record_path(directory, label))
    if claim is None:
        return None
    if claim.get("format") != FORMAT or claim.get("member") != label:
        raise ValueError("the prepare record is not this member's")
    end = _read(end_path(directory, label))
    if end is None:
        return {**claim, "state": "started"}
    if end.get("format") != FORMAT or end.get("member") != label or end.get("outcome") not in OUTCOMES:
        raise ValueError("the prepare's end is not this member's")
    return {**claim, **end, "state": "ended"}


def status(directory: Path, label: str) -> Optional[str]:
    """What the controller reads: None (never started), `running` (no end), `ok` (exit 0) or `failed` (anything else, a
    record that cannot be read as one included: the data of such a member is not to be trusted). An OSError other than a
    missing record is the caller's."""
    try:
        found = read_record(directory, label)
    except (ValueError, UnicodeDecodeError):
        return "failed"
    if found is None:
        return None
    if found["state"] == "started":
        return "running"
    return "ok" if found["outcome"] == "ok" else "failed"


def end_record(directory: Path, label: str, started_at: float, result: Mapping[str, Any]) -> bool:
    """The prepare's end, if it has none: True when this call made it, False when another end is already the record."""
    end = {"format": FORMAT, "member": label, "started_at": started_at, **result}
    try:
        _create(end_path(directory, label), end)
    except FileExistsError:
        return False
    return True


def left(child: "subprocess.Popen[bytes]") -> Optional["os.waitid_result"]:
    """What the command's leader did if it has ended, without reaping it (its pid, so its group's id, stays its own
    until it is reaped); None while it runs."""
    return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)


def kill_group(pgid: int, leader_done: Callable[[], bool]) -> None:
    """TERM the group, wait for its leader up to the grace, then KILL whatever is left of the group."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline and not leader_done():
        time.sleep(POLL_S)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def supervise(
    child: "subprocess.Popen[bytes]", timeout_s: float, ended_by_another: Callable[[], bool]
) -> Tuple[int, str]:
    """Wait for the command: its exit status and the outcome. At the timeout, or when another has written the end, its
    group is ended (`kill_group`); a leader that ended with a status other than 0 has the rest of its group KILLed
    before it is reaped. The leader is reaped last, so a group's id is never signalled once it could be another's."""
    deadline = time.monotonic() + timeout_s
    looked = 0.0
    outcome: Optional[str] = None
    info = left(child)
    while info is None and outcome is None:
        now = time.monotonic()
        if now >= deadline:
            outcome = "timeout"
        elif now >= looked:
            looked = now + LOOK_S
            outcome = "aborted" if ended_by_another() else None
        if outcome is None:
            time.sleep(POLL_S)
            info = left(child)
    if outcome is not None:
        kill_group(child.pid, lambda: left(child) is not None)
    else:
        assert info is not None
        if not (info.si_code == os.CLD_EXITED and info.si_status == 0):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)  # a worker the command left behind
            outcome = "failed"
        else:
            outcome = "ok"
    return child.wait(), outcome


def substituted(argv: Sequence[str], root: Path, layout: str) -> List[str]:
    values = {"root": str(root), "layout": layout}
    return [PLACEHOLDER.sub(lambda found: values[found.group(1)], word) for word in argv]


def say(line: str) -> None:
    with contextlib.suppress(OSError):  # the controller that asked may be gone: the record is what is read
        print(line, flush=True)


def run(directory: Path, label: str, timeout_s: float, argv: Sequence[str],
        clock: Callable[[], float] = time.time) -> int:  # fmt: skip
    """The member's prepare: claimed, run bounded with its output in its own log, ended; the status is 0 only for a
    command that exited 0 and was not ended by another."""
    record = Record(directory).read()
    if member_of(record, label)["status"] != "up":
        raise Refused("the member is not up")
    words = substituted(argv, root_of(directory, label), record["layout"]["path"])
    (directory / LOGS).mkdir(mode=0o700, exist_ok=True)
    started, began = clock(), time.monotonic()
    try:
        _create(record_path(directory, label), {"format": FORMAT, "member": label, "started_at": started})
    except FileExistsError:
        raise Refused("its prepare was started once and is never started again") from None
    log = os.open(log_path(directory, label), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
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
                _replace(proc_path(directory, label), {"pgid": child.pid, "start": start_tick(child.pid)})
            except BaseException:
                kill_group(child.pid, lambda: left(child) is not None)  # a prepare the record cannot name is not left
                child.wait()
                raise
            code, outcome = supervise(child, timeout_s, lambda: os.path.lexists(end_path(directory, label)))
    finally:
        os.close(log)
    ended = clock()
    result = {"outcome": outcome, "exit": code, "ended_at": ended, "duration_s": time.monotonic() - began}
    if not end_record(directory, label, started, result):  # another ended it first (an abort): that is the record
        ours = read_record(directory, label) or {}
        result = {key: ours.get(key) for key in ("outcome", "exit", "ended_at", "duration_s")}
    say(json.dumps({"started_at": started, **result}, sort_keys=True))
    return 0 if result["outcome"] == "ok" else EXIT_TIMEOUT if result["outcome"] == "timeout" else EXIT_FAILED


def abort(directory: Path, label: str, clock: Callable[[], float] = time.time) -> int:
    """End a prepare the record says is running: write the `aborted` end (the live `run` ends its command's group when
    it sees it), and end the group the proc file names if it is alive here with the start tick it names; nothing to do
    for one that has ended or never started."""
    try:
        found = read_record(directory, label)
    except (ValueError, UnicodeDecodeError):
        return 0  # a record that cannot be read names no process: it never holds a retirement back
    if found is None or found["state"] != "started":
        return 0
    ended = clock()
    result = {"outcome": "aborted", "exit": None, "ended_at": ended, "duration_s": ended - float(found["started_at"])}
    end_record(directory, label, float(found["started_at"]), result)
    try:
        proc = _read(proc_path(directory, label)) or {}
    except (ValueError, UnicodeDecodeError):
        proc = {}
    pgid, start = proc.get("pgid"), proc.get("start")
    named = isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1 and start is not None
    if named and start_tick(pgid) == start:  # the recorded process, not another that took its number
        kill_group(pgid, lambda: not alive(pgid))
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
        if not MEMBER.fullmatch(args.member):
            raise Refused("--member must be a federation's label")
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
