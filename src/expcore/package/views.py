# Provenance: new
"""What a package's checks read in a file, failing closed: only kinds of content they can read in full are allowed.

Threat model: the checks catch accidental leaks of private names in text and in its common encodings. They do not
defend against deliberate concealment, and they never read text inside images.

`streams(path, data)` classifies a file and everything inside it, and stops at the first refusal. A stream may be:
- text: valid UTF-8 without NUL bytes, or UTF-16 with a byte-order mark, and not a PDF or PostScript file (`%PDF-`,
  `%!PS`), which are refused even when they are pure ASCII. Every base64 run (either alphabet, padding optional, also
  wrapped over lines as in a `data:` URI) and every hex run of 16 characters or more is decoded (runs are found in
  one linear pass), and what it decodes to is classified by `_walk_decoded`: text is read as text, compressed bytes
  are opened, and bytes of no recognisable kind (a digest, an identifier that only looks like base64) are read for
  their printable strings and refuse nothing. A decoded run that is not text and has a format magic of four bytes or
  more, or a long run that looks compressed and fails to decompress, refuses the file;
- an SQLite database (its magic header): its bytes are read raw, and the database is deserialised into memory,
  read-only, and every value read: the schema's SQL and the TEXT values of each column as text, every BLOB as a byte
  stream under the same allow list, a zlib BLOB decompressed;
- a gzip, bzip2, xz, zip or tar archive, whose members are walked in turn; its own bytes are read as well (a gzip
  header's file name, a zip comment).
Anything else is refused, named with the kind detected: a PDF, a PNG, a raw zlib stream, another compressed or archive
format, text the check cannot tell from binary, or unknown binary. A zip is recognised only at offset 0; any other
stream that the zip reader would still open (a zip prefixed by, or appended to, other bytes) is refused. A member that
fails to decompress (a bad CRC, a stream cut short) refuses the file at once. Nesting deeper than `MAX_DEPTH`, a
stream larger than `MAX_STREAM_BYTES`, an archive of more than `MAX_MEMBERS` members, and decompressing more than
`MAX_TOTAL_BYTES` for one file are refused too; every byte a decompressor produces counts against that total budget,
the walk stops the moment it runs out, and no read asks for more than what is left of it. A file a package attests by
hash (`[[opaque]]` in package.toml) is never given to this module.

`views(stream)` is what the scans read in a stream. Text is read as it is and decoded: continuations (a backslash, the
newline and the indentation after it) joined and backslash escapes (`\\xHH`, `\\uXXXX`, `\\UXXXXXXXX`) undone, HTML
entities and percent-escapes undone, the strings of a JSON or TOML file as parsed, and all of these together until
nothing changes; identical views are read once. A database's or an archive's own bytes are read as UTF-8
(leniently), as UTF-16 in both byte orders and alignments, and as raw bytes; other bytes (a decoded run, a compressed
blob) for their strings of name characters, in UTF-8 and UTF-16.
"""

import base64
import binascii
import bz2
import contextlib
import gzip
import hashlib
import html
import io
import json
import lzma
import re
import sqlite3
import tarfile
import tomllib
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import NoReturn
from urllib.parse import unquote

