# Provenance: new
"""The walk for copies of a declared key file: every stream of a packaged file or a wheel member, to the last one.

A package must never hold a copy of a key file its study's models.toml declares (K13). The content scanner
(`views.streams`) cannot be what finds one: it stops at the first stream it refuses (a PNG in a zip, a base64 run
that decodes to one), and an attested file is not given to it at all. This walk is its own, and nothing cuts it short
but its limits: no kind of content is refused, and an attestation changes nothing.

Each stream is compared by size and then SHA-256 with each key file (`KeyFinders.is_key_file`) and its bytes are
searched for each key file's content as a substring (`KeyFinders.holds_key_file`: the key as it is, and with the white
space around it stripped, since a copy in a note usually lacks a key file's trailing newline), so a key embedded
uncompressed is found in a container of any format the walk does not open (a tar in the v7 format, a tar behind junk, a
write-ahead log, a page deleted from a database). That search is made on a key of at least 16 bytes only; a shorter key
would occur by chance, and is compared by whole stream alone. What a stream holds is walked in turn, read as the scanner
reads it (its classifiers are shared): the members of a zip (at offset 0, or wherever the zip reader finds one) or a
tar, and their comments; what a gzip, bzip2, xz or zlib stream decompresses to; each value of an SQLite database, in WAL
mode or not; and in text, every base64 and hex run, decoded as the scanner decodes it (`views.payloads`). A tar member
is read whenever the reader gives a stream for it, whatever its type, links aside (GNU tar extracts one of an unknown
type as a file); a tar with bytes after its last member that are not the zero blocks ending it is unchecked, because the
reader stops at a header it cannot read where GNU tar goes on to the next. A stream of any other kind (a PNG, a PDF, a
compiled binary) is compared and searched as the bytes it is. A stream that holds the key but contains a stream that is
a copy too (a stored zip and its member) is named by the inner one only.

The limits are the scanner's, for each packaged file and each wheel member alone: containers nested `MAX_DEPTH` deep
(a decoded run is not a level of its own: each one is shorter than the text it is in, so runs inside runs end on their
own); `MAX_MEMBERS` archive members in all; `MAX_STREAM_BYTES` for one stream and `MAX_TOTAL_BYTES` in all,
decompressed or decoded. When the walk cannot finish, at a limit or at a member it cannot read (encrypted, damaged,
compressed in a way the reader does not know), it says so (`Walk.unchecked`), and the build refuses: never a silent
pass. Only what looks like a container by chance is let be: a decoded run (most are digests and identifiers, not
encodings) or a stream with a zlib header or a zip's end record alone, which fails to open, is compared as the bytes
it is. Nothing here prints a digest or a key.
"""

import io
import itertools
import sqlite3
import tarfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from expcore.config.key_locations import KeyFinders
from expcore.package import views
from expcore.package.views import (
    _COMPRESSED,
    _ERRORS,
    _SQLITE,
    _ZIP,
    MIN_RUN,
    _Broken,
    _Budget,
    _Exhausted,
    _inflate,
    _is_tar,
    _read,
    _Refused,
    _spend,
    _text,
    _texts_of,
    _zlib,
    payloads,
)

_ZIP_END = b"PK\x05\x06"
_ZIP_TAIL = 2**16 + 22  # where a zip's end record can be: its own 22 bytes after a comment of up to 64 KiB
_ENCRYPTED = 0x1  # a zip member's flag bit
_WEAK = frozenset({"zlib", "zip end record"})  # signatures random bytes show by chance


@dataclass
class Walk:
    """What one walk found: the labels of the streams that are copies of a key file, and (label, why) for each stream
    it could not check. Filled as the walk goes, on purpose."""

    copies: list[str] = field(default_factory=list)
    unchecked: list[tuple[str, str]] = field(default_factory=list)

    def labels(self) -> set[str]:
        return {*self.copies, *(label for label, _ in self.unchecked)}


class _Unopened(Exception):
    """A container that fails to open; at one of the walk's limits (`limit`), whatever it looked like."""

    def __init__(self, error: BaseException | str, *, limit: bool = False) -> None:
        why = error if isinstance(error, str) else f"it cannot be read ({type(error).__name__})"
        super().__init__(why)
        self.why, self.limit = why, limit


