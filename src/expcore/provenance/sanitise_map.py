# Provenance: new
"""Load and validate the private study-token map that sanitising imports apply.

Format: TOML with `version = 1` and `[[entry]]` tables holding `token`, `replacement` and `category`.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from expcore.provenance.study_tokens import WHOLE_ONLY_BELOW, candidates, normalise

MAP_VERSION = 1
_ENTRY_KEYS = frozenset({"token", "replacement", "category"})
_FORBIDDEN_IN_REPLACEMENT = frozenset("\"'\\{}\r\n\0")


class MapError(ValueError):
    """The sanitising map is unreadable or violates one of its rules."""


@dataclass(frozen=True)
class Entry:
    """One mapping from a study token to its placeholder."""

    token: str
    replacement: str
    category: str


@dataclass(frozen=True)
class TokenMap:
    """A validated map and the SHA-256 of the file it was read from."""

    entries: tuple[Entry, ...]
    sha256: str


def matches_on_components(token: str) -> bool:
    """Tell whether a token is replaced on component boundaries (long enough, and made of run characters)."""
    key = normalise(token)
    return len(key) >= WHOLE_ONLY_BELOW and key in {normalise(c) for c in candidates(token)}


def _describe(index: int, entry: Entry) -> str:
    """Name an entry the way every error message does."""
    return f"entry #{index} (category {entry.category!r}, token {entry.token!r})"


def _read_entry(index: int, raw: object, source: str) -> Entry:
    """Turn one TOML table into an Entry, checking its keys and value types."""
    if not isinstance(raw, dict):
        raise MapError(f"{source}: entry #{index} is not a table")
    unknown = set(raw) - _ENTRY_KEYS
    missing = _ENTRY_KEYS - set(raw)
    if unknown or missing:
        problem = f"unknown keys {sorted(unknown)}" if unknown else f"missing keys {sorted(missing)}"
        raise MapError(f"{source}: entry #{index}: {problem}")
    for key in sorted(_ENTRY_KEYS):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise MapError(f"{source}: entry #{index}: {key!r} must be a non-empty string")
    return Entry(raw["token"], raw["replacement"], raw["category"])


def _check_replacement(source: str, index: int, entry: Entry, keys: set[str]) -> None:
    """Reject a replacement that could break a literal or that would itself contain a mapped token."""
    where = f"{source}: {_describe(index, entry)}"
    if _FORBIDDEN_IN_REPLACEMENT.intersection(entry.replacement) or entry.replacement != entry.replacement.strip():
        raise MapError(f"{where}: replacement has quotes, backslashes, braces, line breaks or edge whitespace")
    if normalise(entry.replacement) in keys:
        raise MapError(f"{where}: replacement equals a mapped token")
    normalised = (normalise(c) for c in candidates(entry.replacement))
    if any(len(c) >= WHOLE_ONLY_BELOW and c in keys for c in normalised):
        raise MapError(f"{where}: replacement contains a mapped token on a component boundary")


def parse_map(data: bytes, source: str = "<map>") -> TokenMap:
    """Parse and validate map bytes; raise MapError naming the offending entry."""
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise MapError(f"{source}: not valid TOML: {error}") from error
    if document.get("version") != MAP_VERSION:
        raise MapError(f"{source}: expected 'version = {MAP_VERSION}'")
    raw_entries = document.get("entry", [])
    if not isinstance(raw_entries, list):
        raise MapError(f"{source}: 'entry' must be an array of tables ([[entry]])")
    entries = tuple(_read_entry(i, raw, source) for i, raw in enumerate(raw_entries, start=1))
    seen_tokens: dict[str, int] = {}
    seen_replacements: dict[str, int] = {}
    for index, entry in enumerate(entries, start=1):
        for seen, value, label in (
            (seen_tokens, entry.token, "token"),
            (seen_replacements, entry.replacement, "replacement"),
        ):
            key = normalise(value)
            if key in seen:
                raise MapError(f"{source}: {_describe(index, entry)}: {label} repeats entry #{seen[key]}")
            seen[key] = index
    keys = set(seen_tokens)
    for index, entry in enumerate(entries, start=1):
        _check_replacement(source, index, entry, keys)
    return TokenMap(entries, hashlib.sha256(data).hexdigest())


def load_map(path: Path) -> TokenMap:
    """Read and validate a map file."""
    return parse_map(path.read_bytes(), str(path))
