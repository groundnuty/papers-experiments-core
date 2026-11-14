# Provenance: new
"""A dataset's manifest: every file a space must hold, with its size and SHA-256, and every directory (O41, #184).

A study makes the manifest once from the copy of the dataset it keeps (`manifest_of_tree`), writes it beside the
dataset (`write_manifest`) and loads it at each federation (`load_manifest`), where `storageimport.ImportedSpace` shows
the space holds exactly that: the same paths, kinds and sizes, every file whole on the data provider, and, when asked,
the same bytes. Paths are relative to the space's root, which is the imported storage's root: op-worker f118ed9 puts an
imported storage's files directly under its mount point (`storage_file_id.erl`, `space_dir_id`).

```json
{"version": 1,
 "files": [{"path": "records/day-01.csv", "size": 240, "sha256": "<64 hex digits>"}, ...],
 "directories": ["records", ...]}
```

Files and directories are sorted by path, each once, and every directory a file lies in is listed, so the manifest is
the whole tree, an empty directory included. A tree that holds anything but regular files and directories is refused:
op-worker's import skips a symbolic link, a FIFO or a socket without a failure (its CHANGELOG, VFS-9118), so such a
tree could never be imported whole. A refusal names the manifest's file and the entry's position, never a path (O12).
"""

import hashlib
import itertools
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError
from expcore_onedata.rangemap import safe_path

VERSION = 1
_HASH_BYTES = 1 << 20  # how much of a file is held at once while it is hashed
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ManifestFile:
    """One file of the dataset: its path in the space, its size and its SHA-256."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    """The dataset a space must hold: its files and its directories, each sorted by path."""

    files: tuple[ManifestFile, ...]
    directories: tuple[str, ...]

    @property
    def bytes(self) -> int:
        """The bytes of every file together."""
        return sum(f.size for f in self.files)

    def to_json(self) -> dict[str, Any]:
        """The manifest as the JSON `write_manifest` writes."""
        return {
            "version": VERSION,
            "files": [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in self.files],
            "directories": list(self.directories),
        }

    def digest(self) -> str:
        """The SHA-256 of the manifest's canonical JSON: what a record names it by, never a path."""
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()

    def label(self, index: int) -> str:
        """How a record and a message name a file of the manifest: by its position, never its path (O12)."""
        return f"manifest.files[{index}]"


def _hash(path: Path) -> tuple[int, str]:
    hasher, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while block := handle.read(_HASH_BYTES):
            hasher.update(block)
            size += len(block)
    return size, hasher.hexdigest()


def manifest_of_tree(root: Path | str) -> Manifest:
    """The manifest of a local tree: every regular file with its size and SHA-256, every directory below `root`.

    Nothing is hashed before the whole tree is known to hold regular files and directories alone; anything else, a
    symbolic link included, raises ValueError with how many entries there are, naming none, and so does a directory
    that cannot be listed, whose files would otherwise be left out. A file is read by blocks, never held whole."""
    top = Path(root)
    if top.is_symlink() or not top.is_dir():
        raise ValueError("the dataset's root is not a directory")
    files: list[Path] = []
    directories: list[Path] = []
    odd = 0

    def unreadable(error: OSError) -> None:  # a directory left out would leave its files out of the manifest
        raise ValueError(f"a directory of the tree could not be read ({type(error).__name__})") from None

    for directory, subdirectories, names in os.walk(top, onerror=unreadable, followlinks=False):
        for name in [*subdirectories, *names]:
            mode = os.lstat(Path(directory) / name).st_mode
            if stat.S_ISDIR(mode):
                directories.append(Path(directory) / name)
            elif stat.S_ISREG(mode):
                files.append(Path(directory) / name)
            else:
                odd += 1
    if odd:
        noun, verb = ("entry", "is") if odd == 1 else ("entries", "are")
        raise ValueError(
            f"{odd} {noun} of the tree {verb} neither a regular file nor a directory; Onedata's import skips such "
            "entries, so the tree would never import whole"
        )
    hashed = sorted(
        (ManifestFile(path.relative_to(top).as_posix(), *_hash(path)) for path in files), key=lambda f: f.path
    )
    return Manifest(tuple(hashed), tuple(sorted(d.relative_to(top).as_posix() for d in directories)))


def write_manifest(manifest: Manifest, path: Path | str) -> None:
    """Write a manifest as JSON, one line per file, so a reader can see which changed."""
    body = manifest.to_json()
    rows = [f"  {json.dumps(entry, sort_keys=True)}" for entry in body["files"]]
    lines = [f'{{"version": {VERSION},', ' "files": [', ",\n".join(rows), " ],"]
    lines.append(f' "directories": {json.dumps(body["directories"])}}}')
    Path(path).write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")


def _fail(file: Path, where: str, problem: str) -> ConfigError:
    return ConfigError(f"{file}: manifest: {where}: {problem}")


def _file_entry(file: Path, index: int, item: Any) -> ManifestFile:
    where = f"files[{index}]"
    if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
        raise _fail(file, where, "must be a table of path, size and sha256 alone")
    path, size, digest = item["path"], item["size"], item["sha256"]
    try:
        safe_path(path)
    except ValueError:
        raise _fail(file, where, "its path must be relative to the space root, with plain names") from None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _fail(file, where, "its size must be a whole number of bytes, zero or more")
    if not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX:
        raise _fail(file, where, "its sha256 must be 64 lower-case hex digits")
    return ManifestFile(path, size, digest)


def load_manifest(path: Path | str) -> Manifest:
    """Load and check a manifest; a mistake raises ConfigError naming the file and the entry's position, never a
    path: the version, a path that is not plain or climbs out, a path given twice or out of order, a size or a hash
    that cannot be one, and a file whose directory is not listed."""
    file = Path(path)
    try:
        body = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{file}: manifest: cannot be read as JSON ({type(error).__name__})") from None
    if not isinstance(body, dict) or set(body) != {"version", "files", "directories"}:
        raise _fail(file, "the file", "must hold version, files and directories alone")
    if body["version"] != VERSION:
        raise _fail(file, "version", f"must be {VERSION}")
    raw_files, raw_dirs = body["files"], body["directories"]
    if not isinstance(raw_files, list) or not isinstance(raw_dirs, list):
        raise _fail(file, "the file", "files and directories must be lists")
    files = tuple(_file_entry(file, i, item) for i, item in enumerate(raw_files))
    for index, (before, after) in enumerate(itertools.pairwise(files), start=1):
        if after.path <= before.path:
            raise _fail(file, f"files[{index}]", "is given twice, or out of order: files are sorted by path, each once")
    directories: list[str] = []
    for index, item in enumerate(raw_dirs):
        try:
            safe_path(item)
        except ValueError:
            raise _fail(file, f"directories[{index}]", "must be relative to the space root, with plain names") from None
        if directories and item <= directories[-1]:
            raise _fail(file, f"directories[{index}]", "is given twice, or out of order")
        directories.append(item)
    listed = set(directories)
    for index, entry in enumerate(files):
        parts = entry.path.split("/")
        if any("/".join(parts[:depth]) not in listed for depth in range(1, len(parts))):
            raise _fail(file, f"files[{index}]", "lies in a directory the manifest does not list")
        if entry.path in listed:
            raise _fail(file, f"files[{index}]", "is listed as a directory too")
    return Manifest(files, tuple(directories))


__all__ = ["VERSION", "Manifest", "ManifestFile", "load_manifest", "manifest_of_tree", "write_manifest"]
