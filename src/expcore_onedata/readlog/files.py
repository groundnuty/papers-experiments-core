# Provenance: new
"""The two files a backend ships for the channel: the collector program and the client module, with their hashes.

Both are plain files of this package that run alone: the collector as `python3 -I collector.py ...` outside the cell,
the client bound read-only into every cell. A backend copies each to a place of the run's own (so that nothing the run
does can change what ran), binds the client at `paths.CLIENT` (its file here is `client.py`; the target's name is
`paths.CLIENT_NAME`), and records both hashes in the run's facts (`facts.py`).
"""

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def collector_file() -> Path:
    """The collector program's source file in this package."""
    return Path(__file__).with_name("collector.py")


def client_file() -> Path:
    """The client module's source file in this package."""
    return Path(__file__).with_name("client.py")


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file's bytes, as lower-case hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
