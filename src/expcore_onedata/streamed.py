# Provenance: new
"""Large files in and out of the platform (O25): what a streamed upload or read reports, and the local half of it.

A file is never held whole: the upload reads the local file a chunk at a time (`chunks`) and the read hands each piece
of a provider's answer on as it comes (`OnezoneClient.read_content`), both hashing as they go. What either reports is a
`Digest`, the size and the SHA-256 of the bytes, and an upload also says what it did (`Upload.action`):

- `created`: the target did not exist; it was created and every byte sent;
- `skipped`: the target already held the same size and SHA-256; nothing was sent;
- `resumed`: the target held a strict prefix of the file; the rest was sent from where it ended;
- `restarted`: the target held something else and was no longer; it was written again from its first byte, every
  write at its offset (a longer target is refused: no write that truncates is ever sent, O44).

A write of content names its media type (`media_type`): Onedata 25.0 answers 415 to a write without one, and the
default is `application/octet-stream`.
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OCTET_STREAM = "application/octet-stream"
ACTIONS = ("created", "skipped", "resumed", "restarted")
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
_MEDIA_TYPE = re.compile(f"{_TOKEN}/{_TOKEN}")  # type/subtype, without parameters (RFC 6838's characters)


@dataclass(frozen=True)
class Digest:
    """The size in bytes and the SHA-256 (hex) of a file's content."""

    size: int
    sha256: str

    def to_json(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class Upload:
    """What an upload did: the target's file id, the digest of the content it now holds (the local file's, verified by
    reading it back), one of `ACTIONS`, and how many bytes were sent."""

    file_id: str
    digest: Digest
    action: str
    sent_bytes: int

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"an upload's action is one of {', '.join(ACTIONS)}")

    def to_json(self) -> dict[str, Any]:
        return {"file_id": self.file_id, **self.digest.to_json(), "action": self.action, "sent_bytes": self.sent_bytes}


def media_type(value: Any) -> str:
    """Return a content type that is a plain `type/subtype`; refuse anything else, a line break above all, since the
    value goes into a header."""
    if not isinstance(value, str) or not _MEDIA_TYPE.fullmatch(value):
        raise ValueError("the content type of a write must be a plain type/subtype, such as application/octet-stream")
    return value


def digest_of(content: bytes) -> Digest:
    """The digest of bytes in memory."""
    return Digest(len(content), hashlib.sha256(content).hexdigest())


def chunks(source: Path, chunk_bytes: int, *, start: int = 0, stop: int | None = None) -> Iterator[tuple[int, bytes]]:
    """Read a local file from `start` to `stop` (its end by default); yield `(offset, bytes)`, `chunk_bytes` at a time.

    Only one chunk is held at a time, whatever the file's size. A file that ends before `stop` simply ends the chunks;
    the caller compares what it got with what it expected.
    """
    if chunk_bytes < 1 or start < 0:
        raise ValueError("chunks are read from a non-negative offset, at least one byte at a time")
    with open(source, "rb") as handle:  # read by chunks, never whole
        handle.seek(start)
        offset = start
        while stop is None or offset < stop:
            want = chunk_bytes if stop is None else min(chunk_bytes, stop - offset)
            block = handle.read(want)
            if not block:
                return
            yield offset, block
            offset += len(block)


__all__ = ["ACTIONS", "OCTET_STREAM", "Digest", "Upload", "chunks", "digest_of", "media_type"]
