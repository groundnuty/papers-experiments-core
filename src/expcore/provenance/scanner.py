# Provenance: new
"""Scan a file's text for study tokens and report findings by hash prefix, never by matched text.

Python source is scanned by token: identifiers, comments, string literals (with escapes decoded), the literal
parts of f-strings, and the concatenation of adjacent literals. Any other file is scanned line by line. The
file's own path is scanned too; when it matches, every finding for that file hides the path.
"""

import ast
import io
import re
import tokenize
import unicodedata
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from os import PathLike
from pathlib import PurePosixPath

from expcore.provenance.candidates import scan_runs, token_hash
from expcore.provenance.hashfile import Hashes
from expcore.provenance.headers import blank_header

FINDING_PREFIX_CHARS = 12
REDACTED_PATH = "<path redacted>"
DOCS_ROOT = "docs"


def _token_types(*names: str) -> frozenset[int]:
    """Return the token type numbers for the names this Python version defines."""
    return frozenset(getattr(tokenize, name) for name in names if hasattr(tokenize, name))


FSTRING_START_TYPES = _token_types("FSTRING_START", "TSTRING_START")
FSTRING_MIDDLE_TYPES = _token_types("FSTRING_MIDDLE", "TSTRING_MIDDLE")
FSTRING_END_TYPES = _token_types("FSTRING_END", "TSTRING_END")

_STRING_PARTS_RE = re.compile(r"(?s)^[A-Za-z]*('''|\"\"\"|'|\")(.*)\1$")


@dataclass(frozen=True, order=True)
class Finding:
    """One match: where it is and the start of its hash, never the matched text; a matching path is redacted."""

    path: str
    line: int
    kind: str
    hash_prefix: str

    def __str__(self) -> str:
        """Format as `path:line kind hashprefix`."""
        return f"{self.path}:{self.line} {self.kind} {self.hash_prefix}"


def _hits(text: str, hashes: Hashes) -> Iterator[tuple[int, str]]:
    """Yield (line offset within text, hash) for each candidate whose hash is in the any set."""
    normalised = unicodedata.normalize("NFKC", text)
    for offset, candidate in scan_runs(normalised):
        digest = token_hash(candidate)
        if digest in hashes.anywhere:
            yield normalised.count("\n", 0, offset), digest


def _whole_hit(value: str, hashes: Hashes) -> str | None:
    """Return the hash when an entire identifier, literal or line equals a listed token."""
    digest = token_hash(value)
    return digest if digest in hashes.whole or digest in hashes.anywhere else None


def decode_escapes(raw: str) -> str | None:
    """Decode backslash escapes as an ordinary literal would; None when there are none or decoding fails."""
    if "\\" not in raw:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            value = raw.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return None
    return value if value != raw else None


def _literal_values(raw: str) -> tuple[str, str | None]:
    """Return a string token's raw content and, when escapes change it, its evaluated value."""
    parts = _STRING_PARTS_RE.match(raw)
    content = parts.group(2) if parts else raw
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # an invalid escape is still a literal worth scanning
            value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return content, None
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    return content, value if isinstance(value, str) and value != content else None


@dataclass
class _FString:
    """An open f-string: its line, its literal parts so far, and whether it has replacement fields."""

    line: int
    parts: list[str] = field(default_factory=list)
    segment: list[str] = field(default_factory=list)
    segment_line: int = 0
    has_fields: bool = False


