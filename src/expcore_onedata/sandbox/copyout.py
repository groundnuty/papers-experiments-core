# Provenance: new
"""Copying what a run left in its sandbox out to the driver, as root, without following or keeping a link (O19).

The agent controls every file under its working directory. A symbolic link it leaves there still points into the host
after a copy, and the copy's later readers (the study's collector, the record builder, a package build) follow it; a
hard link to a file outside the output, a device, a FIFO or a socket is as dangerous. The copy has to run as root,
because the agent's files need not be readable by the driver, so it fails closed, in four steps:

1. The tree is listed as root, once per collection, without following anything and without leaving its file system
   (`find -P ROOT -xdev`): each entry's type, link count, device and inode, size, depth and name, the records ended by
   NUL, so a name may hold any byte but NUL and '/'. Names are matched as the listing spells them.
2. Each output, with the directories on its way to it, is checked against the listing before anything is copied. A
   symbolic link on the way or in it (dangling or not), a regular file with more links than the output holds (a hard
   link to a file outside it), a FIFO, socket, device or door, a directory of another file system, a path longer than
   `MAX_PATH_BYTES`, or more bytes in all than `max_bytes` (the working disk's quota: only a sparse file outgrows the
   disk it was written on) is refused. `UnsafeOutput` names each by its path relative to the root and what it is, never
   a link's target, and nothing is copied. An output whose way holds a file or nothing is absent. In the agent's
   working directory each of these is the agent's own doing: the caller records it for the study's scorers.
3. Each output is copied with `cp -R -P` (never dereferencing), keeping nothing of the source's modes, owners or times,
   then given to the driver with `chown -R -P -h` (never following a link).
4. The copy is verified as the driver, by lstat: only regular files and directories, every file with one link, every
   entry the driver's, and the same files, directories and bytes as the listing showed. A copy that is not is removed,
   and `CopyMismatch` raised: the agent made nothing the listing did not show, so the harness's copy failed.

The listing is held in memory; its size is bounded by the file system it lists (the agent's quota-limited disk, or a
directory of the harness's own). Every command goes through `HostOps`; the driver reads only its own copy.
"""

from __future__ import annotations

import collections
import logging
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import HostOps, describe_error, describe_stderr

log = logging.getLogger(__name__)

LISTING = "%y %n %D:%i %s %d %f\\0"  # find -printf: type, links, device:inode, size, depth, name; NUL-ended
MAX_PATH_BYTES = 2048  # of a path relative to the root: a deeper copy would near the kernel's PATH_MAX and fail midway
SHOWN_REFUSALS = 16  # how many refusals a record names; it counts them all
SHOWN_NAME_CHARS = 256  # how much of a refused path a record shows
LIST_TIMEOUT_S = 120.0
COPY_TIMEOUT_S = 600.0
SPECIAL = {"l": "symbolic link", "p": "FIFO", "s": "socket", "b": "block device", "c": "character device", "D": "door"}
OUTSIDE_LINK = "hard link to a file outside the output"
TOO_LONG = f"path longer than {MAX_PATH_BYTES} bytes"
TOO_BIG = "more bytes than the working disk holds"


@dataclass(frozen=True)
class Entry:
    """One line of the listing."""

    kind: str  # find's %y: f, d, l, p, s, b, c, D, or U for a type it does not know
    links: int
    inode: str  # "<device>:<inode>"
    size: int
    depth: int
    name: str

    @property
    def device(self) -> str:
        return self.inode.split(":", 1)[0]


@dataclass(frozen=True)
class Refusal:
    """What could not be copied safely: its path relative to the root, as the agent named it, and what it is."""

    path: str
    what: str


class UnsafeOutput(SandboxError):
    """What a run left holds something the harness cannot copy safely (`refused`), found in the listing before anything
    was copied; nothing of it was handed on. The message counts the refusals by what they are and names no path, which
    is the agent's text."""

    def __init__(self, refused: Sequence[Refusal]) -> None:
        self.refused = tuple(refused)
        counts = collections.Counter(r.what for r in self.refused)
        super().__init__("the output holds what cannot be copied safely: " + described(counts))


class CopyMismatch(SandboxError):
    """A copy that does not hold what the listing showed: removed, never handed on. Nothing in the listing explains it,
    so it is the harness's failure, as a copy command that fails is."""


def described(counts: Mapping[str, int]) -> str:
    """Refusals counted by what they are, in a fixed order: `symbolic link x2, FIFO x1`."""
    return ", ".join(f"{what} x{n}" for what, n in sorted(counts.items()))


@dataclass
class _Output:
    """One output as the listing shows it."""

    rel: str
    parts: tuple[str, ...]
    present: bool = False
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    links: dict[str, list[Any]] = field(default_factory=dict)  # inode -> [names in the output, link count, path]


