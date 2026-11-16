# Provenance: new
"""A cell's exec is known to have run its program whole and ended, or it is no result (O55).

Two things below the harness can make a cell that never ran read as one that ran and printed nothing. `python -` that
is given no program runs an empty one, and exits 0 with nothing on stdout or stderr: a program lost on its way to the
container's stdin looks exactly so. And the docker client reports exit 0 for an exec whose exit the daemon never
recorded: it reads the exit code from the exec's inspection after its attach stream ends, takes a null exit code (not
recorded) as 0, and never asks whether the exec still runs (`getExecExitStatus` in the docker CLI, `ExecInspect` of
its client). A live Day-0 read cell returned so, exit 0 with both streams empty, and nothing kept could tell which.

So a cell runs under a bootstrap, `BOOTSTRAP`, started by `timeout` in place of the interpreter, as the interpreter
itself (isolated, `-I`, so nothing the agent leaves in its working directory is imported). It reads the program whole
from its stdin and checks its size and SHA-256 against what the harness sent (`Completion.argv`); a program that did
not arrive whole is never run. One that did runs exactly as before: `<python> -`, the program on its stdin, the
streams the exec's own, so its output, its tracebacks and its exit are its own. After that interpreter has exited, the
bootstrap writes one completion record on each stream: a line that starts with a marker naming this exec's nonce, then
whether the source arrived whole and the exit status, as JSON. It then ends as the program did (the same exit code, or
the same signal), so `timeout` and `docker exec` report what they reported before.

The harness takes those lines out of the streams as it reads them (`hostops.run_bounded`'s `marker`, `marks.py`), so
the text the agent sees and the record keeps are the program's own bytes, and `Completion.check` reads the result: a
record with this nonce on both streams, a source that arrived whole and an exit status equal to the one docker
reported make a verified exec. Anything else is incomplete, and its cause is named: the source not written whole to
`docker exec`, or not arrived whole in the container; no record at all, so the exec's exit was not recorded (a stream
that ended first, an exit docker never saw); a record on one stream only, so output was lost; a status docker reports
other than the recorded one. `cells.CellRunner.run` raises `ExecIncomplete` for an incomplete exec that no timeout or
kill explains.

The nonce guards against a record of another exec and against an accident, not against a program that sets out to
forge one: the bootstrap's arguments are readable from the agent's own processes.
"""

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.errors import InfraError
from expcore_onedata.sandbox.api import ExecResult, SandboxError
from expcore_onedata.sandbox.hostops import CmdResult, encode_input

MARKER_WORD = "\x1eexpcore-exec"  # a record separator first: no line of text a program prints begins so by chance
NOT_RUN_EXIT = 70  # what the bootstrap ends with when it did not run the program (its source was not whole)

# Run in the agent's container, by the image's interpreter: plain syntax only, nothing imported from this package.
BOOTSTRAP = r"""
import hashlib, json, os, signal, subprocess, sys
nonce, size, digest, python = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
record = {"source_bytes": 0, "source_whole": False, "exit_status": None}
status, died_by = 70, None
try:
    source = sys.stdin.buffer.read()
    record["source_bytes"] = len(source)
    record["source_whole"] = len(source) == size and hashlib.sha256(source).hexdigest() == digest
    if record["source_whole"]:
        child = subprocess.Popen([python, "-"], stdin=subprocess.PIPE)
        child.communicate(source)
        if child.returncode < 0:
            died_by, status = -child.returncode, 128 - child.returncode
        else:
            status = child.returncode
        record["exit_status"] = status
except BaseException as error:
    record["error"] = type(error).__name__
line = ("\x1eexpcore-exec " + nonce + " " + json.dumps(record, sort_keys=True) + "\n").encode()
for fd in (1, 2):
    try:
        os.set_blocking(fd, True)
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        pass
if died_by is not None:
    try:
        signal.signal(died_by, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    try:
        os.kill(os.getpid(), died_by)
    except OSError:
        pass
os._exit(status)
"""

DOCKER_EXEC = "docker exec"  # the exec a cause names unless the backend names its own (`Completion.runner`)
SOURCE_NOT_SENT = "the cell's source was not written whole to {runner} ({why}): its program did not run"
SOURCE_NOT_WHOLE = (
    "the cell's source did not arrive whole in the container ({got} of {size} bytes): its program did not run"
)
NOT_STARTED = "the cell's interpreter could not be started in the container ({why})"
NOT_RECORDED = (
    "the exec's exit was not recorded: {runner} reported exit {rc} with no completion record from the container, "
    "so its program may not have run, or its output ended before the program did"
)
OTHER_STATUS = "the exec's exit was not recorded: {runner} reported exit {rc} and the container recorded exit {status}"
STREAM_LOST = "the cell's {stream} ended before its completion record: its output was lost"
UNREADABLE = "the completion record of the exec could not be read: its exit is not known"
UNKNOWN_FACTS = {"source_whole": None, "exit_status": None, "nonce_matched": False}