class _PythonScan:
    """Walks the tokens of one Python file, collecting findings."""

    def __init__(self, path: str, hashes: Hashes) -> None:
        """Start with no findings, no open f-string and no pending adjacent literals."""
        self.path = path
        self.hashes = hashes
        self.found: set[Finding] = set()
        self._fstrings: list[_FString] = []
        self._pieces: list[tuple[int, str]] = []

    def _add(self, line: int, kind: str, digest: str) -> None:
        """Record a finding."""
        self.found.add(Finding(self.path, line, kind, digest[:FINDING_PREFIX_CHARS]))

    def _add_hits(self, line: int, kind: str, text: str) -> None:
        """Record every any-match inside the text."""
        for offset, digest in _hits(text, self.hashes):
            self._add(line + offset, kind, digest)

    def _add_whole(self, line: int, kind: str, value: str) -> None:
        """Record a match of an entire value."""
        digest = _whole_hit(value, self.hashes)
        if digest:
            self._add(line, kind, digest)

    def _string(self, tok: tokenize.TokenInfo) -> str:
        """Scan a string or bytes literal, with escapes decoded, and return its value."""
        line = tok.start[0]
        self._add_hits(line, "string", tok.string)
        content, evaluated = _literal_values(tok.string)
        for value in (content, evaluated):
            if value is not None:
                self._add_whole(line, "string", value)
        if evaluated is not None:
            self._add_hits(line, "string", evaluated)
        return content if evaluated is None else evaluated

    def _simple(self, tok: tokenize.TokenInfo) -> None:
        """Scan one identifier or comment."""
        line = tok.start[0]
        if tok.type == tokenize.NAME:
            self._add_whole(line, "name", tok.string)
            self._add_hits(line, "name", tok.string)
        elif tok.type == tokenize.COMMENT:
            self._add_hits(line, "comment", tok.string)

    def _flush(self) -> None:
        """Scan the concatenation of adjacent literals, reporting only what no single piece showed."""
        pieces, self._pieces = self._pieces, []
        if len(pieces) < 2:
            return
        own = {digest for _, value in pieces for _, digest in _hits(value, self.hashes)}
        joined = "".join(value for _, value in pieces)
        line = pieces[0][0]
        for offset, digest in _hits(joined, self.hashes):
            if digest not in own:
                self._add(line + offset, "string", digest)
        whole = _whole_hit(joined, self.hashes)
        if whole and whole not in own:
            self._add(line, "string", whole)

    def _close_segment(self) -> None:
        """Scan the decoded form of the literal text since the last replacement field of the open f-string."""
        top = self._fstrings[-1]
        decoded = decode_escapes("".join(top.segment)) if top.segment else None
        if decoded is not None:
            self._add_hits(top.segment_line, "string", decoded)
        top.segment = []

    def _fstring_end(self) -> None:
        """Close an f-string: with no replacement fields it is one whole literal, otherwise it ends a literal run."""
        self._close_segment()
        closed = self._fstrings.pop()
        if self._fstrings:  # nested: the enclosing f-string already has a replacement field
            return
        if closed.has_fields:
            self._flush()
            return
        raw = "".join(closed.parts)
        value = decode_escapes(raw) or raw
        self._add_whole(closed.line, "string", value)
        self._pieces.append((closed.line, value))

    def feed(self, tok: tokenize.TokenInfo) -> None:
        """Take the next token."""
        kind, line = tok.type, tok.start[0]
        if kind in FSTRING_START_TYPES:
            if self._fstrings:
                self._close_segment()
                self._fstrings[-1].has_fields = True
            self._fstrings.append(_FString(line))
        elif kind in FSTRING_MIDDLE_TYPES:
            top = self._fstrings[-1]
            if not top.segment:
                top.segment_line = line
            top.segment.append(tok.string)
            top.parts.append(tok.string)
            self._add_hits(line, "string", tok.string)
        elif kind in FSTRING_END_TYPES:
            self._fstring_end()
        elif self._fstrings:  # a replacement field of an f-string
            self._close_segment()
            self._fstrings[-1].has_fields = True
            if kind == tokenize.STRING:
                self._string(tok)
            else:
                self._simple(tok)
        elif kind == tokenize.STRING:
            self._pieces.append((line, self._string(tok)))
        elif kind == tokenize.COMMENT:
            self._simple(tok)  # a comment between adjacent literals does not end the run
        elif kind != tokenize.NL:
            self._flush()
            self._simple(tok)

    def finish(self) -> set[Finding]:
        """Flush any pending literals and return the findings."""
        self._flush()
        return self.found


def _scan_text(path: str, text: str, hashes: Hashes, kind: str = "text") -> set[Finding]:
    """Scan text line by line; a line's stripped value may also match a whole-literal hash."""
    found: set[Finding] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        for _, digest in _hits(line, hashes):
            found.add(Finding(path, number, kind, digest[:FINDING_PREFIX_CHARS]))
        stripped = line.strip()
        digest = _whole_hit(stripped, hashes) if stripped else None
        if digest:
            found.add(Finding(path, number, kind, digest[:FINDING_PREFIX_CHARS]))
    return found


def _scan_python(path: str, text: str, hashes: Hashes) -> set[Finding]:
    """Scan Python source by token (header block skipped); fall back to plain text if it cannot be tokenised."""
    body = blank_header(text)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, SyntaxError):
        return _scan_text(path, body, hashes)
    scan = _PythonScan(path, hashes)
    for tok in tokens:
        scan.feed(tok)
    return scan.finish()


def _path_digests(path: str, hashes: Hashes) -> set[str]:
    """Return the hashes a file path matches: as embedded candidates, or as an entire segment or stem."""
    digests = {digest for _, digest in _hits(path, hashes)}
    for segment in path.split("/"):
        for value in (segment, PurePosixPath(segment).stem):
            digest = _whole_hit(value, hashes) if value else None
            if digest:
                digests.add(digest)
    return digests


def is_docs_path(path: str) -> bool:
    """Tell whether a repository-relative path is prose: under docs/, or any Markdown file."""
    posix = PurePosixPath(path)
    return posix.parts[:1] == (DOCS_ROOT,) or posix.suffix.casefold() == ".md"


def scan_source(path: str | PathLike[str], text: str, hashes: Hashes) -> list[Finding]:
    """Scan one file: its path, then its text (Python by tokens, anything else by line).

    Prose (files under docs/ and Markdown anywhere) is checked only against `all`-scope hashes, because it may
    describe a study's design. If the path itself matches, it is reported as a `path` finding and replaced by a
    placeholder in every finding of the file.
    """
    name = str(path)
    scoped = hashes.only_all() if is_docs_path(name) else hashes
    path_digests = _path_digests(name, scoped)
    shown = REDACTED_PATH if path_digests else name
    found = _scan_python(shown, text, scoped) if name.endswith(".py") else _scan_text(shown, text, scoped)
    found |= {Finding(shown, 0, "path", digest[:FINDING_PREFIX_CHARS]) for digest in path_digests}
    return sorted(found)
