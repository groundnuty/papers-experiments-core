# Provenance: new
"""oneclient's bundle on an HPC node: the tar `[sandbox.apptainer] oneclient_bundle` pins, unpacked for one run from
the very bytes that are hashed, the command line that runs it, and what a run records of it (the host mount's
oneclient, `hostmount`).

The bundle (`deploy/hpc-federation/oneclient_bundle.py` builds it from the Compose sidecar's pinned image) holds
`bin/oneclient`, one regular file `lib/<name>` for each library, the image's own loader as `lib/LOADER`, and
`PROVENANCE.json`.

**`unpack`** reads the tar once, hashing every byte as it goes, and writes only what a bundle may hold into an empty
private directory of the run: the directories `bin` and `lib`, and the regular files `bin/oneclient`, `lib/<plain
name>` and `PROVENANCE.json`, each of at most `MAX_FILE_BYTES` and all together at most `MAX_TOTAL_BYTES`. Any other
member (a link, a device, a path that climbs or goes a level deeper) and any name twice, a directory's too, refuses
the start, and so does a tar whose SHA-256, once the whole file is read, is not the pin; the unpacked copy is then
removed. Only that copy runs.

**`Bundle.launcher`** runs the image's loader on the binary: `lib/LOADER --inhibit-cache --inhibit-rpath <every file
of the bundle> --library-path lib bin/oneclient`. `--library-path` alone would not keep the node's libraries out: the
image's libraries carry the DT_RPATH `/opt/oneclient/lib`, which glibc searches before it, and a library opened at run
time (an NSS module) would fall back to the node's `/etc/ld.so.cache`. With every RPATH and RUNPATH of the bundle's
files inhibited and the cache never read, a name is looked for in the bundle, then only in the loader's own
directories (a Debian layout; a node that keeps its libraries under `/usr/lib64` has none there). Measured on a batch
node on 16 Nov 2026: nothing outside the bundle was tried or opened.

**`version_of`** reads `oneclient --version` (`Oneclient: <version>`, and `FUSE library: <version>`), and **`build_id`**
the binary's GNU build-id note, since oneclient prints no build id. With the pin they are what a run records of the
oneclient that served it. Both are `sandbox.oneclient_build`'s, which the Docker sandbox's sidecar record uses too
(re-exported here).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer.settings import Pinned
from expcore_onedata.sandbox.hostops import describe_error
from expcore_onedata.sandbox.oneclient_build import build_id, version_of

LOADER = "ld-linux-x86-64.so.2"
BINARY = "bin/oneclient"
PROVENANCE = "PROVENANCE.json"
DIRECTORIES = ("bin", "lib")
NAME = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,127}")  # a library's file name: never '.', '..' or a path
MAX_FILE_BYTES = 256 << 20
MAX_TOTAL_BYTES = 1 << 30
MAX_MEMBERS = 4096
CHUNK = 1 << 20
MISMATCH = "oneclient's bundle is not the file it is pinned to (its SHA-256 differs)"
UNREAD = "oneclient's bundle could not be read on this node"


@dataclass(frozen=True)
class Bundle:
    """One run's unpacked copy of the bundle."""

    root: Path
    files: tuple[str, ...]  # every file unpacked, relative to the root, in the tar's order

    @property
    def binary(self) -> Path:
        return self.root / BINARY

    def launcher(self) -> list[str]:
        """The words that run oneclient (the module's docstring); its own arguments follow them."""
        lib = self.root / "lib"
        own = sorted(str(self.root / name) for name in self.files if name != PROVENANCE)
        return [str(lib / LOADER), "--inhibit-cache", "--inhibit-rpath", ":".join(own), "--library-path", str(lib),
                str(self.binary)]  # fmt: skip


