"""Helpers for the tests of what a read-only git check must not do: write the repository it reads.

`git status` refreshes `.git/index` as a side effect, but only when an entry's recorded stat no longer matches its
file. A fresh clone's first status, and the first status after a tracked file's mtime changes, are such a case; a
repository that was just committed is not. `aged_copy` makes the first kind: a copy of a repository in which every
tracked file's mtime is later than the one its index entry holds.
"""

import contextlib
import os
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from shutil import copytree
from typing import NamedTuple

LATER_NS = 20 * 10**9


class IndexState(NamedTuple):
    """What a write to `.git/index` would change: its bytes and its mtime."""

    data: bytes
    mtime_ns: int


def index_state(repo: Path) -> IndexState:
    """The index of the repository at `repo`, as bytes and mtime."""
    index = repo / ".git" / "index"
    return IndexState(index.read_bytes(), index.stat().st_mtime_ns)


def aged_copy(repo: Path, destination: Path) -> Path:
    """Copy `repo` to `destination` and push every tracked file's mtime later than its index entry records."""
    copytree(repo, destination, symlinks=True)
    listing = subprocess.run(
        ["git", "-C", str(destination), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    for name in filter(None, listing.split("\0")):
        path = destination / name
        seen = path.stat()
        os.utime(path, ns=(seen.st_atime_ns, seen.st_mtime_ns + LATER_NS))
    return destination


def plain_status_writes_index(repo: Path) -> bool:
    """Run a plain `git status` (no flag, the way an unprotected check runs it) and say whether it wrote the index."""
    before = index_state(repo)
    subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], capture_output=True, check=True
    )
    return index_state(repo) != before


@contextlib.contextmanager
def read_only_tree(root: Path) -> Iterator[None]:
    """Make `root` and everything under it unwritable for the block, and writable again at its end."""
    entries = [root, *(Path(base) / name for base, dirs, files in os.walk(root) for name in [*dirs, *files])]
    modes = {entry: stat.S_IMODE(entry.lstat().st_mode) for entry in entries if not entry.is_symlink()}
    try:
        for entry, mode in modes.items():
            entry.chmod(mode & ~0o222)
        yield
    finally:
        for entry, mode in sorted(modes.items(), key=lambda item: len(item[0].parts)):
            entry.chmod(mode | stat.S_IWUSR)
