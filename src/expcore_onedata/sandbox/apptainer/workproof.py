# Provenance: new
"""The work disk's full-fill proof, kept on the node so later starts there check invariants instead (O71).

The full fill (`workdisk.PROBE`) proves that a write past `/work`'s size is refused with ENOSPC; it costs one whole
image of time and memory per start. A proof it leaves is kept here and reused by later starts of the same driver (a
shard step) on the same boot of the node with the same configuration: they check the cheap invariants instead (the
image's size and file-system type from its superblock, a file system of its own of that size and mount type seen from
a cell, and the copy out of the image), and their record names the proof they relied on (`kind: "invariants"`, its
`id`, when it was made and what it showed).

**Where:** `<work_root>/_proofs/<key>.json`. The work root is the driver's own 0700 directory on the node's tmpfs
(`layout.check_work_root`), which no cell binds: a cell binds only its run's `work`, `tmp`, `shm`, `cell` and egress
directories, files of its run's directory, and the pinned files. `_proofs` is made 0700 and each proof is written 0600
by the driver, crash-safe (a draft, fsynced, renamed). On tmpfs it goes with the node's boot.

**Read, never assumed:** `load` opens the proof without following a link and takes it only when it is a regular file
of the driver's, 0600, with no other link, that parses, names this format, this key and this configuration, and whose
checksum (SHA-256 of its canonical fields) holds. Anything else, a missing file included, is no proof, and the full
fill runs again. The checksum shows a proof whole and unedited by accident; the file's place and modes are what keep
a cell from writing it.

**The key** names the configuration the proof is for: the node's boot (`NodeOps.boot_id`), the driver (pid and start
time, the step that runs the shard's campaign), the image's SHA-256, the image's size, the mkfs command and its
version, and the Apptainer command and its version. A part that is unknown (a boot id or a version that cannot be
read) is no key: such a start fills and keeps nothing.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DIR = "_proofs"
FORMAT = "expcore-work-disk-proof/1"
FIELDS = ("format", "key", "config", "id", "proved_utc", "held_bytes", "size_bytes", "filesystem_bytes", "fstype",
          "superblock")  # fmt: skip


def key_of(config: Mapping[str, Any]) -> str | None:
    """The proof's key for a configuration (the module's docstring): the SHA-256 of its canonical JSON, or None when
    a part of it is unknown."""
    if any(value in (None, "") for value in config.values()):
        return None
    return hashlib.sha256(json.dumps(dict(config), sort_keys=True).encode()).hexdigest()


def _checksum(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: document[k] for k in FIELDS}, sort_keys=True).encode()).hexdigest()


def path_of(work_root: Path, key: str) -> Path:
    return work_root / DIR / f"{key}.json"


def load(work_root: Path, key: str, config: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The kept proof for `key`, and why there is none: `("...", "ok")`, `(None, "missing")` or `(None,
    "unreadable")` (the module's docstring). It never raises."""
    path = path_of(work_root, key)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, "missing"
    except OSError as error:  # a link (ELOOP), or what cannot be opened
        log.error("the work disk's proof could not be opened (%s)", errno.errorcode.get(error.errno, error.errno))
        return None, "unreadable"
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return None, "unreadable"
            if info.st_nlink != 1 or info.st_size > 1 << 16:
                return None, "unreadable"
            document = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(document, dict) or set(document) != {*FIELDS, "sha256"}:
        return None, "unreadable"
    if document["format"] != FORMAT or document["key"] != key or document["config"] != dict(config):
        return None, "unreadable"
    if document["sha256"] != _checksum(document):
        return None, "unreadable"
    return document, "ok"


def make(key: str, config: Mapping[str, Any], *, held: int, size: int, total: int, fstype: str,
         superblock: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    """A new proof of a full fill that held: what a later start relies on."""
    document = {
        "format": FORMAT, "key": key, "config": dict(config), "id": secrets.token_hex(8),
        "proved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "held_bytes": held, "size_bytes": size,
        "filesystem_bytes": total, "fstype": fstype, "superblock": dict(superblock),
    }  # fmt: skip
    return {**document, "sha256": _checksum(document)}


def keep(work_root: Path, document: Mapping[str, Any]) -> bool:
    """Write the proof owner-only (the module's docstring), replacing any for its key; whether it was written. A proof
    that cannot be kept costs a later start a full fill, never a run."""
    base = work_root / DIR
    target = path_of(work_root, str(document["key"]))
    draft = base / f".{document['key']}.{secrets.token_hex(4)}.tmp"
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(base, 0o700)
        info = os.lstat(base)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(errno.ENOTDIR, "the proofs' directory is not the driver's own directory")
        os.chmod(base, 0o700)
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(document), sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, target)
    except OSError as error:
        log.error("the work disk's proof could not be kept (%s)", errno.errorcode.get(error.errno, error.errno))
        with contextlib.suppress(OSError):
            draft.unlink(missing_ok=True)
        return False
    return True


def reference(document: Mapping[str, Any]) -> dict[str, Any]:
    """What a run's record keeps of the proof it relied on: its id, when it was made and what it showed."""
    return {key: document[key] for key in ("id", "proved_utc", "held_bytes", "size_bytes", "filesystem_bytes",
                                           "fstype")}  # fmt: skip


__all__ = ["DIR", "FORMAT", "keep", "key_of", "load", "make", "path_of", "reference"]
