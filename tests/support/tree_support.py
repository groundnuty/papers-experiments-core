"""What a directory holds, byte for byte, to show that a reader left it as it found it (R16)."""

import hashlib
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