def output_parts(rel: str) -> tuple[str, ...]:
    """The names of an output's path under the root; '.' parts drop out, so '' and '.' are the root itself."""
    return tuple(p for p in PurePosixPath(rel).parts if p != ".")


def parse_listing(text: str) -> list[Entry]:
    """The entries of a `LISTING`, root first; `SandboxError` when it is not one (cut off, or out of order)."""
    entries = []
    for record in text.split("\0")[:-1]:  # every record ends in NUL; what follows the last is nothing
        fields = record.split(" ", 5)
        try:
            kind, links, inode, size, depth, name = fields
            entry = Entry(kind, int(links), inode, int(size), int(depth), name)
        except ValueError:
            raise SandboxError("the listing of the sandbox's output does not parse") from None
        deepest = entries[-1].depth + 1 if entries else 0  # pre-order: a child comes right after its directory
        if not name or len(kind) != 1 or entry.depth > deepest or (entries and entry.depth == 0):
            raise SandboxError("the listing of the sandbox's output does not parse")
        entries.append(entry)
    if not entries or text[-1:] != "\0":
        raise SandboxError("the listing of the sandbox's output is empty or cut off")
    return entries


def list_tree(host: HostOps, root: Path) -> list[Entry]:
    """List `root` as root, never following a link or leaving its file system."""
    done = host.run(["find", "-P", str(root), "-xdev", "-printf", LISTING], LIST_TIMEOUT_S, sudo=True)
    if done.returncode != 0:
        log.error("listing the sandbox's output failed (%s): %s", done.returncode, describe_stderr(done.stderr))
        raise SandboxError(f"listing the sandbox's output failed ({done.returncode})")
    return parse_listing(done.stdout)


def _shown(names: Iterable[str]) -> str:
    """A path for a record, cut at `SHOWN_NAME_CHARS` (built name by name, so a deep path costs no more)."""
    out = ""
    for name in names:
        out = f"{out}/{name}" if out else name
        if len(out) > SHOWN_NAME_CHARS:
            return out[:SHOWN_NAME_CHARS] + "..."
    return out


def check(
    entries: Sequence[Entry], rels: Sequence[str], *, max_bytes: int | None
) -> tuple[dict[str, _Output], list[Refusal]]:
    """Each output as the listing shows it, and everything in or on the way to one that cannot be copied safely."""
    outputs = {rel: _Output(rel, output_parts(rel)) for rel in rels}
    refused: list[Refusal] = []
    device = entries[0].device
    names: list[str] = []  # the names from the root down to the entry
    lengths: list[int] = [0]  # the byte length of the path at each depth
    for entry in entries:
        if entry.depth:
            del names[entry.depth - 1 :], lengths[entry.depth :]
            names.append(entry.name)
            lengths.append(lengths[-1] + bool(lengths[-1]) + len(entry.name.encode("utf-8", "replace")))
        for out in outputs.values():
            k = len(out.parts)
            if entry.depth < k:  # on the way to the output, if the names so far are its first ones
                if names == list(out.parts[: entry.depth]) and entry.kind == "l":
                    refused.append(Refusal(_shown(names), SPECIAL["l"]))
                continue
            if names[:k] != list(out.parts):
                continue
            out.present = out.present or entry.depth == k
            problem = _problem(entry, device, lengths[-1])
            if problem:
                refused.append(Refusal(_shown(names), problem))
            elif entry.kind == "d":
                out.dirs += 1
            else:
                out.files, out.bytes = out.files + 1, out.bytes + entry.size
                if entry.links > 1:
                    out.links.setdefault(entry.inode, [0, entry.links, _shown(names)])[0] += 1
    total, over = 0, False
    for out in outputs.values():
        refused += [Refusal(path, OUTSIDE_LINK) for seen, links, path in out.links.values() if links > seen]
        total += out.bytes
        if max_bytes is not None and total > max_bytes and not over:  # named once, at the output that passes it
            refused.append(Refusal(_shown(out.parts) or ".", TOO_BIG))
            over = True
    return outputs, list(dict.fromkeys(refused))  # a path on the way to two outputs is one refusal


def _problem(entry: Entry, device: str, length: int) -> str | None:
    """What makes one entry of an output unsafe to copy, or None."""
    if entry.kind not in ("f", "d"):
        return SPECIAL.get(entry.kind, "file of a type the listing does not know")
    if entry.device != device:
        return "directory of another file system" if entry.kind == "d" else "file of another file system"
    if length > MAX_PATH_BYTES:
        return TOO_LONG
    return None


