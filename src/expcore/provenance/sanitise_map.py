# Provenance: new
"""Load and validate the private study-token map that sanitising imports apply.

Format: TOML with `version = 1` and `[[entry]]` tables holding `token`, `replacement` and `category`, one of
`MAP_CATEGORIES` (the token list's categories; a singular, capitals, `_` or a space in place of `-` are read as the
same). The map is private, so no error shows a value it holds beyond what is known to be public: an entry is named by
its number, its category when it is one of the documented words, and its replacement, never its token, and a
replacement that holds a mapped token is left out too. Any other category is free text and is never shown: such an
entry still sanitises, since the category plays no part in it, but errors call it undocumented. An unknown key is
counted, never shown, and a file that is not TOML is named by the line and column alone. `load_map` names no path
either: the map's path is the maintainer's.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from expcore.provenance.study_tokens import WHOLE_ONLY_BELOW, candidates, normalise

MAP_VERSION = 1
_ENTRY_KEYS = frozenset({"token", "replacement", "category"})
# The token list's categories (docs/architecture.md, "The study-token list"), each with its singular.
MAP_CATEGORIES = {
    "conditions": "condition",
    "data-states": "data-state",
    "tasks": "task",
    "datasets": "dataset",
    "models": "model",
    "repositories": "repository",
    "hosts": "host",
    "addresses": "address",
    "spaces": "space",
    "providers": "provider",
    "users": "user",
    "environment-variables": "environment-variable",
}
_ACCEPTED = {form: plural for plural, singular in MAP_CATEGORIES.items() for form in (plural, singular)}
_TOML_PLACE = re.compile(r"\(at line (\d+), column (\d+)\)")
MAP_SOURCE = "the sanitising map"
UNDOCUMENTED = ""  # the category of an entry whose category is none of MAP_CATEGORIES, which is never shown
_FORBIDDEN_IN_REPLACEMENT = frozenset("\"'\\{}\r\n\0")


class MapError(ValueError):
    """The sanitising map is unreadable or violates one of its rules."""


@dataclass(frozen=True)
class Entry:
    """One mapping from a study token to its placeholder."""

    token: str
    replacement: str
    category: str
    number: int = 0  # its place in the map, from 1, which errors name instead of the token


@dataclass(frozen=True)
class TokenMap:
    """A validated map and the SHA-256 of the file it was read from."""

    entries: tuple[Entry, ...]
    sha256: str


def matches_on_components(token: str) -> bool:
    """Tell whether a token is replaced on component boundaries (long enough, and made of run characters)."""
    key = normalise(token)
    return len(key) >= WHOLE_ONLY_BELOW and key in {normalise(c) for c in candidates(token)}


def holds_mapped_token(value: str, keys: set[str]) -> bool:
    """Tell whether a value equals a mapped token or holds one as a component, so showing it would show the token."""
    return normalise(value) in keys or any(normalise(c) in keys for c in candidates(value))


def category_of(entry: Entry) -> str:
    """How an error names an entry's category: the documented word, or that it is undocumented, never free text."""
    return f"category {entry.category!r}" if entry.category in MAP_CATEGORIES else "an undocumented category"


def _describe(index: int, entry: Entry, keys: set[str]) -> str:
    """Name an entry the way every error message does: its number, its category and its replacement, never its token.

    The replacement is public, unless it holds a mapped token, and then it is left out as well.
    """
    if holds_mapped_token(entry.replacement, keys):
        return f"entry #{index} ({category_of(entry)})"
    return f"entry #{index} ({category_of(entry)}, replacement {entry.replacement!r})"


def _read_entry(index: int, raw: object, source: str) -> Entry:
    """Turn one TOML table into an Entry, checking its keys and value types."""
    if not isinstance(raw, dict):
        raise MapError(f"{source}: entry #{index} is not a table")
    unknown = set(raw) - _ENTRY_KEYS
    missing = _ENTRY_KEYS - set(raw)
    if unknown:  # a key is free text, a token typed in the wrong place for instance: counted, never shown
        raise MapError(
            f"{source}: entry #{index}: {len(unknown)} unknown key(s); an entry holds exactly 'category', "
            "'replacement' and 'token'"
        )
    if missing:
        raise MapError(f"{source}: entry #{index}: missing keys {sorted(missing)}")
    for key in sorted(_ENTRY_KEYS):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise MapError(f"{source}: entry #{index}: {key!r} must be a non-empty string")
    category = _ACCEPTED.get(re.sub(r"[\s_]+", "-", raw["category"].strip().casefold()), UNDOCUMENTED)
    return Entry(raw["token"], raw["replacement"], category, index)


def _check_replacement(source: str, index: int, entry: Entry, keys: set[str]) -> None:
    """Reject a replacement that could break a literal or that would itself contain a mapped token."""
    where = f"{source}: {_describe(index, entry, keys)}"
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
    except UnicodeDecodeError as error:
        raise MapError(f"{source}: not UTF-8 (at byte {error.start})") from None
    except tomllib.TOMLDecodeError as error:  # its text can quote the map: only the place is shown
        place = _TOML_PLACE.search(str(error))
        where = f" (at line {place.group(1)}, column {place.group(2)})" if place else ""
        if not place and "(at end of document)" in str(error):
            where = " (at the end of the file)"
        raise MapError(f"{source}: not valid TOML{where}") from None
    if document.get("version") != MAP_VERSION:
        raise MapError(f"{source}: expected 'version = {MAP_VERSION}'")
    raw_entries = document.get("entry", [])
    if not isinstance(raw_entries, list):
        raise MapError(f"{source}: 'entry' must be an array of tables ([[entry]])")
    entries = tuple(_read_entry(i, raw, source) for i, raw in enumerate(raw_entries, start=1))
    keys = {normalise(entry.token) for entry in entries}
    seen_tokens: dict[str, int] = {}
    seen_replacements: dict[str, int] = {}
    for index, entry in enumerate(entries, start=1):
        for seen, value, label in (
            (seen_tokens, entry.token, "token"),
            (seen_replacements, entry.replacement, "replacement"),
        ):
            key = normalise(value)
            if key in seen:
                raise MapError(f"{source}: {_describe(index, entry, keys)}: {label} repeats entry #{seen[key]}")
            seen[key] = index
    for index, entry in enumerate(entries, start=1):
        _check_replacement(source, index, entry, keys)
    return TokenMap(entries, hashlib.sha256(data).hexdigest())


def load_map(path: Path) -> TokenMap:
    """Read and validate a map file; an error names it as the sanitising map, never by its path."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise MapError(f"{MAP_SOURCE} cannot be read ({type(error).__name__})") from None
    return parse_map(data, MAP_SOURCE)