@dataclass
class _State:
    locations: KeyFinders
    found: Walk
    budget: _Budget = field(default_factory=_Budget)
    members: int = 0

    def visit(self, label: str, data: bytes, depth: int, *, strict: bool) -> None:
        """Compare one stream, then walk what it holds. `strict` is False for a decoded run, which fails to open as
        an archive only by chance, so it is then compared and let be.

        A stream is a copy when it is a key file by size and SHA-256, or, failing that, when it holds a key file's
        content anywhere (`KeyFinders.holds_key_file`): the one search of its bytes, whatever they are, finds the key
        in a container the walk does not open (a tar with no "ustar" magic, a write-ahead log, a deleted database
        page). A stored zip holds its members' bytes as they are, so what holds a copy that a member of it was named
        for is named by the member alone: the stream is the copy only when no stream inside it was."""
        copy = self.locations.is_key_file(data)
        if copy:
            self.found.copies.append(label)
        before = len(self.found.copies)
        self._walk(label, data, depth, strict=strict)
        if not copy and len(self.found.copies) == before and self.locations.holds_key_file(data):
            self.found.copies.append(label)

    def _walk(self, label: str, data: bytes, depth: int, *, strict: bool) -> None:
        """Walk what one stream holds, as `visit` does."""
        text = _text(data)
        if text is not None:
            self._runs(label, text, depth)
            return
        kind = _container(data)
        if kind is not None:
            self._open(label, data, kind, depth, strict=strict and kind not in _WEAK)

    def _runs(self, label: str, text: str, depth: int) -> None:
        """Every base64 and hex run of the text, decoded as the scanner decodes it, once per run."""
        if len(text) < MIN_RUN:
            return
        seen: dict[bytes, int] = {}
        for form in _texts_of(text):
            for where, decoded, _ in payloads(form, seen):
                _spend(self.budget, label, len(decoded))
                self.visit(f"{label}!({where})", decoded, depth, strict=False)

    def _open(self, label: str, data: bytes, kind: str, depth: int, *, strict: bool) -> None:
        """Walk the streams a container holds, one level deeper; one that cannot be opened is unchecked when it is
        surely a container (`strict`), and only the bytes it is otherwise."""
        inside = self._inside(label, data, kind)
        try:
            first = next(inside, None)
        except _Unopened as unopened:
            if strict or unopened.limit:
                self.found.unchecked.append((label, unopened.why))
            return
        if first is None:
            return
        if depth >= views.MAX_DEPTH:
            self.found.unchecked.append((label, f"archives nested deeper than {views.MAX_DEPTH} levels"))
            return
        for child, held, why in itertools.chain([first], inside):
            if held is None:
                self.found.unchecked.append((child, why))
            else:
                self.visit(child, held, depth + 1, strict=True)

    def _inside(self, label: str, data: bytes, kind: str) -> Iterator[tuple[str, bytes | None, str]]:
        """(label, bytes, "") for each stream a container holds, or (label, None, why) for one that cannot be read;
        raises `_Unopened` when the container itself cannot be opened."""
        if kind in _COMPRESSED:
            yield self._decompressed(label, data, kind)
        elif kind == "zlib":
            yield self._inflated(label, data)
        elif kind == "sqlite":
            yield from _values(label, data)
        elif kind == "tar":
            yield from self._tar(label, data)
        else:
            yield from self._zip(label, data)

    def _decompressed(self, label: str, data: bytes, kind: str) -> tuple[str, bytes | None, str]:
        inner = f"{label}!({kind})"
        try:
            with _COMPRESSED[kind](io.BytesIO(data)) as stream:
                return inner, _read(stream, inner, self.budget), ""
        except _Exhausted:
            raise
        except _Refused as refused:
            raise _Unopened(_why(refused), limit=not isinstance(refused, _Broken)) from None
        except _ERRORS as error:
            raise _Unopened(error) from None

    def _inflated(self, label: str, data: bytes) -> tuple[str, bytes | None, str]:
        inner = f"{label}!(zlib)"
        try:
            return inner, _inflate(data, inner, self.budget), ""
        except _Exhausted:
            raise
        except _Refused as refused:
            raise _Unopened(_why(refused), limit=not isinstance(refused, _Broken)) from None
        except zlib.error as error:
            raise _Unopened(error) from None

    def _count(self, members: int) -> None:
        self.members += members
        if self.members > views.MAX_MEMBERS:
            raise _Unopened(f"more than {views.MAX_MEMBERS} archive members in all", limit=True)

    def _member(self, label: str, opened) -> tuple[str, bytes | None, str]:
        """One member, read within the budget, as `_inside` gives it."""
        try:
            with opened() as handle:
                return label, _read(handle, label, self.budget), ""
        except _Exhausted:
            raise
        except _Refused as refused:
            return label, None, _why(refused)
        except _ERRORS as error:
            return label, None, f"it cannot be read ({type(error).__name__})"

    def _zip(self, label: str, data: bytes) -> Iterator[tuple[str, bytes | None, str]]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            infos = archive.infolist()
        except _ERRORS as error:
            raise _Unopened(error) from None
        with archive:
            self._count(len(infos))
            if archive.comment:
                yield f"{label}!(comment)", archive.comment, ""
            for info in infos:
                member = f"{label}!{info.filename}"
                if info.comment:
                    yield f"{member}(comment)", info.comment, ""
                if info.is_dir():
                    continue
                if info.flag_bits & _ENCRYPTED:
                    yield member, None, "it is encrypted"
                    continue
                yield self._member(member, lambda info=info: archive.open(info))

    def _tar(self, label: str, data: bytes) -> Iterator[tuple[str, bytes | None, str]]:
        try:
            archive = tarfile.TarFile(fileobj=io.BytesIO(data))  # a plain tar: `_container` saw its header
            infos = archive.getmembers()
        except _ERRORS as error:
            raise _Unopened(error) from None
        with archive:
            self._count(len(infos))
            for info in infos:
                if info.islnk() or info.issym():  # a link's data is its target's, a member of its own
                    continue
                handle = archive.extractfile(info)  # a stream for a regular member and for one of a type the reader
                if handle is not None:  # does not know, as GNU tar extracts it; None for a directory or a device
                    yield self._member(f"{label}!{info.name}", lambda handle=handle: handle)
            if not _zeros_from(data, archive.offset):
                yield label, None, "bytes follow its last member that are not the zero blocks ending a tar"


