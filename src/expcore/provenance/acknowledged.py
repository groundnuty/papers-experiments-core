# Provenance: new
"""Acknowledged findings in already-pushed history.

A public history is never rewritten, so a finding in an old commit cannot be removed. The maintainers list it
instead, by full commit id, path, line and hash prefix, never by the matched text. Only `check-commits` on historical
commits honours the list; the working-tree check never does, and an entry naming the current commit is refused,
because that commit can still be fixed. A finding whose path is redacted cannot be acknowledged: the redacted form
does not identify the file, and a path that holds a private name is for the maintainers to resolve.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from expcore.provenance.gitobjects import GitError, run_git
from expcore.provenance.scanner import REDACTED_PATH, Finding

DEFAULT_ACKNOWLEDGED = Path("tests/data/study_tokens.acknowledged")
COMMIT_CHARS = 40

_LINE_RE = re.compile(r"(?P<commit>[0-9a-f]{40}) (?P<path>.+):(?P<line>\d+) (?P<prefix>[0-9a-f]{12})")


@dataclass(frozen=True)
class Acknowledgement:
    """One accepted finding: a full commit id, where in it, and the start of the hash that matched."""

    commit: str
    path: str
    line: int
    hash_prefix: str
    source_line: int

    def matches(self, sha: str, finding: Finding) -> bool:
        """Tell whether a finding of the given commit is exactly the one acknowledged."""
        return (
            sha == self.commit
            and finding.path != REDACTED_PATH
            and finding.path == self.path
            and finding.line == self.line
            and finding.hash_prefix == self.hash_prefix
        )


def parse_acknowledgements(text: str, source: str) -> list[Acknowledgement]:
    """Parse `<full commit id> <path>:<line> <hash prefix>` lines; '#' comments and blank lines are allowed."""
    entries: list[Acknowledgement] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"{source}:{number}: expected '<40-hex commit id> <path>:<line> <12 hex>'")
        if match["path"] == REDACTED_PATH:
            raise ValueError(f"{source}:{number}: a finding with a redacted path cannot be acknowledged")
        entries.append(Acknowledgement(match["commit"], match["path"], int(match["line"]), match["prefix"], number))
    return entries


def load_acknowledgements(path: Path, *, required: bool = False) -> list[Acknowledgement]:
    """Load the file; a missing file means nothing is acknowledged unless it was named explicitly (required)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise
        return []
    return parse_acknowledgements(text, str(path))


def refuse_current_commit(entries: Sequence[Acknowledgement], repo: Path, source: str) -> None:
    """Raise ValueError, naming the line, if an entry names the commit checked out in repo."""
    try:
        head = run_git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    except GitError:
        return  # a repository with no commit has nothing to acknowledge
    for entry in entries:
        if head == entry.commit:
            raise ValueError(f"{source}:{entry.source_line}: names the current commit, which can still be fixed")


def split_acknowledged(
    results: Sequence[tuple[str, Finding]], entries: Sequence[Acknowledgement]
) -> tuple[list[tuple[str, Finding]], list[tuple[str, Finding]]]:
    """Split findings into those not acknowledged and those exactly matched by an entry."""
    remaining: list[tuple[str, Finding]] = []
    acknowledged: list[tuple[str, Finding]] = []
    for sha, finding in results:
        hit = any(entry.matches(sha, finding) for entry in entries)
        (acknowledged if hit else remaining).append((sha, finding))
    return remaining, acknowledged
