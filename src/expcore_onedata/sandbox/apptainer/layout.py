# Provenance: new
"""The files a run of the Apptainer backend uses, pinned and laid out in its own directory (O56, O13).

The work root must be an existing directory of the driver's uid, owner-only and not a link (a batch node's per-job
`$TMPDIR` can be a link into a shared file system with a file quota). Every pinned file but the image is copied into
the run's directory and the copy hashed, so what runs is what was hashed whatever happens to the original. The image
is too large to copy for every run: its SHA-256 is computed once per identity (device, inode, size, modification and
change times), and the identity is checked again before every exec, so a file replaced or written to since its hash
is never run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
from pathlib import Path

from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer.argv import bindable
from expcore_onedata.sandbox.apptainer.settings import Pinned
from expcore_onedata.sandbox.apptainer.sockets import socket_path_problem

CHUNK = 1 << 20
TOOL_SERVER_DIR = "toolserver"  # in the run's directory, 0700, while the run's tool server runs: its HOME (O56)
Identity = tuple[int, int, int, int, int]
_HASHED: dict[Identity, str] = {}  # the image's SHA-256 by the identity it had when it was hashed
_HASHED_LOCK = threading.Lock()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def identity(path: Path) -> Identity:
    """What a file is now, by `stat`: any write or replacement changes it."""
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def pin_image(pin: Pinned) -> Identity:
    """The image's identity once its SHA-256 is shown to be the pinned one; `StartRefused` otherwise (O13)."""
    try:
        before = identity(pin.path)
        with _HASHED_LOCK:
            known = _HASHED.get(before)
        digest = known or sha256_of(pin.path)
        if identity(pin.path) != before:
            raise StartRefused("the agent's image changed while it was hashed")
    except OSError:
        raise StartRefused("the agent's image could not be read on this node") from None
    if digest != pin.sha256:
        raise StartRefused("the agent's image is not the file it is pinned to (its SHA-256 differs)")
    with _HASHED_LOCK:
        _HASHED[before] = digest
    return before


def check_work_root(path: Path, uid: int) -> None:
    """The work root as the backend needs it, or `StartRefused` naming what is wrong (never the path)."""
    try:
        info = os.lstat(path)
    except OSError:
        raise StartRefused("[sandbox] work_root does not exist on this node") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StartRefused("[sandbox] work_root must be a directory, not a link")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise StartRefused("[sandbox] work_root must be the driver's own, with no access for others (0700)")
    if not bindable(path):
        raise StartRefused(
            "[sandbox] work_root may hold no ':', ',' or white space: the cells' binds are made under it"
        )


def check_socket_path(path: Path, what: str) -> None:
    """Before anything exists: `StartRefused` when the collector's socket path does not fit a Unix socket's address
    (`sockets.socket_path_problem`: a long `[sandbox] work_root` is the cause, since the path is under it)."""
    problem = socket_path_problem(path, what)
    if problem is not None:
        raise StartRefused(problem)


def copy_pinned(pin: Pinned, target: Path, what: str, *, mode: int = 0o444) -> None:
    """Copy a pinned file to a new file of the run's, and show the copy has the pinned SHA-256; `SandboxError` naming
    `what` otherwise (the copy is removed)."""
    try:
        with open(pin.path, "rb") as source:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as copy:
                shutil.copyfileobj(source, copy, CHUNK)
        same = sha256_of(target) == pin.sha256
    except OSError:
        target.unlink(missing_ok=True)
        raise SandboxError(f"{what} could not be copied into the run's directory") from None
    if not same:
        target.unlink(missing_ok=True)
        raise SandboxError(f"{what} is not the file it is pinned to (its SHA-256 differs)")
    target.chmod(mode)


def write_text(path: Path, text: str, *, mode: int = 0o444) -> None:
    """A new file of the run's with this text (no secret)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(mode)


__all__ = ["check_socket_path", "check_work_root", "copy_pinned", "identity", "pin_image", "sha256_of", "write_text"]
