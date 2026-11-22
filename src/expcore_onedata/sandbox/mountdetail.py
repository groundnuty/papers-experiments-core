# Provenance: new
"""What a failed mount proof records of the mounts and the process that failed it, in words that name no path (#179,
milestone 2, O60).

A proof line goes into the run's evidence, and the watcher's log "names no path and no token" (`mountwatch`). So a
failed proof says what KIND of mount broke it, never where it is: the device and file system type (kernel numbers and
names), whether the mount's root and point are one of the few system names every node has or something else, whether the
same mount is in the host's own table (a cell whose table is a copy of the host's), and for a protected directory its
index in the watcher's list and how the mount's root relates to it. Every value is one of a closed set or matches a
pattern that cannot hold a host, a user or a token, so a malformed mount table cannot put text of its own in the line.

The keys are additive: a proof line gains `offending` (at most `MAX_ENTRIES` entries), `offending_total` and `process`;
no key of an older line changes, and `cell` stays exactly `pid`, `start_time`, `nspid_depth`, which the reader requires.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MAX_ENTRIES = 8
GENERIC_PATHS = frozenset({"/", "/proc", "/sys", "/dev", "/dev/shm", "/dev/pts", "/run", "/tmp", "/var/tmp"})
OTHER, UNKNOWN = "other", "?"
UNREAD = "unread"
SAME, DIFFERENT = "same", "different"
HOST, OWN = "host", "own"  # a process's root mount is the host's, or its own
DEV = re.compile(r"\d{1,10}:\d{1,10}")
FSTYPE = re.compile(r"[A-Za-z0-9_.+-]{1,24}")

# A mount an entry is made from: (problem, mount, index of the protected directory or None, relation or None).
Offence = tuple[str, Mapping[str, str], "int | None", "str | None"]


def generic(path: str) -> str:
    """A path as a role: the path itself when it is a system name every node has, else `other`."""
    return path if path in GENERIC_PATHS else OTHER


def entries(found: Sequence[Offence], host_entries: frozenset[tuple[str, str, str]]) -> dict[str, Any]:
    """The `offending` and `offending_total` keys of a failed proof: the first `MAX_ENTRIES` mounts that triggered a
    problem, and how many did."""
    out = []
    for problem, mount, index, relation in found[:MAX_ENTRIES]:
        entry: dict[str, Any] = {
            "problem": problem,
            "dev": mount["dev"] if DEV.fullmatch(mount["dev"]) else UNKNOWN,
            "fstype": mount["fstype"] if FSTYPE.fullmatch(mount["fstype"]) else UNKNOWN,
            "root": generic(mount["root"]),
            "point": generic(mount["mountpoint"]),
            "host_copy": (mount["dev"], mount["root"], mount["mountpoint"]) in host_entries,
        }
        if index is not None:
            entry["protected"], entry["relation"] = index, relation
        out.append(entry)
    return {"offending": out, "offending_total": len(found)}


def process_view(
    pid: int,
    own_mnt_ns: str | None,
    readlink: Callable[[str], str],
    root_mount: str,
) -> dict[str, str]:
    """The `process` key of a failed proof: the process's root (`/` or `other`, as the watcher's `/proc` shows it),
    whether its mount namespace is the watcher's own (`same` or `different`), and whose its root mount is (`host` when
    its `/` mount is the host's, `own`, or `?`); `unread` where a link cannot be read, which is never an error."""
    return {
        "root": _root(pid, readlink),
        "mnt_ns": mnt_ns_state(pid, own_mnt_ns, readlink),
        "root_mount": root_mount,
    }


def _root(pid: int, readlink: Callable[[str], str]) -> str:
    try:
        return "/" if readlink(f"/proc/{pid}/root") == "/" else OTHER
    except OSError:
        return UNREAD


def mnt_ns_state(pid: int, own: str | None, readlink: Callable[[str], str]) -> str:
    """Whether a process's mount namespace is the watcher's own: `same`, `different`, or `unread`."""
    if own is None:
        return UNREAD
    try:
        return SAME if readlink(f"/proc/{pid}/ns/mnt") == own else DIFFERENT
    except OSError:
        return UNREAD