def record_line(nonce: str, record: Mapping[str, Any]) -> str:
    """The completion record's line as the bootstrap writes it, for a host that does not run one (the tests' fake)."""
    return f"{MARKER_WORD} {nonce} {json.dumps(dict(record), sort_keys=True)}\n"


class ExecIncomplete(InfraError, SandboxError):
    """An exec whose program is not known to have run whole and ended (O55): infrastructure of kind `sandbox`, named by
    its cause, never a result. `result` is what `docker exec` returned, with the check's facts (`exec_check`)."""

    def __init__(self, cause: str, result: ExecResult) -> None:
        super().__init__("sandbox", cause)
        self.cause = cause
        self.result = result


@dataclass(frozen=True)
class Check:
    """What a result says of its exec: `facts` for the record (whether the source arrived whole, the exit status the
    completion record saw, whether a record with this exec's nonce came on both streams), and `cause`, why the exec is
    not known to have completed, None when it is."""

    facts: dict[str, Any]
    cause: str | None


def _status(code: int) -> int:
    """An exit as the shell and `docker exec` give it: a death by signal N is 128 + N."""
    return 128 - code if code < 0 else code


def _parse(payload: str) -> dict[str, Any] | None:
    """A record's payload, or None when it is not one the bootstrap writes."""
    try:
        record = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(record, dict) or "exit_status" not in record:
        return None
    whole, size, status = record.get("source_whole"), record.get("source_bytes"), record["exit_status"]
    if not isinstance(whole, bool) or type(size) is not int or not (status is None or type(status) is int):
        return None
    return record


@dataclass(frozen=True)
class Completion:
    """One exec's program and the nonce its completion record carries; `runner` is the exec a cause names (`docker
    exec`, or the Apptainer backend's `apptainer exec`)."""

    nonce: str
    size: int
    sha256: str
    runner: str = DOCKER_EXEC

    @classmethod
    def for_source(cls, code: str, *, runner: str = DOCKER_EXEC) -> "Completion":
        data = encode_input(code)  # the bytes `docker exec` is given, so the bytes the bootstrap checks
        return cls(secrets.token_hex(16), len(data), hashlib.sha256(data).hexdigest(), runner)

    @property
    def marker(self) -> bytes:
        """What every line of this exec's completion record starts with."""
        return f"{MARKER_WORD} {self.nonce} ".encode()

    def argv(self, python: str) -> list[str]:
        """The command `timeout` runs in the agent's container: the bootstrap, isolated, by `python`, which also runs
        the program."""
        return [python, "-I", "-c", BOOTSTRAP, self.nonce, str(self.size), self.sha256, python]

    def check(self, done: CmdResult) -> Check:
        """Read a finished exec against this exec's completion record (see the module docstring)."""
        out, err = done.stdout_marks or (), done.stderr_marks or ()
        rc, runner = _status(done.returncode), self.runner
        if not out and not err:
            why = (
                SOURCE_NOT_SENT.format(runner=runner, why=done.input_error)
                if done.input_error
                else NOT_RECORDED.format(runner=runner, rc=rc)
            )
            return Check(dict(UNKNOWN_FACTS), why)
        if len(out) > 1 or len(err) > 1 or (out and err and out != err):
            return Check(dict(UNKNOWN_FACTS), UNREADABLE)
        if not out or not err:
            return Check(dict(UNKNOWN_FACTS), STREAM_LOST.format(stream="stdout" if not out else "stderr"))
        record = _parse(out[0])
        if record is None:
            return Check(dict(UNKNOWN_FACTS), UNREADABLE)
        status = record.get("exit_status")
        facts = {"source_whole": record["source_whole"], "exit_status": status, "nonce_matched": True}
        if not record["source_whole"]:
            return Check(facts, SOURCE_NOT_WHOLE.format(got=record["source_bytes"], size=self.size))
        if status is None:
            return Check(facts, NOT_STARTED.format(why=record.get("error", "no exit status")))
        if status != rc:
            return Check(facts, OTHER_STATUS.format(runner=runner, rc=rc, status=status))
        return Check(facts, None)


__all__ = ["BOOTSTRAP", "Check", "Completion", "ExecIncomplete", "record_line"]