class _Hashing(io.RawIOBase):
    """A file read through a SHA-256 of every byte that passes."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle, self.digest = handle, hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.handle.read(len(buffer))
        buffer[: len(data)] = data
        self.digest.update(data)
        return len(data)


def _allowed(member: tarfile.TarInfo) -> str | None:
    """What is wrong with a member, None when the bundle may hold it."""
    name = member.name
    if member.isdir():
        return None if name in DIRECTORIES else "a directory the bundle does not hold"
    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
        return "a member that is no regular file or directory"
    folder, _, leaf = name.rpartition("/")
    if name in (BINARY, PROVENANCE) or (folder == "lib" and NAME.fullmatch(leaf)):
        return None if member.size <= MAX_FILE_BYTES else "a file larger than a bundle's"
    return "a path the bundle does not hold"


def _write(source: BinaryIO, target: Path, mode: int) -> int:
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    written = 0
    with os.fdopen(handle, "wb") as out:
        while chunk := source.read(CHUNK):
            out.write(chunk)
            written += len(chunk)
    return written


def _extract(tar: tarfile.TarFile, root: Path) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    total = 0
    for count, member in enumerate(tar, start=1):
        problem = "more members than a bundle's" if count > MAX_MEMBERS else _allowed(member)
        if problem is not None:
            raise StartRefused(f"oneclient's bundle holds {problem}")
        if member.name in seen:
            raise StartRefused("oneclient's bundle names a member twice")
        seen.add(member.name)
        if member.isdir():
            (root / member.name).mkdir(mode=0o700, exist_ok=True)
            continue
        total += member.size
        if total > MAX_TOTAL_BYTES:
            raise StartRefused("oneclient's bundle is larger than a bundle may be")
        (root / member.name).parent.mkdir(mode=0o700, exist_ok=True)
        source = tar.extractfile(member)
        executable = member.name in (BINARY, f"lib/{LOADER}")
        try:
            written = _write(source, root / member.name, 0o500 if executable else 0o400)  # type: ignore[arg-type]
        except FileExistsError:
            raise StartRefused("oneclient's bundle names a file twice") from None
        if written != member.size:
            raise StartRefused("oneclient's bundle is truncated")
        files.append(member.name)
    for needed in (BINARY, f"lib/{LOADER}"):
        if needed not in files:
            raise StartRefused(f"oneclient's bundle has no {needed}")
    return files


def unpack(pin: Pinned, root: Path) -> Bundle:
    """The bundle unpacked into `root`, an empty directory of the run (the module's docstring); `StartRefused` when the
    tar is not the pinned one or holds what a bundle may not, with nothing of it left in `root`."""
    if ":" in str(root) or any(os.listdir(root)):
        raise StartRefused("oneclient's bundle needs an empty directory whose path holds no ':'")
    try:
        return Bundle(root, tuple(_unpacked(pin, root)))
    except BaseException:
        _empty(root)
        raise


def _unpacked(pin: Pinned, root: Path) -> list[str]:
    try:
        raw = open(pin.path, "rb")  # noqa: SIM115 (closed below, after the whole file is hashed)
    except OSError:
        raise StartRefused(UNREAD) from None
    with raw:
        reader = _Hashing(raw)
        files: list[str] = []
        refused: StartRefused | None = None
        try:
            with tarfile.open(fileobj=io.BufferedReader(reader, CHUNK), mode="r|") as tar:
                files = _extract(tar, root)
        except StartRefused as error:
            refused = error
        except (tarfile.TarError, EOFError):
            refused = StartRefused("oneclient's bundle is not a tar a bundle can be")
        except OSError as error:  # a read of the pinned file, or a write of the copy, failed: the node's
            raise SandboxError(f"oneclient's bundle could not be unpacked ({describe_error(error)})") from None
        try:
            while reader.read(CHUNK):  # the rest of the file, whatever follows the archive's end, is hashed too
                pass
        except OSError:
            raise StartRefused(UNREAD) from None
    if reader.digest.hexdigest() != pin.sha256:  # another file than the pinned one, whatever it holds
        raise StartRefused(MISMATCH)
    if refused is not None:
        raise refused
    return files


def _empty(root: Path) -> None:
    for entry in os.listdir(root):
        path = root / entry
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


__all__ = ["Bundle", "build_id", "unpack", "version_of"]