MAX_DEPTH = 4
MAX_STREAM_BYTES = 256 * 2**20
MAX_TOTAL_BYTES = 1024 * 2**20
MAX_MEMBERS = 100_000
TEXT, SQLITE, ARCHIVE, BYTES = "text", "sqlite", "archive", "bytes"
FILE, BLOB, DECODED = "file", "blob", "decoded"  # where a stream came from: its rules differ (see `_walk`)
_CHUNK = 2**20
_DECODE_ROUNDS = 4
_SQLITE = b"SQLite format 3\x00"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_BOMS = (b"\xff\xfe", b"\xfe\xff")
_TEXT_SIGNATURES = ((b"%PDF-", "PDF"), (b"%!PS", "PostScript"))
_COMPRESSED = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}
_KNOWN = (  # kinds that are refused, named so the author knows what was found; strong ones are four bytes or more
    (b"%PDF", "PDF"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF8", "GIF"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"LZIP", "lzip"),
    (b"\x89LZO\x00\r\n\x1a\n", "lzop"),
    (b"MSCF", "cabinet"),
    (b"xar!", "xar"),
    (b"!<arch>\n", "ar"),
    (b"070701", "cpio"),
    (b"070702", "cpio"),
    (b"070707", "cpio"),
    (b"\xed\xab\xee\xdb", "rpm"),
    (b"hsqs", "squashfs"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x1f\x9d", "compress"),
    (b"\x1f\xa0", "compress"),
    (b"\x5d\x00\x00", "lzma"),
    (b"\xc7\x71", "cpio"),
)
_BACKSLASH = re.compile(r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2}))")
_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")
MIN_RUN = 16  # base64 and hex runs from this many characters are decoded
MAX_HEADINGS = 8  # the most leading lines a joined run is also decoded without
LONG_RUN = 40  # a run this long that looks compressed but fails to decompress refuses its file
_SEGMENT = re.compile(r"[A-Za-z0-9+/_-]+(?:={1,2})?")  # one maximal run of the base64 alphabet; no backtracking
_LINE_BREAK = re.compile(r"[ \t]*\r?\n[ \t]*")  # what may join two lines of one wrapped run
_HEX_SUB = re.compile(r"[0-9a-fA-F]{16,}")
# The strings of bytes that are probably random, as `strings` finds them but only of the characters a name can be
# made of (letters, digits, `_ . -`, a space, and any non-ASCII letter in UTF-8), in UTF-8 and in UTF-16 either way.
_UTF8_STRINGS = re.compile(rb"(?:[A-Za-z0-9_.\- ]|[\xc2-\xf4][\x80-\xbf]{1,3}){4,}")
_UTF16LE_STRINGS = re.compile(rb"(?:[A-Za-z0-9_.\- ]\x00){4,}")
_UTF16BE_STRINGS = re.compile(rb"(?:\x00[A-Za-z0-9_.\- ]){4,}")
_ERRORS = (OSError, EOFError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, tarfile.TarError,
           lzma.LZMAError, zlib.error, sqlite3.Error)  # fmt: skip


@dataclass(frozen=True)
class Stream:
    """One stream of a file: where it is, its bytes, its kind, and its text when it is text."""

    label: str
    data: bytes
    kind: str
    text: str | None = None


class _Refused(Exception):
    """A stream the checks cannot read; the walk of the file stops."""

    def __init__(self, label: str, why: str) -> None:
        super().__init__(label)
        self.label, self.why = label, why


class _Broken(_Refused):
    """A stream that fails to decompress (a bad CRC, a stream cut short)."""


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


def _refuse(label: str, why: str) -> NoReturn:
    raise _Refused(label, f"refused, {why}; only text, SQLite databases and archives of them can be checked")


def _spend(budget: _Budget, label: str, size: int) -> None:
    budget.total += size
    if budget.total > MAX_TOTAL_BYTES:
        raise _Exhausted(label)


def _read(stream: io.BufferedIOBase, label: str, budget: _Budget) -> bytes:
    """Read a stream in chunks within both caps, counting every byte as it comes; refuse on a decompression error."""
    remaining = MAX_TOTAL_BYTES - budget.total
    if remaining <= 0:
        raise _Exhausted(label)
    limit = min(MAX_STREAM_BYTES, remaining)
    parts, size = [], 0
    try:
        while size <= limit:
            chunk = stream.read(min(_CHUNK, limit + 1 - size))
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            budget.total += len(chunk)  # counted as produced, an oversized or failing read included
    except _ERRORS as error:
        raise _Broken(label, f"refused, it fails to decompress ({type(error).__name__})") from None
    if size <= limit:
        return b"".join(parts)
    if limit < MAX_STREAM_BYTES:
        raise _Exhausted(label)
    _refuse(label, f"over the cap of {MAX_STREAM_BYTES} bytes")


