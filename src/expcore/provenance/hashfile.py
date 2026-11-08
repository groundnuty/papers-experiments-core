# Provenance: new
"""The committed hash file and the private plain list it is generated from.

Each hash line reads `<match> <scope> <sha256>`. The match is `any` (the token is found wherever it is embedded)
or `whole` (only an entire identifier, literal or line equals it, used for tokens under four characters). The
scope is `all` (checked in every file) or `code` (checked everywhere except under docs/, where such a name is
ordinary prose). A line without a scope is an older two-field line and reads as scope `all`.

The plain list may carry `# category: <name>` header lines; a token takes the category above it.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from expcore.provenance.candidates import HASH_PREFIX, WHOLE_ONLY_BELOW, normalise, token_hash

SCOPE_ALL = "all"
SCOPE_CODE = "code"
SCOPES = (SCOPE_CODE, SCOPE_ALL)
CODE_CATEGORIES = frozenset({"conditions", "data-states", "tasks", "datasets"})

_CATEGORY_RE = re.compile(r"^#\s*category\s*:\s*(.+?)\s*$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Hashes:
    """The loaded hash file: hash to scope, for tokens found anywhere and for tokens matching only an entire value."""

    anywhere: dict[str, str]
    whole: dict[str, str]

    def __bool__(self) -> bool:
        """Report whether the file holds any hash at all."""
        return bool(self.anywhere or self.whole)

    def only_all(self) -> "Hashes":
        """Return the hashes whose scope is `all`, which are the only ones checked under docs/."""
        return Hashes(
            {h: s for h, s in self.anywhere.items() if s == SCOPE_ALL},
            {h: s for h, s in self.whole.items() if s == SCOPE_ALL},
        )


class PlainEntry(NamedTuple):
    """One token of the private plain list, with its line number, category and resulting scope."""

    line: int
    token: str
    category: str | None
    scope: str


def scope_for_category(category: str | None) -> str:
    """Map a plain-list category to a scope: names a study invented for code are `code`, everything else `all`."""
    return SCOPE_CODE if category and category.strip().casefold() in CODE_CATEGORIES else SCOPE_ALL


def parse_plain_entries(text: str) -> list[PlainEntry]:
    """Read a plain list: one token per line, blank and ordinary '#' lines ignored, '# category:' headers tracked."""
    entries: list[PlainEntry] = []
    category: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        header = _CATEGORY_RE.match(line)
        if header:
            category = header.group(1)
        elif line and not line.startswith("#"):
            entries.append(PlainEntry(number, line, category, scope_for_category(category)))
    return entries


def parse_plain_list(text: str) -> list[str]:
    """Read just the tokens of a plain list."""
    return [entry.token for entry in parse_plain_entries(text)]


def render_hash_file(entries: Iterable[str | tuple[str, str]]) -> str:
    """Render the hash file: sorted, de-duplicated lines; a bare token has scope `all`, a pair is (token, scope)."""
    best: dict[tuple[str, str], str] = {}
    for entry in entries:
        token, scope = (entry, SCOPE_ALL) if isinstance(entry, str) else entry
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
        normalised = normalise(token)
        if normalised:
            key = ("whole" if len(normalised) < WHOLE_ONLY_BELOW else "any", token_hash(token))
            if best.get(key) != SCOPE_ALL:  # a token listed under two categories is checked everywhere
                best[key] = scope
    header = [
        "# expcore study-token hashes, format 2: '<any|whole> <code|all> <sha256>' (two-field lines read as 'all').",
        f"# Hash input: '{HASH_PREFIX}' + NFKC, case-folded, stripped token. Regenerate; do not edit.",
    ]
    return "\n".join([*header, *sorted(f"{match} {scope} {digest}" for (match, digest), scope in best.items())]) + "\n"


def parse_hash_file(text: str, source: str) -> Hashes:
    """Parse hash-file text; raise ValueError naming the source and line on a malformed line."""
    tables: dict[str, dict[str, str]] = {"any": {}, "whole": {}}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) == 2:
            fields.insert(1, SCOPE_ALL)
        ok = len(fields) == 3 and fields[0] in tables and fields[1] in SCOPES and _DIGEST_RE.fullmatch(fields[2])
        if not ok:
            raise ValueError(f"{source}:{number}: expected '<any|whole> <code|all> <sha256>'")
        match, scope, digest = fields
        if tables[match].get(digest) != SCOPE_ALL:
            tables[match][digest] = scope
    return Hashes(tables["any"], tables["whole"])


def hashes_from_tokens(tokens: Iterable[str]) -> Hashes:
    """Hash tokens in memory with the same rules as the hash file, all with scope `all`."""
    return parse_hash_file(render_hash_file(tokens), "<memory>")
