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

A read also says what the platform answered, range by range (`ReadOutcome`, handed to the read's `answers`): the
status, the `Content-Range` as it was sent (None when there was none) and the bytes of the body, so a whole answer
(200, no range), a ranged one (206 with its range) and one clipped at the file's end (206 with a shorter range) are
told apart by what the platform said, never by what the read asked for (O37).

A write of content names its media type (`media_type`): Onedata 25.0 answers 415 to a write without one, and the
default is `application/octet-stream`.

An upload's source is a local file, or a `Slice`: a byte range of a local file read as a file of its own, whose byte 0
is the file's byte `offset` and of which nothing outside the range is ever read. A regular member of an uncompressed tar
is one (`tar_members`), so a dataset kept as one archive per space is uploaded member by member straight from its
archive, never unpacked.
"""

import hashlib
import re
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.rangemap import safe_path

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
class ReadOutcome:
    """What the platform answered to one read of a file's content, as it sent it: the HTTP `status`, its
    `Content-Range` header (None when it sent none) and the `body_bytes` it delivered. Only an answer the read accepted
    as the bytes it asked for is reported: a refused one raises instead (O37)."""

    status: int
    content_range: str | None
    body_bytes: int

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "content_range": self.content_range, "body_bytes": self.body_bytes}


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


@dataclass(frozen=True)
class Slice:
    """`size` bytes of the local file at `path` from its byte `offset`, read as a file of its own (O25): its offsets are
    its own, byte 0 being the file's byte `offset`, and no read of it reaches before `offset` or past `offset + size`.
    Whether the file holds the whole range is checked where it is used (an upload refuses one it does not)."""

    path: Path
    offset: int
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, str | Path):
            raise ValueError("a slice names its file by a path")
        object.__setattr__(self, "path", Path(self.path))
        for name in ("offset", "size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"a slice's {name} is a whole number of bytes, 0 or more")


def chunks(
    source: Path | str | Slice, chunk_bytes: int, *, start: int = 0, stop: int | None = None
) -> Iterator[tuple[int, bytes]]:
    """Read a local file, or a slice of one, from `start` to `stop` (its end by default); yield `(offset, bytes)`,
    `chunk_bytes` at a time, the offsets the source's own.

    Only one chunk is held at a time, whatever the file's size. A file that ends before `stop` simply ends the chunks;
    the caller compares what it got with what it expected. A slice ends at its last byte whatever the `stop`, so no read
    reaches past it (the next member of an archive), and starts at its own `offset`, never before.
    """
    if chunk_bytes < 1 or start < 0:
        raise ValueError("chunks are read from a non-negative offset, at least one byte at a time")
    if isinstance(source, Slice):
        path, base, end = source.path, source.offset, source.size if stop is None else min(stop, source.size)
    else:
        path, base, end = Path(source), 0, stop
    with open(path, "rb") as handle:  # read by chunks, never whole
        handle.seek(base + start)
        offset = start
        while end is None or offset < end:
            want = chunk_bytes if end is None else min(chunk_bytes, end - offset)
            block = handle.read(want)
            if not block:
                return
            yield offset, block
            offset += len(block)


class ArchiveRefused(ValueError):
    """An archive whose members cannot be read as byte ranges of it; the message names why (and the member)."""


# The first bytes of each compression a tar is commonly kept under: such an archive holds no member as a range of it.
_COMPRESSIONS = (
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"LZIP", "lzip"),
    (b"\x1f\x9d", "compress"),
)


_COUNT = re.compile(r"[0-9]+")  # a pax numeric field as a count: decimal digits only, no sign, space or point


def tar_members(archive: Path | str) -> dict[str, Slice]:
    """Every member of an uncompressed tar (ustar, pax or GNU) as the slice of the archive that holds its bytes, by its
    name, in the archive's order (O25). Only the headers are read; a member's bytes are read when its slice is.

    The archive is refused whole (`ArchiveRefused`, naming why) when any member cannot be read as a plain byte range or
    uploaded as a file of a space: a compressed archive, or a file that is no tar; a sparse member, whose bytes in the
    archive are not its content; a link, hard or symbolic; any other entry that is not a regular file (a directory, a
    device, a FIFO); a name held twice, since which bytes are the file's is then not known; a name that is not a path
    relative to a space's root with plain parts (`rangemap.safe_path`: no `/` first, no empty, `.` or `..` part); a pax
    size that is not a plain count (tarfile would read it as 0); a member whose bytes reach past the archive's end;
    bytes up to where the reading stopped that no member returned holds (`_account`: tarfile can drop a member it
    fails inside); and an archive that does not end in its end-of-archive blocks where its reading stopped
    (`_check_end`), since tarfile ends its reading quietly at a damaged later header. A global pax header is read past.
    """
    path = Path(archive)
    with open(path, "rb") as handle:
        head = handle.read(tarfile.BLOCKSIZE)
    if not _is_tar_header(head):  # a tar's first header may begin with any name, "BZh..." too: magic only otherwise
        for magic, kind in _COMPRESSIONS:
            if head.startswith(magic):
                raise ArchiveRefused(f"the archive is compressed ({kind}): only an uncompressed tar is read by members")
    held = path.stat().st_size
    members: dict[str, Slice] = {}
    try:
        with tarfile.open(path, mode="r:") as tar, open(path, "rb") as raw:
            ended = 0  # where the bytes the members returned so far account for end
            for member in tar:
                _account(raw, ended, member.offset)
                members[_member_name(member, members)] = _member_slice(path, member, held)
                ended = member.offset_data + _padded(member.size)
            _account(raw, ended, tar.offset)
            stopped = tar.offset
    except tarfile.ReadError as error:
        raise ArchiveRefused(f"the file is not an uncompressed tar, or one cut short ({error})") from None
    _check_end(path, stopped, held)
    return members


def _padded(size: int) -> int:
    """A member's bytes in the archive: its size up to a whole number of blocks."""
    return -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE


def _account(raw: Any, ended: int, start: int) -> None:
    """Refuse bytes between where the members returned so far end and where the next starts (or the reading stopped)
    that no member returned holds. tarfile can read a member's headers, fail inside it and stop as if the archive had
    ended past it (a pax size of -6 does), dropping the member without a word; so every byte up to where the reading
    stopped must be a returned member's or a global pax header's, which tarfile applies to every member and returns as
    none (its keys reach each member's `pax_headers`, so they are checked there)."""
    at = ended
    while at < start:
        raw.seek(at)
        try:
            header = tarfile.TarInfo.frombuf(raw.read(tarfile.BLOCKSIZE), "utf-8", "surrogateescape")
        except tarfile.HeaderError:
            header = None
        if header is None or header.type != tarfile.XGLTYPE:
            break
        at += tarfile.BLOCKSIZE + _padded(header.size)
    if at != start:
        raise ArchiveRefused(
            f"no member returned holds bytes {ended} to {start} of the archive: tarfile skipped a member it could not "
            "read, so members may be lost"
        )


def _check_end(path: Path, stopped: int, held: int) -> None:
    """Refuse an archive that does not end where its reading stopped. `tarfile` ends its reading quietly at any header
    it cannot parse after the first (a damaged checksum, a header cut short, a member's bytes read as a header after a
    size that lied), so a damaged archive would read as a shorter one with every later member lost. A tar ends in two
    zero blocks, padded with zeros; so from where the reading stopped to the file's end there must be at least two
    blocks and nothing but zeros. The tail is read a block at a time and the first other byte refuses it."""
    if held - stopped < 2 * tarfile.BLOCKSIZE:
        raise ArchiveRefused(
            f"the archive does not end in its end-of-archive blocks at byte {stopped}: it is cut short, or a header "
            "that cannot be read stopped the reading"
        )
    with open(path, "rb") as handle:
        handle.seek(stopped)
        while block := handle.read(64 * tarfile.BLOCKSIZE):
            if block.count(0) != len(block):
                raise ArchiveRefused(
                    f"the archive's reading stopped at byte {stopped} with bytes other than its end-of-archive blocks "
                    "after it: a header that cannot be read, or data past the end, so members may be lost"
                )


def _is_tar_header(block: bytes) -> bool:
    """Whether a block is a valid tar header (its checksum holds), as an uncompressed tar's first block is."""
    try:
        tarfile.TarInfo.frombuf(block, "utf-8", "surrogateescape")
    except tarfile.HeaderError:
        return False
    return True


def _member_name(member: tarfile.TarInfo, seen: dict[str, Slice]) -> str:
    """A member's name, once it is shown to be a regular file's, a path in a space and the first of its name."""
    name = member.name
    if member.issparse() or any(key.startswith("GNU.sparse.") for key in member.pax_headers):
        raise ArchiveRefused(f"member {name!r} is sparse: its bytes in the archive are not its content")
    declared = member.pax_headers.get("size")
    if declared is not None and (not _COUNT.fullmatch(declared) or int(declared) != member.size):
        # tarfile reads a pax size it cannot parse as 0: the member would be empty and its bytes read as a header
        raise ArchiveRefused(f"member {name!r} has a pax size that is not a plain count of its bytes")
    if member.islnk() or member.issym():
        raise ArchiveRefused(f"member {name!r} is a link: it holds no bytes of its own")
    if not member.isreg():
        raise ArchiveRefused(f"member {name!r} is not a regular file")
    try:
        safe_path(name)
    except ValueError:
        raise ArchiveRefused(f"member {name!r} is not a path relative to a space's root, with plain parts") from None
    if name in seen:
        raise ArchiveRefused(f"member {name!r} is in the archive twice")
    return name


def _member_slice(path: Path, member: tarfile.TarInfo, held: int) -> Slice:
    if member.offset_data + member.size > held:
        raise ArchiveRefused(f"member {member.name!r} reaches past the end of the archive")
    return Slice(path, member.offset_data, member.size)


__all__ = [
    "ACTIONS",
    "OCTET_STREAM",
    "ArchiveRefused",
    "Digest",
    "ReadOutcome",
    "Slice",
    "Upload",
    "chunks",
    "digest_of",
    "media_type",
    "tar_members",
]