def _inflate(data: bytes, label: str, budget: _Budget) -> bytes:
    """Decompress a zlib stream within both caps."""
    remaining = MAX_TOTAL_BYTES - budget.total
    if remaining <= 0:
        raise _Exhausted(label)
    limit = min(MAX_STREAM_BYTES, remaining)
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        # Deflate data whose trailing check fails is a broken stream, refused like one cut short. It is told from data
        # that is not deflate at all by decompressing without the four-byte trailer: genuine deflate ends exactly there.
        probe = zlib.decompressobj(-zlib.MAX_WBITS)  # raw deflate after the two-byte header, to its final block
        try:
            partial = probe.decompress(data[2:], limit + 1)
        except zlib.error:
            partial = b""
        budget.total += len(partial)
        trailer = probe.unused_data[:4]
        if partial and probe.eof and len(trailer) == 4 and int.from_bytes(trailer, "big") != zlib.adler32(partial):
            raise _Broken(label, "refused, it fails to decompress (a zlib stream whose check fails)") from None
        raise
    budget.total += len(out)
    if len(out) > limit:
        if limit < MAX_STREAM_BYTES:
            raise _Exhausted(label)
        _refuse(label, f"over the cap of {MAX_STREAM_BYTES} bytes")
    if not inflater.eof:  # cut short, as opposed to data that is not deflate at all, which raises zlib.error
        raise _Broken(label, "refused, it fails to decompress (a zlib stream cut short)")
    return out


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


def _known(data: bytes, strong_only: bool) -> str | None:
    """A refused kind recognised by its magic bytes; with `strong_only`, only magics of four bytes or more count."""
    counted = [(magic, kind) for magic, kind in _KNOWN if len(magic) >= 4 or not strong_only]
    return next((kind for magic, kind in counted if data.startswith(magic)), None)


def _detected(data: bytes) -> str:
    """The kind of a refused stream, as far as it can be told."""
    named = _known(data, strong_only=False)
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
                _refuse(label, f"more than {MAX_MEMBERS} members")
            if archive.comment:
                yield "(comment)", archive.comment
            for info in infos:
                if info.comment:
                    yield f"{info.filename}(comment)", info.comment
                if not info.is_dir():
                    with archive.open(info) as member:
                        yield info.filename, _read(member, f"{label}!{info.filename}", budget)
        return
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        infos = archive.getmembers()
        if len(infos) > MAX_MEMBERS:
            _refuse(label, f"more than {MAX_MEMBERS} members")
        for info in infos:
            if info.isfile():
                handle = archive.extractfile(info)
                if handle is not None:
                    yield info.name, _read(handle, f"{label}!{info.name}", budget)
            elif not info.isdir():
                yield info.name, info.linkname.encode("utf-8")


def _decode_base64(run: str) -> bytes | None:
    run = re.sub(r"\s+", "", run).rstrip("=").replace("-", "+").replace("_", "/")
    run = run[: len(run) - 1] if len(run) % 4 == 1 else run
    with contextlib.suppress(binascii.Error, ValueError):
        return base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
    return None


def _runs(text: str) -> Iterator[tuple[int, str]]:
    """Maximal runs of the base64 alphabet, in one linear pass; a run wrapped over lines (single line breaks, each
    after a line without padding) is joined into one. A joined run is also given from each line whose width differs
    from the line before it, up to `MAX_HEADINGS` (caption lines above an encoded block would shift its alignment)."""

    def flush(parts: list[tuple[int, str]]) -> Iterator[tuple[int, str]]:
        yield parts[0][0], "".join(part for _, part in parts)
        # Also from every line whose width differs from the line before it, up to MAX_HEADINGS of them: the lines of
        # a wrapped block are all one width, so a plain block is decoded once, and caption lines above an encoded
        # block (each of its own width) do not shift its alignment.
        skips = [k for k in range(1, len(parts)) if len(parts[k - 1][1]) != len(parts[k][1])][:MAX_HEADINGS]
        for skip in skips:
            yield parts[skip][0], "".join(part for _, part in parts[skip:])

    parts: list[tuple[int, str]] = []
    end = 0
    for match in _SEGMENT.finditer(text):
        if parts and not parts[-1][1].endswith("=") and _LINE_BREAK.fullmatch(text, end, match.start()):
            parts.append((match.start(), match.group(0)))
        else:
            if parts:
                yield from flush(parts)
            parts = [(match.start(), match.group(0))]
        end = match.end()
    if parts:
        yield from flush(parts)


