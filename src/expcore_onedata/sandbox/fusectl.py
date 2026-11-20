# Provenance: new
"""A FUSE mount's connection in fusectl, as both backends read it (O34, O97).

The kernel lists every FUSE connection under `/sys/fs/fuse/connections/<N>/`: `waiting`, the requests handed to the
daemon and not yet answered, and `abort`, which ends every one of them with an error when `1` is written to it (the
mount is dead from then on). `N` is the minor of the mount's device (`0:N` in the mount table). The directory and the
two files belong to the user the mount was made for, so the mount's owner may read `waiting` and write `abort` without
privilege, provided fusectl is mounted (a user cannot mount it).

This module holds what the Docker teardown (which writes through `sudo`) and the Apptainer host mount (which writes as
the mount's owner) share: the name of a connection from a device, the device of a mount point read from a mount table
(never by touching the mount point, which would wait on the same wedged connection), the state of a connection's
control files, and the two reads and the one write the host mount makes.
"""

from __future__ import annotations

import os
from pathlib import Path

from expcore_onedata.sandbox import mountproof

FUSECTL = Path("/sys/fs/fuse/connections")
WRITABLE = "writable"  # the connection's `abort` may be written and its `waiting` read, by this uid
ABSENT = "absent"  # fusectl is not mounted, or has no directory for the connection
NOT_WRITABLE = "not_writable"  # the connection has a directory, but this uid may not use its control files
STATES = (WRITABLE, ABSENT, NOT_WRITABLE)
ABORT, WAITING = "abort", "waiting"


def conn(dev: str) -> str | None:
    """The fusectl directory name of a FUSE mount's device ("0:N" -> "N"); None for a device that is not an anonymous
    one (a FUSE mount always has major 0)."""
    major, _, minor = dev.partition(":")
    return minor if major == "0" and minor.isdigit() else None


def fuse_device(mountinfo: str, mountpoint: str) -> str | None:
    """The device ("major:minor") of the FUSE mount at `mountpoint` in a mount table (the last one mounted there), or
    None when it shows none or cannot be read whole. Read from the table alone: a `stat` of the mount point would wait
    on a connection that is wedged."""
    return mountproof.fuse_dev(mountinfo, mountpoint)


def state(root: Path, connection: str | None) -> str:
    """Whether this uid may use the connection's control files: `writable` when `abort` is writable and `waiting`
    readable, `not_writable` when the directory exists and that does not hold, `absent` when it does not exist, or the
    connection is unknown (`conn` gave None)."""
    if connection is None:
        return ABSENT
    directory = root / connection
    if not os.path.isdir(directory):
        return ABSENT
    usable = os.access(directory / ABORT, os.W_OK) and os.access(directory / WAITING, os.R_OK)
    return WRITABLE if usable else NOT_WRITABLE


def waiting(root: Path, connection: str) -> int | None:
    """The requests waiting on a connection, None when the counter cannot be read (unknown, never zero)."""
    try:
        with open(root / connection / WAITING, encoding="ascii") as stream:
            text = stream.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() else None


def write_abort(root: Path, connection: str) -> None:
    """Abort the connection: every request it holds ends with `ECONNABORTED`, later ones with `ENOTCONN`. The file is
    opened, never created (a stale name must not make a file), and `OSError` is the caller's to answer."""
    descriptor = os.open(root / connection / ABORT, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.write(descriptor, b"1")
    finally:
        os.close(descriptor)


__all__ = [
    "ABORT",
    "ABSENT",
    "FUSECTL",
    "NOT_WRITABLE",
    "STATES",
    "WAITING",
    "WRITABLE",
    "conn",
    "fuse_device",
    "state",
    "waiting",
    "write_abort",
]
