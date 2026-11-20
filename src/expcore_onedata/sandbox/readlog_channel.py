# Provenance: new
"""The Docker backend's end of the read-log channel (`expcore_onedata.readlog`): the run's directories and pinned files,
the harness's canary, and the reading of the collector's log. LINUX ONLY: the agent reaches the collector's Unix
datagram socket through a bind of its directory, and Docker Desktop's file sharing cannot pass a socket through a bind.

A study's canonical scripts log every read they make, from inside the agent's container, to a collector the harness runs
in a container of its own (`containers.collector_run_args`). The run's directory holds the channel's directories, the
names the Apptainer backend's helper has, each bound alone so that a bind carries nothing else:

    readlog/program/   the collector's program, a pinned copy (read-only file), bound read-only into the collector
    readlog/log/       the collector's log (0700, the driver's); bound into the collector alone, never the agent
    readlog-sock/      the collector's socket; read-write in the collector, READ-ONLY in the agent
    readlog-client/    the client module, a pinned copy (read-only file); READ-ONLY in the agent

The collector runs as the driver's uid, and the only sender it accepts is the declared agent uid (`SandboxSettings.
agent_uid`): its socket directory is 0755 and its socket 0666 so that another uid can send, and the reader makes a log
with a record from any other uid unknown. When the driver's uid is the agent's, the directory is 0700, as the collector
requires of a directory only its own uid sends through. The peer's pid across the two PID namespaces is recorded as
the kernel gives it (0 where it cannot be translated) and never relied on.

The same collector program, client module, schema, canary and reader as the Apptainer backend's: a log is the same
bytes and means the same for the same sends.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from expcore_onedata.readlog import files, paths
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.containers import READLOG_LOG_NAME, READLOG_PROGRAM_NAME, ReadLogDirs

DIRECTORY = "readlog"  # under the run's directory: the collector's program and its log
SOCKET_DIRECTORY = "readlog-sock"  # likewise: the collector's socket
CLIENT_DIRECTORY = "readlog-client"  # likewise: the client module
COPY_NAME = "read-log.jsonl"  # the collection's copy of the log, in its destination: the Apptainer backend's name too
CANARY_SAID = "canary sent"
# The harness's canary is a cell of the agent's own user that imports the client by its explicit path, as a study's
# script does, and sends the log's first record. A cell that raises, or does not say so, refuses the start.
CANARY_SOURCE = (
    "import importlib.util as u\n"
    f"spec = u.spec_from_file_location({paths.CLIENT_MODULE!r}, {paths.CLIENT!r})\n"
    "client = u.module_from_spec(spec)\n"
    "spec.loader.exec_module(client)\n"
    "client.log_canary()\n"
    f"print({CANARY_SAID!r})\n"
)
# The most a log can hold: the collector's cap and the room of its end line. A log past it was not written by the
# collector, and is not read (None), never read as a prefix.
MAX_READ_BYTES = paths.MAX_LOG_BYTES + paths.END_RESERVE_BYTES


@dataclass(frozen=True)
class Prepared:
    """What the run's start made: the directories, the hashes of the two copies that run, and the uid and gid the
    collector runs as."""

    dirs: ReadLogDirs
    collector_sha256: str
    client_sha256: str
    uid: int
    gid: int


def _directory(path: Path, mode: int) -> Path:
    path.mkdir()
    path.chmod(mode)  # whatever the driver's umask
    return path


def _pinned_copy(source: Path, target: Path, pin: str | None = None) -> str:
    """A read-only copy of `source` at `target` (created exclusively, 0444), and the SHA-256 of the copy: what runs is
    what is hashed, whatever happens to the package's file after. With a `pin`, a copy that does not have it is no copy
    of the pinned file: `SandboxError`, never a run on another program."""
    data = source.read_bytes()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    target.chmod(0o444)
    found = files.sha256_of(target)
    if pin is not None and found != pin:
        raise SandboxError("the run's copy of the read-log collector is not the pinned file")
    return found


def pin_matches(pin: str) -> bool:
    """Whether this core's collector file has the SHA-256 the platform pins. False when it cannot be read."""
    try:
        return files.sha256_of(files.collector_file()) == pin
    except OSError:
        return False


def prepare(run_dir: Path, *, agent_uid: int, pin: str) -> Prepared:
    """Make the run's directories and the two pinned copies, the collector's shown to have the platform's pin. The
    collector's socket directory is 0755 for an agent uid that is not the driver's (the collector checks it) and 0700
    when it is."""
    uid, gid = os.getuid(), os.getgid()
    root = _directory(run_dir / DIRECTORY, 0o700)
    dirs = ReadLogDirs(
        program=_directory(root / "program", 0o755),
        sockets=_directory(run_dir / SOCKET_DIRECTORY, 0o700 if uid == agent_uid else 0o755),
        log=_directory(root / "log", 0o700),
        client=_directory(
            run_dir / CLIENT_DIRECTORY, 0o755
        ),  # the agent's uid reads it: a bind keeps the source's mode
    )
    collector = _pinned_copy(files.collector_file(), dirs.program / READLOG_PROGRAM_NAME, pin)
    client = _pinned_copy(files.client_file(), dirs.client / paths.CLIENT_NAME)
    return Prepared(dirs, collector, client, uid, gid)


def read_log_bytes(dirs: ReadLogDirs) -> bytes:
    """The collector's log, whole, read once, without following a link: raises `OSError` for a log that is missing, is
    not a regular file or is larger than a collector can have written. Never a prefix of it."""
    descriptor = os.open(dirs.log / READLOG_LOG_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "the read log is not a regular file")
        if info.st_size > MAX_READ_BYTES:
            raise OSError(errno.EFBIG, "the read log is larger than the collector writes")
        data = handle.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise OSError(errno.EFBIG, "the read log is larger than the collector writes")
    return data


def log_path(dirs: ReadLogDirs) -> Path:
    return dirs.log / READLOG_LOG_NAME


def holds_token(dirs: ReadLogDirs, token: bytes) -> bool | None:
    """Whether the log holds `token`, read in chunks (any size), without following a link: True or False; None when it
    cannot be read, which a caller that keeps evidence treats as holding it. A missing log holds nothing (False)."""
    if not token:
        return False
    try:
        descriptor = os.open(log_path(dirs), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            tail = b""
            while chunk := handle.read(1 << 20):
                if token in tail + chunk:
                    return True
                tail = (tail + chunk)[-(len(token) - 1) :] if len(token) > 1 else b""
    except OSError:
        return None
    return False


def canary_sent(stdout: str, exit_code: int) -> bool:
    """Whether the canary cell sent the log's first record: it exited 0 and said so."""
    return exit_code == 0 and stdout.strip() == CANARY_SAID


__all__ = [
    "CANARY_SAID",
    "CANARY_SOURCE",
    "CLIENT_DIRECTORY",
    "COPY_NAME",
    "DIRECTORY",
    "MAX_READ_BYTES",
    "SOCKET_DIRECTORY",
    "Prepared",
    "canary_sent",
    "holds_token",
    "log_path",
    "pin_matches",
    "prepare",
    "read_log_bytes",
]
