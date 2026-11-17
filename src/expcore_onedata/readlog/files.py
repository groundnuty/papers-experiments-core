# Provenance: new
"""The two files a backend ships for the channel: the collector program and the client module, with their hashes.

Both are plain files of this package that run alone: the collector as `python3 -I collector.py ...` outside the cell,
the client bound read-only into every cell. A backend copies each to a place of the run's own (so that nothing the run
does can change what ran), binds the client at `paths.CLIENT`, and records both hashes in the run's facts (`facts.py`).
"""

from pathlib import Path


def collector_file() -> Path:
    """The collector program's source file in this package."""
    raise NotImplementedError


def client_file() -> Path:
    """The client module's source file in this package."""
    raise NotImplementedError


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file's bytes, as lower-case hex."""
    raise NotImplementedError