def _zeros_from(data: bytes, start: int) -> bool:
    """Whether `data` is all zero bytes from `start` on. After the last member the tar reader has read, a tar holds
    only the zero blocks that end it (and the padding after them): the reader stops without a word at a header it
    cannot read, where GNU tar skips to the next one, so any other byte there is a member left unread."""
    return data.count(0, start) == len(data) - start


def _values(label: str, data: bytes) -> Iterator[tuple[str, bytes | None, str]]:
    """The schema's SQL and every TEXT and BLOB value of every table of an SQLite database, each one a stream of its
    own, from a deserialised, read-only copy; TEXT is taken as the bytes stored, valid UTF-8 or not."""
    connection = sqlite3.connect(":memory:")
    connection.text_factory = bytes
    try:
        try:
            connection.deserialize(_rollback_journal(data))
            connection.execute("PRAGMA query_only = ON")
            schema = connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        except sqlite3.Error as error:
            raise _Unopened(error) from None
        yield f"{label}!(schema)", b"\n".join(b"%s %s: %s" % (k, n, sql or b"") for k, n, sql in schema), ""
        for kind, raw, _ in schema:
            if kind != b"table":
                continue
            name = raw.decode("utf-8", "replace")
            try:
                cursor = connection.execute('SELECT * FROM "{}"'.format(name.replace('"', '""')))
                columns = [description[0] for description in cursor.description]
                rows = cursor.fetchall()
            except sqlite3.Error as error:
                yield f"{label}!{name}", None, f"it cannot be read ({type(error).__name__})"
                continue
            for number, row in enumerate(rows, start=1):
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, bytes):
                        yield f"{label}!{name}.{column}#{number}", value, ""
    finally:
        connection.close()


def _rollback_journal(data: bytes) -> bytes:
    """The database file with its header's write and read versions (bytes 18 and 19) at 1, the rollback journal, when
    either is 2, the write-ahead log: `Connection.deserialize` refuses a database in WAL mode, and its tables are read
    the same either way. Only the in-memory copy is changed."""
    if 2 in data[18:20]:
        return data[:18] + b"\x01\x01" + data[20:]
    return data


def _why(refused: _Refused) -> str:
    """The scanner's reason, without its words for the content check: a stream over the cap, or one that fails to
    decompress."""
    return refused.why.removeprefix("refused, ").split("; only text")[0]


def _container(data: bytes) -> str | None:
    """What kind of container the bytes are, by their signature; None for any other bytes."""
    if data.startswith(b"\x1f\x8b\x08"):  # a gzip header names deflate: two bytes alone are chance
        return "gzip"
    if data.startswith(b"BZh") and data[3:4].isdigit():
        return "bzip2"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if data.startswith(_SQLITE):
        return "sqlite"
    if data.startswith(_ZIP):
        return "zip"
    if _is_tar(data):
        return "tar"
    if _ZIP_END in data[-_ZIP_TAIL:]:  # a zip after other bytes, which the zip reader still opens
        return "zip end record"
    if _zlib(data):
        return "zlib"
    return None


def walk(label: str, data: bytes, locations: KeyFinders) -> Walk:
    """Walk one packaged file, or one member of a wheel, for copies of a declared key file (see the module docstring).
    Empty when no key file could be read: then there is nothing to compare."""
    found = Walk()
    if not locations.digests:
        return found
    state = _State(locations, found)
    try:
        state.visit(label, data, 0, strict=True)
    except _Exhausted as exhausted:
        found.unchecked.append((exhausted.label, f"over {views.MAX_TOTAL_BYTES} bytes decompressed in all"))
    except RecursionError:
        found.unchecked.append((label, "encoded runs nested too deeply to walk"))
    return found
