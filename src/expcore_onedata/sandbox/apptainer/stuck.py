# Provenance: new
"""What a process that outlived its kill is blocked on, parsed from `/proc`'s text (O97): pure functions, no file read.

A cell's process that is killed at its deadline but stays in uninterruptible sleep is a reader waiting on the data
mount's FUSE connection. Whose block it is (the study's own outage, or a wedge) is decided from what the process waits
on, never by touching it: the driver is the same uid on the same wedged connection, so a `stat`, `open` or `read` of the
target would block too. This module parses what `/proc` says of each task (`stuckread` reads the files):

- `syscall`: `parse_syscall`. The number of the call the task is blocked in and its six arguments, or `running`, or
  `-1` for a task blocked outside a syscall (a page fault on a mapped file). `fd_call` says which calls name a file
  descriptor and `path_call` which a path (the nodes are x86_64; another machine's numbers are never read as these);
- `fdinfo` and `mountinfo`: `parse_fdinfo`, `parse_mountinfo`. The mount an open file is on, and every mount of the
  cell's own mount table, so that a file is placed on the cell's fixed targets and never by a name the agent chose;
- the path argument of a blocked path call, read at the pointer the syscall file gives (`cstring`): the agent's own
  argument, never a file's content, and placed lexically (`normalize`, `Roles.place`) since a resolution through the
  file system would block.

`Roles` names the cell's fixed targets (the data mount's path, `/work`, `/tmp`, `/dev/shm`, the read-only inputs) and
gives a mount its role. **A mount is `data` only when it is a FUSE mount at the data mount's path and, when the host
knows the run's connection, on that device**; anything else at that path is `other`, so a role is never granted on a
guess. A file on the data mount has a `space`, the first component under the mount (the mount shows each space by its
name at its root), and a `rest`, the path under the space, at most `REST_KEPT` characters.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

AT_FDCWD = -100  # the "directory descriptor" of an `*at` call that means the working directory
PATH_LIMIT = 4096  # the longest path argument read from a process's memory (PATH_MAX)
REST_KEPT = 256  # the longest rest-of-path a record keeps
RUNNING, UNREADABLE = "running", "unreadable"
OUTSIDE_A_SYSCALL = -1  # `syscall`'s number for a task blocked outside any syscall (a page fault on a mapping)
DATA, WORK, TMP, SHM, INPUT, OTHER = "data", "work", "tmp", "shm", "input", "other"
ROLES = (DATA, WORK, TMP, SHM, INPUT, OTHER)
X86_64 = ("x86_64", "AMD64")  # the machines whose syscall numbers the tables below are

# x86_64 syscall numbers, by the argument that holds what the call waits on. The first field of `syscall` is the number.
FD_CALLS: dict[int, tuple[str, int]] = {  # number: (name, index of the file descriptor argument)
    0: ("read", 0),
    5: ("fstat", 0),
    16: ("ioctl", 0),
    17: ("pread64", 0),
    19: ("readv", 0),
    40: ("sendfile", 1),  # `in_fd`
    74: ("fsync", 0),
    217: ("getdents64", 0),
    275: ("splice", 0),  # `fd_in`
    295: ("preadv", 0),
    326: ("copy_file_range", 0),  # `fd_in`
    327: ("preadv2", 0),
}
PATH_CALLS: dict[int, tuple[str, int | None, int]] = {  # number: (name, index of the directory fd or None, pointer's)
    2: ("open", None, 0),
    4: ("stat", None, 0),
    6: ("lstat", None, 0),
    257: ("openat", 0, 1),
    262: ("newfstatat", 0, 1),
    267: ("readlinkat", 0, 1),
    269: ("faccessat", 0, 1),
    332: ("statx", 0, 1),
}
_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclasses.dataclass(frozen=True)
class Syscall:
    """A task's `syscall` file: the call's number and its six arguments (none when the number is -1)."""

    nr: int
    args: tuple[int, ...]


def parse_syscall(text: str) -> Syscall | str:
    """The reading of `/proc/<pid>/task/<tid>/syscall`: a `Syscall`, `RUNNING` for a task not in a syscall, or
    `UNREADABLE` for text that is neither. A task blocked outside a syscall reads `-1 <sp> <pc>`."""
    words = text.split()
    if words == [RUNNING]:
        return RUNNING
    try:
        if len(words) == 3 and words[0] == "-1":
            return Syscall(-1, ())
        if len(words) == 9:
            return Syscall(int(words[0]), tuple(int(word, 16) for word in words[1:7]))
    except ValueError:
        pass
    return UNREADABLE


def signed32(value: int) -> int:
    """The low 32 bits of a syscall argument as a signed int: how a kernel reads a file descriptor argument (`AT_FDCWD`
    is -100, and a descriptor's upper bits are never looked at)."""
    return ((value & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


def fd_call(call: Syscall) -> tuple[str, int] | None:
    """`(name, descriptor)` when the call waits on a file descriptor it names (a read-family call), else None."""
    entry = FD_CALLS.get(call.nr)
    return None if entry is None or len(call.args) <= entry[1] else (entry[0], signed32(call.args[entry[1]]))


def path_call(call: Syscall) -> tuple[str, int | None, int] | None:
    """`(name, directory descriptor or None, pointer to the path)` when the call waits on a path it names, else None.
    The directory descriptor is None for a call relative to the working directory only."""
    entry = PATH_CALLS.get(call.nr)
    if entry is None or len(call.args) <= max(entry[2], entry[1] or 0):
        return None
    return entry[0], None if entry[1] is None else signed32(call.args[entry[1]]), call.args[entry[2]]


def cstring(data: bytes | None) -> str | None:
    """The text up to the first NUL of a memory read; None when there is no NUL in it (a path longer than the read, or
    a short read that stopped before one: unknown, never a cut path)."""
    if data is None:
        return None
    end = data.find(b"\0")
    return None if end < 0 else data[:end].decode("utf-8", "replace")


def normalize(path: str) -> str:
    """An absolute path with its `.`, `..` and repeated slashes removed, lexically: no file system is asked, so a link
    on the way is not followed (and `..` at the root stays there)."""
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/" + "/".join(parts)


def under(point: str, path: str) -> list[str] | None:
    """The components of the absolute `path` below the mount point `point`; None when it is not below it."""
    if path == point:
        return []
    prefix = point.rstrip("/") + "/"
    return [part for part in path[len(prefix) :].split("/") if part] if path.startswith(prefix) else None


@dataclasses.dataclass(frozen=True)
class FdInfo:
    """What `/proc/<pid>/fdinfo/<fd>` says: the file's offset, its open flags (octal text) and the mount it is on."""

    pos: int | None
    flags: str | None
    mnt_id: int | None


def parse_fdinfo(text: str) -> FdInfo:
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        found.setdefault(key.strip(), value.strip())
    pos, flags, mount = found.get("pos"), found.get("flags"), found.get("mnt_id")
    return FdInfo(
        int(pos) if pos is not None and pos.isdigit() else None,
        flags if flags is not None and re.fullmatch(r"[0-7]{1,12}", flags) else None,
        int(mount) if mount is not None and mount.isdigit() else None,
    )


@dataclasses.dataclass(frozen=True)
class MountEntry:
    """One line of a mount table: its id, its device (`major:minor`), where it is mounted and its file system type."""

    ident: int
    device: str
    point: str
    fstype: str

    @property
    def fuse(self) -> bool:
        return self.fstype.startswith("fuse")


def parse_mountinfo(text: str) -> tuple[MountEntry, ...]:
    """The mounts of a `/proc/<pid>/mountinfo`, in the table's order, a line that is not whole left out. Octal escapes
    in the mount point (`\\040`) are decoded."""
    found = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(" "), tail.split(" ")
        if sep and len(fields) >= 6 and fields[0].isdigit() and len(rest) >= 2:
            point = _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), fields[4])
            found.append(MountEntry(int(fields[0]), fields[2], point, rest[0]))
    return tuple(found)


def mount_with_id(mounts: Iterable[MountEntry], ident: int) -> MountEntry | None:
    return next((entry for entry in mounts if entry.ident == ident), None)


def mount_of_path(mounts: Iterable[MountEntry], path: str) -> MountEntry | None:
    """The mount that holds the absolute `path`: the one with the longest mount point above it, the last listed where
    two are at one point (a later mount covers an earlier). Lexical: a link on the way is not followed."""
    best: MountEntry | None = None
    for entry in mounts:
        if under(entry.point, path) is not None and (best is None or len(entry.point) >= len(best.point)):
            best = entry
    return best


@dataclasses.dataclass(frozen=True)
class Place:
    """Where a file is, as a record keeps it: its mount's role and file system type, and for the data mount the space
    and the rest of the path under it (None when the path is not below the mount point, so unknown)."""

    mount: str
    fstype: str
    space: str | None = None
    rest: str | None = None

    @property
    def fuse(self) -> bool:
        return self.fstype.startswith("fuse")


@dataclasses.dataclass(frozen=True)
class Roles:
    """The cell's fixed targets, which give a mount its role. `device` is the run's connection (`major:minor`), None
    when the host has none to give; with it, only a FUSE mount on that device at the data path is the data mount."""

    data: str
    device: str | None = None
    work: str = "/work"
    tmp: str = "/tmp"
    shm: str = "/dev/shm"
    inputs: tuple[str, ...] = ()

    def of(self, entry: MountEntry) -> str:
        if entry.point == self.data:
            return DATA if entry.fuse and self.device in (None, entry.device) else OTHER
        for role, target in ((WORK, self.work), (TMP, self.tmp), (SHM, self.shm)):
            if entry.point == target:
                return role
        return INPUT if entry.point in self.inputs else OTHER

    def place(self, entry: MountEntry, path: str | None) -> Place:
        """Where `path` (an absolute path, or None when it is not known) is, on the mount `entry`."""
        role = self.of(entry)
        if role != DATA or path is None:
            return Place(role, entry.fstype)
        below = under(entry.point, path)
        if below is None:
            return Place(role, entry.fstype)
        return Place(role, entry.fstype, below[0] if below else None, "/".join(below[1:])[:REST_KEPT])


def within_root(path: str | None, root: str) -> str | None:
    """`path` as the process with the root directory `root` sees it. The kernel shows a descriptor's, a working
    directory's and a mapping's path from the reader's side, so for a process that was chrooted (not pivoted) the path
    carries the root's prefix its own mount table does not: it is taken off. A path not under `root`, and any path with
    no `root` to go by (`/` or unknown), is as it was."""
    if path is None or root in ("", "/"):
        return path
    if path == root:
        return "/"
    return path[len(root) :] if path.startswith(root + "/") else path


def link_path(target: str | None) -> str | None:
    """The absolute path a `/proc` link names, None for what is not one (`pipe:[1]`, `anon_inode:...`, `socket:[1]`);
    the kernel's ` (deleted)` mark is dropped."""
    if target is None or not target.startswith("/"):
        return None
    return normalize(target.removesuffix(" (deleted)"))


__all__ = [
    "AT_FDCWD",
    "DATA",
    "FD_CALLS",
    "INPUT",
    "OTHER",
    "OUTSIDE_A_SYSCALL",
    "PATH_CALLS",
    "PATH_LIMIT",
    "REST_KEPT",
    "ROLES",
    "RUNNING",
    "SHM",
    "TMP",
    "UNREADABLE",
    "WORK",
    "X86_64",
    "FdInfo",
    "MountEntry",
    "Place",
    "Roles",
    "Syscall",
    "cstring",
    "fd_call",
    "link_path",
    "mount_of_path",
    "mount_with_id",
    "normalize",
    "parse_fdinfo",
    "parse_mountinfo",
    "parse_syscall",
    "path_call",
    "signed32",
    "under",
    "within_root",
]
