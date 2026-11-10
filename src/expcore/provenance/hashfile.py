# Provenance: new
"""The committed hash file and the private plain list it is generated from.

Each hash line reads `<match> <scope> <sha256>`. The match is `any` (the token is found wherever it is embedded)
or `whole` (only an entire identifier, literal or line equals it, used for tokens under four characters). The
scope is `all` (checked in every file) or `code` (checked everywhere except under docs/, where such a name is
ordinary prose). A line without a scope is an older two-field line and reads as scope `all`.

The plain list may carry `# category: <name>` header lines; a token takes the category above it. A line
`whole: <token>` marks one token as matching only as an entire identifier, literal or line, whatever its length: for
an ordinary word that would otherwise be found inside unrelated names. It is normalised and hashed like any other
token, and rendered as `whole <scope> <sha256>`; where a token is listed both ways, the override wins.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from expcore.provenance.candidates import HASH_PREFIX, WHOLE_ONLY_BELOW, normalise, token_hash

SCOPE_ALL = "all"
SCOPE_CODE = "code"
SCOPES = (SCOPE_CODE, SCOPE_ALL)
# Names a study invented for its design, and public model names: code must not hard-code them, but prose may name
# them. Everything else (hosts, addresses, spaces, providers, users, environment variables) is private everywhere.
CODE_CATEGORIES = frozenset({"conditions", "data-states", "tasks", "datasets", "models", "repositories"})

_CATEGORY_RE = re.compile(r"^#\s*category\s*:\s*(.+?)\s*$", re.IGNORECASE)
WHOLE_PREFIX = "whole:"  # a plain-list line that marks its token as matching only as an entire value
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
GENERATE = (
    "Generate the file from the plain list (one token per line): "
    "python -m expcore.provenance.study_tokens hash PLAIN > FILE"
)


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
    """One token of the private plain list, with its line number, category, resulting scope and whether its line
    marks it as matching only as an entire value."""

    line: int
    token: str
    category: str | None
    scope: str
    whole: bool = False


def scope_for_category(category: str | None) -> str:
    """Map a plain-list category to a scope: names a study invented for code are `code`, everything else `all`."""
    return SCOPE_CODE if category and category.strip().casefold() in CODE_CATEGORIES else SCOPE_ALL


def parse_plain_entries(text: str) -> list[PlainEntry]:
    """Read a plain list: one token per line, blank and ordinary '#' lines ignored, '# category:' headers tracked, and
    a `whole:` line marking its token whole-only. Raise ValueError, naming the line but never its token, for a `whole:`
    line that names no token."""
    entries: list[PlainEntry] = []
    category: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        header = _CATEGORY_RE.match(line)
        if header:
            category = header.group(1)
        elif line and not line.startswith("#"):
            whole = line.startswith(WHOLE_PREFIX)
            token = line[len(WHOLE_PREFIX) :].strip() if whole else line
            if not token:
                raise ValueError(f"line {number}: '{WHOLE_PREFIX}' names no token")
            entries.append(PlainEntry(number, token, category, scope_for_category(category), whole))
    return entries


def parse_plain_list(text: str) -> list[str]:
    """Read just the tokens of a plain list."""
    return [entry.token for entry in parse_plain_entries(text)]


def render_hash_file(entries: Iterable[str | tuple[str, str] | tuple[str, str, bool]]) -> str:
    """Render the hash file: sorted, de-duplicated lines. A bare token has scope `all`; a pair is (token, scope); a
    triple is (token, scope, whole), where `whole` marks the token as matching only as an entire value."""
    scopes: dict[str, str] = {}  # digest -> scope, `all` winning for a token listed under two categories
    whole: dict[str, bool] = {}  # digest -> matched only whole: short, or marked so on any of its lines
    for entry in entries:
        token, scope, marked = (entry, SCOPE_ALL, False) if isinstance(entry, str) else (*entry, False)[:3]
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
        normalised = normalise(token)
        if normalised:
            digest = token_hash(token)
            if scopes.get(digest) != SCOPE_ALL:  # a token listed under two categories is checked everywhere
                scopes[digest] = scope
            whole[digest] = whole.get(digest, False) or bool(marked) or len(normalised) < WHOLE_ONLY_BELOW
    best = {("whole" if whole[digest] else "any", digest): scope for digest, scope in scopes.items()}
    header = [
        "# expcore study-token hashes, format 2: '<any|whole> <code|all> <sha256>' (two-field lines read as 'all').",
        f"# Hash input: '{HASH_PREFIX}' + NFKC, case-folded, stripped token. Regenerate; do not edit.",
    ]
    return "\n".join([*header, *sorted(f"{match} {scope} {digest}" for (match, digest), scope in best.items())]) + "\n"


def _malformed(source: str, number: int, fields: list[str]) -> ValueError:
    """The error for a line not in the hash format: a line that starts with a digest is a plain `sha256(token)`, the
    likeliest mistake, and is named as one; either way the message gives the command that makes a correct file."""
    if _DIGEST_RE.fullmatch(fields[0]):
        return ValueError(
            f"{source}:{number}: a bare SHA-256 digest, without the '<any|whole> <code|all>' fields; a study-token "
            f"hash is salted with '{HASH_PREFIX}' and normalised, so a plain digest of a name matches nothing. "
            f"{GENERATE}"
        )
    return ValueError(f"{source}:{number}: expected '<any|whole> <code|all> <sha256>'. {GENERATE}")


def parse_hash_file(text: str, source: str) -> Hashes:
    """Parse hash-file text; raise ValueError naming the source and line on a malformed line.

    A file of lines in the right format whose digests were made some other way cannot be told from a correct one:
    a digest shows nothing of its input. Only the shape of each line is checked.
    """
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
            raise _malformed(source, number, line.split())
        match, scope, digest = fields
        if tables[match].get(digest) != SCOPE_ALL:
            tables[match][digest] = scope
    return Hashes(tables["any"], tables["whole"])


def hashes_from_tokens(tokens: Iterable[str]) -> Hashes:
    """Hash tokens in memory with the same rules as the hash file, all with scope `all`."""
    return parse_hash_file(render_hash_file(tokens), "<memory>")