def payloads(text: str) -> Iterator[tuple[str, bytes, bool]]:
    """Every base64 run and every hex run of `MIN_RUN` characters or more, decoded, once each: (what it was, the
    bytes, whether the run was long). Linear in the text."""
    seen: dict[bytes, bool] = {}
    for start, run in _runs(text):
        found = []
        if len(run) >= MIN_RUN:
            found.append((f"base64@{start}", _decode_base64(run), len(run) >= LONG_RUN))
        for match in _HEX_SUB.finditer(run):
            digits = match.group(0)
            found.append(
                (f"hex@{start + match.start()}", bytes.fromhex(digits[: len(digits) // 2 * 2]), len(digits) >= LONG_RUN)
            )
        for where, decoded, long in found:
            if decoded and decoded not in seen:
                seen[decoded] = long
                yield where, decoded, long


def _texts_of(text: str) -> list[str]:
    """The text, its decoded form and its parsed JSON strings: where an encoded payload may be written."""
    forms = [text, _decoded(text)]
    with contextlib.suppress(ValueError, RecursionError):
        forms.append("\n".join(_strings(json.loads(text))))
    return list(dict.fromkeys(forms))


def _walk_text(label: str, data: bytes, text: str, depth: int, budget: _Budget) -> Iterator[Stream]:
    yield Stream(label, data, TEXT, text)
    for form in _texts_of(text):
        for where, decoded, long in payloads(form):
            _spend(budget, label, len(decoded))
            yield from _walk_decoded(f"{label}!({where})", decoded, depth + 1, budget, strict=long)


def _walk_decoded(label: str, data: bytes, depth: int, budget: _Budget, *, strict: bool) -> Iterator[Stream]:
    """Classify the bytes a base64 or hex run decodes to.

    Most runs are digests, identifiers or words, not encodings, so what has no recognisable kind is only bytes, read
    raw, and refuses nothing. Text is read as text, never refused for a signature (a sentence about `%PDF-` is
    text). Bytes that are not text are refused for a format magic of four bytes or more. Compressed bytes (a gzip,
    bzip2 or xz magic, a zip or tar archive, or a zlib header) are opened; when a long run (`strict`) fails to
    decompress it refuses the file as a failing archive member does, except a zlib header followed by data that is
    not deflate at all, which one random run in a thousand shows by chance: those bytes are read raw. A short run is
    never refused for a failure. Below `MAX_DEPTH`, text is read but not decoded further, and anything to open is
    refused when strict and read raw when not.
    """
    text = _text(data)
    if text is not None:
        if depth > MAX_DEPTH:
            yield Stream(label, data, TEXT, text)
            return
        yield from _walk_text(label, data, text, depth, budget)
        return
    known = _known(data, strong_only=True)
    if known:
        _refuse(label, f"{known} content")
    compression = _compression(data)
    if compression == "gzip" and data[2:3] != b"\x08":  # a gzip header names deflate; two bytes alone are chance
        compression = None
    # an encoded SQLite database is opened as one, like a file: its compressed or escaped values are read
    archive = compression is not None or data.startswith(_ZIP) or data.startswith(_SQLITE) or _is_tar(data)
    if not archive and not _zlib(data):
        yield Stream(label, data, BYTES)
        return
    if depth > MAX_DEPTH:
        if strict:
            _refuse(label, f"nested deeper than {MAX_DEPTH} levels")
        yield Stream(label, data, BYTES)
        return
    try:
        yield from _walk_binary(label, data, depth, budget, DECODED, compression, archive, strict=strict)
    except _Broken:
        if strict:
            raise
        yield Stream(label, data, BYTES)
    except (_Refused, _Exhausted):
        raise
    except _ERRORS as error:
        if strict and archive:
            _refuse(label, f"it fails to decompress ({type(error).__name__})")
        yield Stream(label, data, BYTES)


def _sqlite_columns(data: bytes) -> Iterator[tuple[str, str | bytes]]:
    """The schema's SQL, the TEXT values of each column joined as one text, and every BLOB, of a deserialised copy."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA query_only = ON")
        schema = connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        yield "(schema)", "\n".join(f"{kind} {name}: {sql or ''}" for kind, name, sql in schema)
        for kind, name, _ in schema:
            if kind != "table":
                continue
            cursor = connection.execute('SELECT * FROM "{}"'.format(name.replace('"', '""')))
            columns = [description[0] for description in cursor.description]
            texts: dict[str, list[str]] = {column: [] for column in columns}
            for number, row in enumerate(cursor, start=1):
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, str):
                        texts[column].append(value)
                    elif isinstance(value, bytes):
                        yield f"{name}.{column}#{number}", value
            for column, values in texts.items():
                if values:
                    yield f"{name}.{column}", "\n".join(values)
    finally:
        connection.close()


def _walk(label: str, data: bytes, depth: int, budget: _Budget, origin: str = FILE) -> Iterator[Stream]:
    """Classify one stream and walk what it holds (see the module docstring).

    `origin` says where the stream came from: a FILE (a file or an archive member) or a BLOB is refused when it is of
    no allowed kind, and a BLOB that is a zlib stream is decompressed. A decoded run is `_walk_decoded`'s.
    """
    deep = depth > MAX_DEPTH
    if not data.startswith(_ZIP) and zipfile.is_zipfile(io.BytesIO(data)):
        _refuse(label, "a zip archive that does not start at offset 0 (prefixed or appended)")
    signature = next((kind for magic, kind in _TEXT_SIGNATURES if data.startswith(magic)), None)
    if signature:
        _refuse(label, f"{signature} content")
    text = _text(data)
    compression = _compression(data)
    archive = compression is not None or data.startswith(_ZIP) or _is_tar(data)
    known = _known(data, strong_only=False)
    if known and text is None:
        _refuse(label, f"{known} content")
    if deep:
        _refuse(label, f"nested deeper than {MAX_DEPTH} levels")
    if text is not None:
        yield from _walk_text(label, data, text, depth, budget)
        return
    try:
        yield from _walk_binary(label, data, depth, budget, origin, compression, archive)
    except (_Refused, _Exhausted):
        raise
    except _ERRORS as error:
        _refuse(label, f"it cannot be opened ({type(error).__name__})")


def _walk_binary(
    label: str,
    data: bytes,
    depth: int,
    budget: _Budget,
    origin: str,
    compression: str | None,
    archive: bool,
    *,
    strict: bool = True,
) -> Iterator[Stream]:
    if data.startswith(_SQLITE):
        yield Stream(label, data, SQLITE)
        for where, value in list(_sqlite_columns(data)):
            if isinstance(value, str):
                yield from _walk_text(f"{label}!{where}", value.encode("utf-8", "replace"), value, depth + 1, budget)
            else:
                yield from _walk(f"{label}!{where}", value, depth + 1, budget, BLOB)
        return
    if not archive and _zlib(data) and origin != FILE:
        inner = _inflate(data, f"{label}!(zlib)", budget)
        yield Stream(label, data, BYTES)
        if origin == DECODED:
            yield from _walk_decoded(f"{label}!(zlib)", inner, depth + 1, budget, strict=strict)
        else:
            yield from _walk(f"{label}!(zlib)", inner, depth + 1, budget, origin)
        return
    if not archive:
        _refuse(label, _detected(data))
    yield Stream(label, data, ARCHIVE)
    if compression:
        with _COMPRESSED[compression](io.BytesIO(data)) as stream:
            inner = _read(stream, f"{label}!({compression})", budget)
        yield from _walk(f"{label}!({compression})", inner, depth + 1, budget, FILE)
        return
    for name, inner in _members(label, data, budget):
        yield from _walk(f"{label}!{name}", inner, depth + 1, budget, FILE)


def streams(path: str, data: bytes) -> tuple[list[Stream], list[tuple[str, str]]]:
    """Every stream of a file up to the first refusal, and that refusal as (label, why) if there is one."""
    budget = _Budget()
    found: list[Stream] = []
    try:
        for stream in _walk(path, data, 0, budget):
            found.append(stream)
    except _Refused as refused:
        budget.problems.append((refused.label, refused.why))
    except _Exhausted as exhausted:
        why = f"refused, the file decompresses to over {MAX_TOTAL_BYTES} bytes in total; scan stopped"
        budget.problems.append((exhausted.label, why))
    return found, budget.problems


def hidden_name(position: int, label: str) -> str:
    """How a finding names a stream whose label must not be shown: the file's place in the package and the start of
    the label's SHA-256, so that two such findings are never folded into one and the author can find each."""
    return f"file {position} (path sha256 {hashlib.sha256(label.encode('utf-8')).hexdigest()[:12]})"


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
    """Continuations joined (with the indentation after them) and backslash escapes decoded."""
    return _backslash(_CONTINUATION.sub("", text))


def _decoded(text: str) -> str:
    """Entities, percent-escapes, continuations and backslash escapes undone, again and again until nothing changes."""
    combined = text
    for _ in range(_DECODE_ROUNDS):
        decoded = normalise(unquote(html.unescape(combined)))
        if decoded == combined:
            break
        combined = decoded
    return combined


PARSED = ("json", "toml")  # views without the file's own lines


def _text_views(label: str, text: str) -> list[tuple[str, str]]:
    views = [("text", text), ("normalised", normalise(text))]
    for parser, kind in ((json.loads, "json"), (tomllib.loads, "toml")):
        if kind == "json" or label.lower().endswith(".toml"):
            with contextlib.suppress(ValueError, RecursionError):  # not that format: the other views still apply
                views.append((kind, "\n".join(_strings(parser(text)))))
    views += [("entities", html.unescape(text)), ("percent", unquote(text))]
    return [*views, ("decoded", _decoded(text))]


def _strings_views(data: bytes) -> list[tuple[str, str]]:
    """The printable strings of bytes that are probably random (a decoded run of no recognisable kind)."""
    return [
        ("strings", "\n".join(m.group(0).decode("utf-8", "replace") for m in _UTF8_STRINGS.finditer(data))),
        ("strings-utf16le", "\n".join(m.group(0).decode("utf-16-le") for m in _UTF16LE_STRINGS.finditer(data))),
        ("strings-utf16be", "\n".join(m.group(0).decode("utf-16-be") for m in _UTF16BE_STRINGS.finditer(data))),
    ]


def views(stream: Stream) -> list[tuple[str, str]]:
    """What the scans read in one stream, each distinct view once: text and its decodings; a database's or an
    archive's bytes under every encoding; the printable strings of other bytes (a decoded run, a compressed blob)."""
    if stream.text is not None:
        found = _text_views(stream.label, stream.text)
    elif stream.kind == BYTES:
        found = _strings_views(stream.data)
    else:
        found = _byte_views(stream.data)
    distinct: dict[str, str] = {}
    for name, text in found:
        if text not in distinct.values() or name in PARSED:
            distinct.setdefault(name, text)
    return list(distinct.items())
