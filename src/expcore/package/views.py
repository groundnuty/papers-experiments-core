# Provenance: new
"""What a package's checks read in a file: every byte stream it holds, each under several text views.

- `streams(path, data)` returns the file's bytes and, recursively, what gzip, bzip2 and xz content decompresses to and
  what the members of zip and tar archives hold, each labelled `path!member` (a member's own name is part of its
  label, so the label is checked as a path). Content is recognised by its magic bytes, whatever the file is called.
  Another compressed or archive format recognised by its magic bytes (zstd, 7z, rar, lz4, lzip, lzop, compress,
  lzma, cabinet, xar, ar, cpio, rpm, squashfs), an encrypted or unreadable member, a stream larger than
  `MAX_STREAM_BYTES`, more than `MAX_TOTAL_BYTES` in all, or nesting deeper than `MAX_DEPTH` is not read in part:
  it is returned as a problem, and the build refuses the package as unscannable.
- `byte_views(data)` decodes one stream as UTF-8 (leniently), as UTF-16 little- and big-endian at both byte
  alignments, and as raw bytes (Latin-1, one character per byte), so a name in any of those encodings is text to the
  scanner.
- `decoded_views(name, text)` undoes what hides a name in text without changing it: the strings of a JSON or TOML
  file as parsed, HTML entities, percent-escapes and backslash escapes (`\\uXXXX`, `\\UXXXXXXXX`, `\\xXX`), each alone
  and all of them together until nothing changes.

PDF and PNG files are read as raw bytes only: their own compressed streams are not opened.
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
_DECODE_ROUNDS = 4
_COMPRESSED = (
    (b"\x1f\x8b", "gzip", gzip.open),
    (b"BZh", "bzip2", bz2.open),
    (b"\xfd7zXZ\x00", "xz", lzma.open),
)
_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_UNSCANNABLE = (
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


@dataclass
class _Budget:
    """What has been decompressed so far; shared by one file's whole recursion, so it is mutated on purpose."""

    total: int = 0
    problems: list[str] = field(default_factory=list)


def _capped(stream: io.BufferedIOBase, label: str, budget: _Budget) -> bytes | None:
    """Read a stream up to the cap; None (and a problem) when it is larger, or the total is spent."""
    data = stream.read(MAX_STREAM_BYTES + 1)
    if len(data) > MAX_STREAM_BYTES:
        budget.problems.append(f"{label}: unscannable, over the cap of {MAX_STREAM_BYTES} bytes")
        return None
    budget.total += len(data)
    if budget.total > MAX_TOTAL_BYTES:
        budget.problems.append(f"{label}: unscannable, over the total cap of {MAX_TOTAL_BYTES} bytes")
        return None
    return data


def _is_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar"


def _members(label: str, data: bytes, budget: _Budget) -> Iterator[tuple[str, bytes]]:
    """The members of a zip or tar archive, each read under the cap."""
    if data.startswith(_ZIP):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    read = _capped(member, f"{label}!{info.filename}", budget)
                if read is not None:
                    yield info.filename, read
        return
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for info in archive.getmembers():
            if info.isfile():
                handle = archive.extractfile(info)
                read = None if handle is None else _capped(handle, f"{label}!{info.name}", budget)
                if read is not None:
                    yield info.name, read
            elif not info.isdir():
                yield info.name, info.linkname.encode("utf-8")  # a link or a device: its name and target are text


def _walk(label: str, data: bytes, depth: int, budget: _Budget) -> Iterator[tuple[str, bytes]]:
    yield label, data
    unscannable = next((kind for magic, kind in _UNSCANNABLE if data.startswith(magic)), None)
    if unscannable:
        budget.problems.append(f"{label}: unscannable {unscannable} content; a package holds only what its checks read")
        return
    compressed = next(((kind, opener) for magic, kind, opener in _COMPRESSED if data.startswith(magic)), None)
    if compressed and compressed[0] == "bzip2" and not data[3:4].isdigit():
        compressed = None  # "BZh" opens bzip2 only with its block size, 1 to 9, after it
    archive = data.startswith(_ZIP) or _is_tar(data)
    if not compressed and not archive:
        return
    if depth >= MAX_DEPTH:
        budget.problems.append(f"{label}: unscannable, nested deeper than {MAX_DEPTH} levels")
        return
    try:
        if compressed:
            kind, opener = compressed
            with opener(io.BytesIO(data)) as stream:
                inner = _capped(stream, f"{label}!({kind})", budget)
            if inner is not None:
                yield from _walk(f"{label}!({kind})", inner, depth + 1, budget)
            return
        for name, inner in list(_members(label, data, budget)):
            yield from _walk(f"{label}!{name}", inner, depth + 1, budget)
    except (OSError, EOFError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, tarfile.TarError,
            lzma.LZMAError) as error:  # fmt: skip
        budget.problems.append(f"{label}: unscannable, cannot be opened ({type(error).__name__})")


def streams(path: str, data: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Every byte stream of a file with its label, and the problems that make part of it unscannable."""
    budget = _Budget()
    found = list(_walk(path, data, 0, budget))
    return found, budget.problems


def byte_views(data: bytes) -> list[tuple[str, str]]:
    """One stream as text under every encoding the checks read, NUL bytes as line breaks."""
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


def decoded_views(name: str, text: str) -> list[tuple[str, str]]:
    """The text with what can hide a name undone: parsed strings, entities, percent- and backslash escapes."""
    views = []
    for parser, kind in ((json.loads, "json"), (tomllib.loads, "toml")):
        if name.lower().endswith(f".{kind}") or kind == "json":
            with contextlib.suppress(ValueError, RecursionError):  # not that format: its other views still apply
                views.append((kind, "\n".join(_strings(parser(text)))))
    views += [("entities", html.unescape(text)), ("percent", unquote(text)), ("backslash", _backslash(text))]
    combined = text
    for _ in range(_DECODE_ROUNDS):
        decoded = _backslash(unquote(html.unescape(combined)))
        if decoded == combined:
            break
        combined = decoded
    return [*views, ("decoded", combined)]
