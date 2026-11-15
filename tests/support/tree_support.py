"""What a directory holds, byte for byte, to show that a reader left it as it found it (R16)."""

import contextlib
import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path


def tree_of(root: Path) -> dict[str, str]:
    """Every file below `root`, dot-named ones too, by its POSIX path relative to it, with the SHA-256 of its bytes."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def added(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The files `after` holds that `before` did not, sorted."""
    return sorted(set(after) - set(before))


def without(tree: dict[str, str], prefix: str) -> dict[str, str]:
    """The tree without the files under `prefix` (a directory's relative path with its slash)."""
    return {name: digest for name, digest in tree.items() if not name.startswith(prefix)}


@contextlib.contextmanager
def read_only(directory: Path) -> Iterator[Path]:
    """Make `directory` read-only (mode 555: no file can be created, removed or renamed in it) and restore its mode
    on exit, as a copy fetched read-only from another host is. Its subdirectories keep their own modes."""
    mode = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o555)
    try:
        assert not os.access(directory, os.W_OK), "the test runs with a user that ignores directory modes"
        yield directory
    finally:
        directory.chmod(mode)