def _remove(host: HostOps, path: Path) -> None:
    """Remove a copy that is not to be handed on; `rm` never follows a link. A failure is logged and left."""
    done = host.run(["rm", "-rf", "--one-file-system", "--", str(path)], COPY_TIMEOUT_S, sudo=True)
    if done.returncode != 0 or os.path.lexists(path):
        log.error("a refused copy could not be removed (%s): %s", done.returncode, describe_stderr(done.stderr))


def _copy(host: HostOps, src: Path, dst: Path) -> None:
    """Copy as root with no-dereference semantics, then make the copy the driver's without following a link. The copy
    goes to a new path: one merged into what is already there could not be verified against the listing."""
    if os.path.lexists(dst):
        raise SandboxError("a copy out of the sandbox would be merged into what its destination already holds")
    dst.parent.mkdir(parents=True, exist_ok=True)
    copied = host.run(
        ["cp", "-R", "-P", "--no-preserve=all", "--one-file-system", "-T", "--", str(src), str(dst)],
        COPY_TIMEOUT_S,
        sudo=True,
    )
    owner = host.run(["chown", "-R", "-P", "-h", "--", f"{os.getuid()}:{os.getgid()}", str(dst)], 60, sudo=True)
    if copied.returncode != 0 or owner.returncode != 0:
        log.error("copying out failed: %s", describe_stderr(copied.stderr or owner.stderr))
        raise SandboxError("copying a file out of the sandbox failed")


def verify(dst: Path, out: _Output) -> str | None:
    """Why the copy at `dst` is not safe to hand on, or None: read by lstat as the driver, never following a link."""
    uid, gid = os.getuid(), os.getgid()
    files = dirs = size = 0
    pending = [dst]
    try:
        while pending:
            path = pending.pop()
            info = os.lstat(path)
            if (info.st_uid, info.st_gid) != (uid, gid):
                return "an entry of the copy is not the driver's"
            if stat.S_ISDIR(info.st_mode):
                dirs += 1
                with os.scandir(path) as listing:
                    pending += [Path(e.path) for e in listing]
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files, size = files + 1, size + info.st_size
            else:
                return "the copy holds an entry that is not a file of one link or a directory"
    except OSError as error:
        return f"the copy could not be read ({describe_error(error)})"
    if (files, dirs, size) != (out.files, out.dirs, out.bytes):
        return "the copy does not hold what the listing showed"
    return None


def copy_tree(
    host: HostOps, root: Path, copies: Sequence[tuple[str, Path]], *, max_bytes: int | None
) -> dict[str, dict[str, int] | None]:
    """Copy each output `rel` of `root` to its destination, as root, under the rule of this module.

    `root` is the harness's own directory (the agent's working directory is the root of its own disk, which the agent
    cannot replace). Each destination is a new path, and an output inside another one is copied with it, so its
    destination must lie at the same place under the other's. Returns, per output, its files and bytes, or None when it
    is absent; raises `UnsafeOutput` when anything in or on the way to an output cannot be copied safely, before any
    copy is made, `CopyMismatch` when a copy is not what the listing showed (that copy is removed), and `SandboxError`
    when a command fails."""
    rels = [rel for rel, _ in copies]
    if not host.exists(root):
        return dict.fromkeys(rels)
    outputs, refused = check(list_tree(host, root), rels, max_bytes=max_bytes)
    if refused:
        raise UnsafeOutput(refused)
    got: dict[str, dict[str, int] | None] = {}
    copied: list[tuple[str, ...]] = []
    for rel, dst in sorted(copies, key=lambda copy: len(outputs[copy[0]].parts)):  # an enclosing output first
        out = outputs[rel]
        got[rel] = {"files": out.files, "bytes": out.bytes} if out.present else None
        if not out.present or any(out.parts[: len(parts)] == parts for parts in copied):
            continue
        _copy(host, root.joinpath(*out.parts), dst)
        why = verify(dst, out)
        if why is not None:
            log.error("a copy out of the sandbox was refused after it was made: %s", why)
            _remove(host, dst)
            raise CopyMismatch(f"a copy out of the sandbox is not what was listed: {why}")
        copied.append(out.parts)
    return {rel: got[rel] for rel in rels}


def record(refused: Iterable[Refusal]) -> dict[str, Any]:
    """What a run's result and metadata keep of the refusals: at most `SHOWN_REFUSALS` named, by path and what each is,
    and all of them counted, in all and by what they are."""
    every = list(refused)
    return {
        "refused": [{"path": r.path, "what": r.what} for r in every[:SHOWN_REFUSALS]],
        "count": len(every),
        "by_what": dict(sorted(collections.Counter(r.what for r in every).items())),
    }


__all__ = [
    "MAX_PATH_BYTES",
    "CopyMismatch",
    "Refusal",
    "UnsafeOutput",
    "copy_tree",
    "list_tree",
    "parse_listing",
    "record",
]
