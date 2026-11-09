# Provenance: new
"""What a package's checks read in a file, failing closed: only kinds of content they can read in full are allowed.

`streams(path, data)` classifies a file and everything inside it. A stream may be:
- text: valid UTF-8 without NUL bytes, or UTF-16 with a byte-order mark;
- an SQLite database (its magic header), which stores text uncompressed, so its bytes are read as they are;
- a gzip, bzip2, xz, zip or tar archive, whose members are classified in turn, recursively; its own bytes are read as
  well (a gzip header's file name, a zip comment).
Anything else is refused, named with the kind detected: a PDF, a PNG, a raw zlib stream, another compressed or archive
format, text the check cannot tell from binary, or unknown binary. A zip is recognised only at offset 0; any other
stream that the zip reader would still open (a zip prefixed by, or appended to, other bytes) is refused. Nesting
deeper than `MAX_DEPTH`, a stream larger than `MAX_STREAM_BYTES`, an archive of more than `MAX_MEMBERS` members, and
decompressing more than `MAX_TOTAL_BYTES` for one file are refused too; when the total budget runs out the walk stops
at once, and no read asks for more than what is left of it. A file a package attests by hash (`[[opaque]]` in
package.toml) is never given to this module.

`views(stream)` is what the scans read in a stream. Text is read as it is and decoded: continuations joined and
backslash escapes (`\\xHH`, `\\uXXXX`, `\\UXXXXXXXX`) undone, HTML entities and percent-escapes undone, the strings of
a JSON or TOML file as parsed, and all of these together until nothing changes. A database or an archive's own bytes
are read as UTF-8 (leniently), as UTF-16 in both byte orders and alignments, and as raw bytes.
"""

import bz2
import contextlib
import gzip
import html
import io
import json
import lzma
import re
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import unquote

MAX_DEPTH = 4
MAX_STREAM_BYTES = 256 * 2**20
MAX_TOTAL_BYTES = 1024 * 2**20
MAX_MEMBERS = 100_000
TEXT, SQLITE, ARCHIVE = "text", "sqlite", "archive"
_DECODE_ROUNDS = 4
_SQLITE = b"SQLite format 3\x00"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_BOMS = (b"\xff\xfe", b"\xfe\xff")
_COMPRESSED = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}
_KNOWN = (  # kinds that are refused, named so the author knows what was found
    (b"%PDF", "PDF"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF8", "GIF"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"LZIP", "lzip"),
    (b"\x89LZO\x00\r\n\x1a\n", "lzop"),
    (b"\x1f\x9d", "compress"),
    (b"\x1f\xa0", "compress"),
    (b"\x5d\x00\x00", "lzma"),
    (b"MSCF", "cabinet"),
    (b"xar!", "xar"),
    (b"!<arch>\n", "ar"),
    (b"070701", "cpio"),
    (b"070702", "cpio"),
    (b"070707", "cpio"),
    (b"\xc7\x71", "cpio"),
    (b"\xed\xab\xee\xdb", "rpm"),
    (b"hsqs", "squashfs"),
)
_BACKSLASH = re.compile(r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2}))")
_CONTINUATION = re.compile(r"\\\r?\n")


@dataclass(frozen=True)
class Stream:
    """One stream of a file: where it is, its bytes, its kind, and its text when it is text."""

    label: str
    data: bytes
    kind: str
    text: str | None = None


class _Exhausted(Exception):
    """The total budget ran out at this stream; the walk stops."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


@dataclass
class _Budget:
    """What one file's walk has decompressed so far; shared by the whole recursion, so it is mutated on purpose."""

    total: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)  # (label, why), so a caller can redact the label


def _refuse(label: str, why: str) -> tuple[str, str]:
    return label, f"refused, {why}; only text, SQLite databases and archives of them can be checked"


def _read(stream: io.BufferedIOBase, label: str, budget: _Budget) -> bytes | None:
    """Read a stream within both caps; None (and a problem) when it is over the stream cap."""
    remaining = MAX_TOTAL_BYTES - budget.total
    if remaining <= 0:
        raise _Exhausted(label)
    limit = min(MAX_STREAM_BYTES, remaining)
    data = stream.read(limit + 1)
    budget.total += len(data)  # an oversized read counts too
    if len(data) <= limit:
        return data
    if limit < MAX_STREAM_BYTES:
        raise _Exhausted(label)
    budget.problems.append(_refuse(label, f"over the cap of {MAX_STREAM_BYTES} bytes"))
    return None


def _compression(data: bytes) -> str | None:
    if data.startswith(b"\x1f\x8b"):
        return "gzip"
    if data.startswith(b"BZh") and data[3:4].isdigit():
        return "bzip2"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    return None


def _is_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


def _zlib(data: bytes) -> bool:
    return len(data) > 2 and data[0] & 0x0F == 8 and data[0] >> 4 <= 7 and (data[0] * 256 + data[1]) % 31 == 0


