# Provenance: new
"""Normalising, hashing, and the candidate strings a study token could be embedded as.

A candidate comes from a run of letters, digits and `_ . -`. The run is cut into components at `_`, `.` and
`-`; every contiguous sequence of components, re-joined with the separators it had, is a candidate. A hostname
in a URL, a space name in a path and a name inside a composed identifier therefore all become candidates.
"""

import functools
import hashlib
import re
import unicodedata
from collections.abc import Iterator

HASH_PREFIX = "expcore-study-token-v1:"
WHOLE_ONLY_BELOW = 4
MAX_COMPONENTS = 24
MAX_CANDIDATE_CHARS = 256
MAX_EDGE_CHARS = 2

_ASCII_RUN = re.compile(r"[A-Za-z0-9_.\-]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")
_UNICODE_RUN = re.compile(r"[\w.\-]+")
_UNICODE_WORD = re.compile(r"[^\W_]+")


def normalise(token: str) -> str:
    """Normalise a token: Unicode NFKC, case-folded, surrounding whitespace removed."""
    return unicodedata.normalize("NFKC", token).casefold().strip()


@functools.lru_cache(maxsize=1 << 16)
def token_hash(token: str) -> str:
    """Return the hex SHA-256 of the versioned prefix followed by the normalised token."""
    return hashlib.sha256((HASH_PREFIX + normalise(token)).encode("utf-8")).hexdigest()


def iter_runs(text: str, *, unicode_words: bool = False) -> Iterator[re.Match[str]]:
    """Yield the maximal runs of letters, digits and `_ . -` in the text (any script when unicode_words)."""
    return (_UNICODE_RUN if unicode_words else _ASCII_RUN).finditer(text)


def iter_windows(run: str, *, unicode_words: bool = False) -> Iterator[tuple[int, int]]:
    """Yield (start, end) of every candidate inside a run.

    Candidates are the contiguous sequences of up to MAX_COMPONENTS components, each also with up to
    MAX_EDGE_CHARS characters of the separator before and after it (so `_private_` is found inside
    `prefix._private_.suffix`), and the whole run when it starts or ends with a separator. A candidate is at
    most MAX_CANDIDATE_CHARS long, so the work grows linearly with the text.
    """
    words = [m.span() for m in (_UNICODE_WORD if unicode_words else _ASCII_WORD).finditer(run)]
    for i, (start, _) in enumerate(words):
        lead = start - (words[i - 1][1] if i else 0)
        for j in range(i, min(i + MAX_COMPONENTS, len(words))):
            end = words[j][1]
            if end - start > MAX_CANDIDATE_CHARS:
                break
            trail = (words[j + 1][0] if j + 1 < len(words) else len(run)) - end
            for before in range(min(lead, MAX_EDGE_CHARS) + 1):
                for after in range(min(trail, MAX_EDGE_CHARS) + 1):
                    if end - start + before + after <= MAX_CANDIDATE_CHARS:
                        yield start - before, end + after
    if words and (words[0][0] > 0 or words[-1][1] < len(run)) and len(run) <= MAX_CANDIDATE_CHARS:
        yield 0, len(run)


def scan_runs(text: str) -> Iterator[tuple[int, str]]:
    """Yield (offset, candidate) for every candidate of already NFKC-normalised text."""
    for run in iter_runs(text):
        value = run.group()
        for start, end in iter_windows(value):
            yield run.start() + start, value[start:end]


def candidates(text: str) -> Iterator[str]:
    """Yield every string a study token could be embedded as in the text."""
    for _, candidate in scan_runs(unicodedata.normalize("NFKC", text)):
        yield candidate


def is_matchable(token: str) -> bool:
    """Report whether the token, as written, appears among the candidates of its own text."""
    return normalise(token) in {normalise(c) for c in candidates(token)}