def _text(data: bytes) -> str | None:
    """The text of UTF-8 without NUL bytes or of UTF-16 with a byte-order mark; None for anything else."""
    if data.startswith(_BOMS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _detected(data: bytes) -> str:
    """The kind of a refused stream, as far as it can be told."""
    named = next((kind for magic, kind in _KNOWN if data.startswith(magic)), None)
    if named:
        return f"{named} content"
    if _zlib(data):
        return "a raw zlib stream"
    if data.startswith(_BOMS) or b"\x00" not in data[:4096]:
        return "not text: neither UTF-8 without NUL bytes nor UTF-16 with a byte-order mark"
    return "unknown binary content, not text: neither UTF-8 without NUL bytes nor UTF-16 with a byte-order mark"


def _members(label: str, data: bytes, budget: _Budget) -> Iterator[tuple[str, bytes]]:
    """The members of a zip or tar archive, each read within the caps; a link's target is a member of its own."""
    if data.startswith(_ZIP):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                budget.problems.append(_refuse(label, f"more than {MAX_MEMBERS} members"))
                return
            if archive.comment:
                yield "(comment)", archive.comment
            for info in infos:
                if info.comment:
                    yield f"{info.filename}(comment)", info.comment
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    read = _read(member, f"{label}!{info.filename}", budget)
                if read is not None:
                    yield info.filename, read
        return
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        infos = archive.getmembers()
        if len(infos) > MAX_MEMBERS:
            budget.problems.append(_refuse(label, f"more than {MAX_MEMBERS} members"))
            return
        for info in infos:
            if info.isfile():
                handle = archive.extractfile(info)
                read = None if handle is None else _read(handle, f"{label}!{info.name}", budget)
                if read is not None:
                    yield info.name, read
            elif not info.isdir():
                yield info.name, info.linkname.encode("utf-8")


def _walk(label: str, data: bytes, depth: int, budget: _Budget) -> Iterator[Stream]:
    if not data.startswith(_ZIP) and zipfile.is_zipfile(io.BytesIO(data)):
        budget.problems.append(_refuse(label, "a zip archive that does not start at offset 0 (prefixed or appended)"))
        return
    text = _text(data)
    if text is not None:
        yield Stream(label, data, TEXT, text)
        return
    if data.startswith(_SQLITE):
        yield Stream(label, data, SQLITE)
        return
    compression = _compression(data)
    if compression is None and not data.startswith(_ZIP) and not _is_tar(data):
        budget.problems.append(_refuse(label, _detected(data)))
        return
    yield Stream(label, data, ARCHIVE)
    if depth >= MAX_DEPTH:
        budget.problems.append(_refuse(label, f"nested deeper than {MAX_DEPTH} levels"))
        return
    try:
        if compression:
            with _COMPRESSED[compression](io.BytesIO(data)) as stream:
                inner = _read(stream, f"{label}!({compression})", budget)
            if inner is not None:
                yield from _walk(f"{label}!({compression})", inner, depth + 1, budget)
            return
        for name, inner in _members(label, data, budget):
            yield from _walk(f"{label}!{name}", inner, depth + 1, budget)
    except _Exhausted:
        raise
    except (OSError, EOFError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, tarfile.TarError,
            lzma.LZMAError) as error:  # fmt: skip
        budget.problems.append(_refuse(label, f"it cannot be opened ({type(error).__name__})"))


def streams(path: str, data: bytes) -> tuple[list[Stream], list[tuple[str, str]]]:
    """Every stream of a file, and the problems that refuse it, each as (label, why); the walk stops when the total
    budget runs out."""
    budget = _Budget()
    found: list[Stream] = []
    try:
        for stream in _walk(path, data, 0, budget):
            found.append(stream)
    except _Exhausted as exhausted:
        budget.problems.append(
            _refuse(exhausted.label, f"the file decompresses to over {MAX_TOTAL_BYTES} bytes in total; scan stopped")
        )
    return found, budget.problems


def _byte_views(data: bytes) -> list[tuple[str, str]]:
    views = [
        ("utf-8", data.decode("utf-8", errors="replace")),
        ("utf-16le", data.decode("utf-16-le", errors="replace")),
        ("utf-16le+1", data[1:].decode("utf-16-le", errors="replace")),
        ("utf-16be", data.decode("utf-16-be", errors="replace")),
        ("utf-16be+1", data[1:].decode("utf-16-be", errors="replace")),
        ("bytes", data.decode("latin-1")),
    ]
    return [(name, text.replace("\0", "\n")) for name, text in views]


def _strings(value: object) -> Iterator[str]:
    """Every key and string of parsed data."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _backslash(text: str) -> str:
    def char(match: re.Match[str]) -> str:
        code = int(next(group for group in match.groups() if group), 16)
        return chr(code) if code <= 0x10FFFF else match.group(0)

    return _BACKSLASH.sub(char, text)


def normalise(text: str) -> str:
    """Continuations joined and backslash escapes decoded."""
    return _backslash(_CONTINUATION.sub("", text))


PARSED = ("json", "toml")  # views without the file's own lines


def _text_views(label: str, text: str) -> list[tuple[str, str]]:
    views = [("text", text), ("normalised", normalise(text))]
    for parser, kind in ((json.loads, "json"), (tomllib.loads, "toml")):
        if kind == "json" or label.lower().endswith(".toml"):
            with contextlib.suppress(ValueError, RecursionError):  # not that format: the other views still apply
                views.append((kind, "\n".join(_strings(parser(text)))))
    views += [("entities", html.unescape(text)), ("percent", unquote(text))]
    combined = text
    for _ in range(_DECODE_ROUNDS):
        decoded = normalise(unquote(html.unescape(combined)))
        if decoded == combined:
            break
        combined = decoded
    return [*views, ("decoded", combined)]


def views(stream: Stream) -> list[tuple[str, str]]:
    """What the scans read in one stream: text and its decodings, or a database's or archive's bytes as text."""
    if stream.text is not None:
        return _text_views(stream.label, stream.text)
    return _byte_views(stream.data)
