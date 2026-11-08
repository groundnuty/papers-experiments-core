# Provenance: new
"""Study-token hashing and the check that no commit of this public repository contains a study's name.

The plain token list stays private. The repository holds only SHA-256 hashes, and every finding reports a
hash prefix, never the text that matched, because CI logs are public.
"""

import argparse
import ast
import functools
import hashlib
import io
import re
import sys
import tokenize
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from expcore.provenance.gitobjects import GitError, changed_files, read_blob, resolve_commits
from expcore.provenance.headers import blank_header

HASH_PREFIX = "expcore-study-token-v1:"
WHOLE_ONLY_BELOW = 4
MAX_COMPONENTS = 12
MAX_CANDIDATE_CHARS = 256
FINDING_PREFIX_CHARS = 12
DEFAULT_HASHES = Path("tests/data/study_tokens.sha256")
COMMIT_ROOTS = ("src/", "examples/")
SKIPPED_DIRS = frozenset({"__pycache__", ".git", ".venv"})

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2

_RUN_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_STRING_PARTS_RE = re.compile(r"(?s)^[A-Za-z]*('''|\"\"\"|'|\")(.*)\1$")
FSTRING_MIDDLE_TYPES = tuple(
    getattr(tokenize, name) for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE") if hasattr(tokenize, name)
)


@dataclass(frozen=True)
class Hashes:
    """The loaded hash file: hashes that match anywhere, and hashes that match only a whole literal."""

    anywhere: frozenset[str]
    whole: frozenset[str]

    def __bool__(self) -> bool:
        """Report whether the file holds any hash at all."""
        return bool(self.anywhere or self.whole)


@dataclass(frozen=True, order=True)
class Finding:
    """One match: where it is and the start of its hash, never the matched text."""

    path: str
    line: int
    kind: str
    hash_prefix: str

    def __str__(self) -> str:
        """Format as `path:line kind hashprefix`."""
        return f"{self.path}:{self.line} {self.kind} {self.hash_prefix}"


def normalise(token: str) -> str:
    """Normalise a token: Unicode NFKC, case-folded, surrounding whitespace removed."""
    return unicodedata.normalize("NFKC", token).casefold().strip()


@functools.lru_cache(maxsize=1 << 16)
def token_hash(token: str) -> str:
    """Return the hex SHA-256 of the versioned prefix followed by the normalised token."""
    return hashlib.sha256((HASH_PREFIX + normalise(token)).encode("utf-8")).hexdigest()


def iter_windows(run: str) -> Iterator[tuple[int, int]]:
    """Yield (start, end) of every contiguous word sequence in a run, plus the run itself if edged by separators.

    A window holds at most MAX_COMPONENTS words and MAX_CANDIDATE_CHARS characters, so work grows linearly.
    """
    words = [m.span() for m in _WORD_RE.finditer(run)]
    for i, (start, _) in enumerate(words):
        for _, end in words[i : i + MAX_COMPONENTS]:
            if end - start > MAX_CANDIDATE_CHARS:
                break
            yield start, end
    if words and (words[0][0] > 0 or words[-1][1] < len(run)) and len(run) <= MAX_CANDIDATE_CHARS:
        yield 0, len(run)


def _scan_runs(text: str) -> Iterator[tuple[int, str]]:
    """Yield (offset, candidate) for every window of every run in already-normalised text."""
    for run in _RUN_RE.finditer(text):
        for start, end in iter_windows(run.group()):
            yield run.start() + start, run.group()[start:end]


def candidates(text: str) -> Iterator[str]:
    """Yield every string a study token could be embedded as: windows of the separator-joined components of each run."""
    for _, candidate in _scan_runs(unicodedata.normalize("NFKC", text)):
        yield candidate


def is_matchable(token: str) -> bool:
    """Report whether the token, as written, appears among the candidates of its own text."""
    return normalise(token) in {normalise(c) for c in candidates(token)}


def parse_plain_list(text: str) -> list[str]:
    """Read a plain token list: one token per line, blank lines and '#' lines ignored."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def render_hash_file(tokens: Iterable[str]) -> str:
    """Render the hash file for a token list: sorted, de-duplicated 'any|whole <sha256>' lines."""
    lines = set()
    for token in tokens:
        normalised = normalise(token)
        if normalised:
            kind = "whole" if len(normalised) < WHOLE_ONLY_BELOW else "any"
            lines.add(f"{kind} {token_hash(token)}")
    header = [
        "# expcore study-token hashes, format 1: '<any|whole> <sha256>'.",
        f"# Hash input: '{HASH_PREFIX}' + NFKC, case-folded, stripped token. Regenerate; do not edit.",
    ]
    return "\n".join([*header, *sorted(lines)]) + "\n"


def parse_hash_file(text: str, source: str) -> Hashes:
    """Parse hash-file text; raise ValueError naming the source and line on a malformed line."""
    anywhere: set[str] = set()
    whole: set[str] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, digest = line.partition(" ")
        if kind not in ("any", "whole") or not re.fullmatch(r"[0-9a-f]{64}", digest.strip()):
            raise ValueError(f"{source}:{number}: expected 'any <sha256>' or 'whole <sha256>'")
        (anywhere if kind == "any" else whole).add(digest.strip())
    return Hashes(frozenset(anywhere), frozenset(whole))


def load_hashes(path: Path) -> Hashes:
    """Load a hash file from disk."""
    return parse_hash_file(path.read_text(encoding="utf-8"), str(path))


def _hits(text: str, hashes: Hashes) -> Iterator[tuple[int, str]]:
    """Yield (line offset within text, hash) for each candidate whose hash is in the any set."""
    normalised = unicodedata.normalize("NFKC", text)
    for offset, candidate in _scan_runs(normalised):
        digest = token_hash(candidate)
        if digest in hashes.anywhere:
            yield normalised.count("\n", 0, offset), digest


def _whole_hit(value: str, hashes: Hashes) -> str | None:
    """Return the hash when an entire identifier, literal or line equals a listed token."""
    digest = token_hash(value)
    return digest if digest in hashes.whole or digest in hashes.anywhere else None


def _finding(path: str, line: int, kind: str, digest: str) -> Finding:
    """Build a Finding from the first characters of the matching hash."""
    return Finding(path, line, kind, digest[:FINDING_PREFIX_CHARS])


def _scan_text(path: str, text: str, hashes: Hashes, kind: str = "text") -> set[Finding]:
    """Scan text line by line; a line's stripped value may also match a whole-literal hash."""
    found: set[Finding] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        found.update(_finding(path, number, kind, digest) for _, digest in _hits(line, hashes))
        stripped = line.strip()
        digest = _whole_hit(stripped, hashes) if stripped else None
        if digest:
            found.add(_finding(path, number, kind, digest))
    return found


def _eval_literal(raw: str) -> str:
    """Evaluate a string or bytes literal token to text (bytes as latin-1); raise ValueError if it is neither."""
    value = ast.literal_eval(raw)
    if isinstance(value, bytes):
        return value.decode("latin-1")
    if isinstance(value, str):
        return value
    raise ValueError("not a string literal")


def _string_values(raw: str) -> tuple[str, str | None]:
    """Return a string token's raw content and, when escapes change it, its evaluated value."""
    parts = _STRING_PARTS_RE.match(raw)
    content = parts.group(2) if parts else raw
    try:
        value = _eval_literal(raw)
    except (ValueError, SyntaxError):
        return content, None
    return content, value if value != content else None


def _scan_token(path: str, tok: tokenize.TokenInfo, hashes: Hashes) -> set[Finding]:
    """Scan one NAME, STRING, f-string part or COMMENT token."""
    line = tok.start[0]
    if tok.type == tokenize.NAME:
        digest = _whole_hit(tok.string, hashes)
        found = {_finding(path, line, "name", digest)} if digest else set()
        return found | {_finding(path, line + n, "name", d) for n, d in _hits(tok.string, hashes)}
    if tok.type == tokenize.COMMENT:
        return {_finding(path, line + n, "comment", d) for n, d in _hits(tok.string, hashes)}
    if tok.type in FSTRING_MIDDLE_TYPES:
        return {_finding(path, line + n, "string", d) for n, d in _hits(tok.string, hashes)}
    found = {_finding(path, line + n, "string", d) for n, d in _hits(tok.string, hashes)}
    content, evaluated = _string_values(tok.string)
    for value in (content, evaluated):
        digest = _whole_hit(value, hashes) if value is not None else None
        if digest:
            found.add(_finding(path, line, "string", digest))
    if evaluated is not None:
        found.update(_finding(path, line, "string", d) for _, d in _hits(evaluated, hashes))
    return found


def _scan_python(path: str, text: str, hashes: Hashes) -> set[Finding]:
    """Scan names, literals and comments of Python source; fall back to plain text if it cannot be tokenised."""
    body = blank_header(text)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, SyntaxError):
        return _scan_text(path, body, hashes)
    wanted = {tokenize.NAME, tokenize.STRING, tokenize.COMMENT, *FSTRING_MIDDLE_TYPES}
    found: set[Finding] = set()
    for tok in tokens:
        if tok.type in wanted:
            found |= _scan_token(path, tok, hashes)
    return found


def scan_source(path: str | Path, text: str, hashes: Hashes) -> list[Finding]:
    """Scan one file's text: Python by tokens (header block skipped), anything else as plain text."""
    name = str(path)
    found = _scan_python(name, text, hashes) if name.endswith(".py") else _scan_text(name, text, hashes)
    return sorted(found)


def _decode(data: bytes) -> str | None:
    """Decode file bytes as UTF-8 (BOM dropped, bad bytes replaced); None for binary content."""
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace")


def scan_paths(paths: Iterable[Path], hashes: Hashes) -> list[Finding]:
    """Scan files and directories (recursively); raise FileNotFoundError for a path that does not exist."""
    findings: list[Finding] = []
    for root in paths:
        if not root.exists():
            raise FileNotFoundError(f"no such path: {root}")
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if _wanted(p, root))
        for file in files:
            text = _decode(file.read_bytes())
            if text is not None:
                findings.extend(scan_source(file, text, hashes))
    return findings


def _wanted(path: Path, root: Path) -> bool:
    """Tell whether a path found under a scanned directory is a regular file outside skipped directories."""
    return path.is_file() and not SKIPPED_DIRS.intersection(path.relative_to(root).parts)


def check_commits(
    repo: Path,
    revs: Sequence[str],
    hashes: Hashes,
    roots: tuple[str, ...] = COMMIT_ROOTS,
    *,
    history: bool = False,
) -> list[tuple[str, Finding]]:
    """Scan every file each commit added or modified under the roots, at that commit's content."""
    results: list[tuple[str, Finding]] = []
    for sha in resolve_commits(repo, revs, history=history):
        for path in changed_files(repo, sha):
            text = _decode(read_blob(repo, sha, path)) if path.startswith(roots) else None
            if text is not None:
                results.extend((sha, f) for f in scan_source(path, text, hashes))
    return results


def _load_for_cli(explicit: Path | None) -> Hashes | None:
    """Load the named hash file, or the default if present; None (after a warning) when the default is absent."""
    if explicit is not None:
        return load_hashes(explicit)
    if DEFAULT_HASHES.is_file():
        return load_hashes(DEFAULT_HASHES)
    print(f"warning: {DEFAULT_HASHES} not found; study-token check skipped", file=sys.stderr)
    return None


def _report(lines: Sequence[str]) -> int:
    """Print findings to stdout and return the exit code."""
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} study-token finding(s); the matched text is deliberately not shown", file=sys.stderr)
    return EXIT_FINDINGS if lines else EXIT_OK


def _cmd_hash(args: argparse.Namespace) -> int:
    """Print the hash file for a plain list; warn (by line number) about tokens that can never match."""
    text = args.plain_list.read_text(encoding="utf-8")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#") and not is_matchable(line):
            print(
                f"warning: {args.plain_list}:{number}: token has characters outside letters, digits and _ . - "
                "or edge separators; it can match only as an entire literal or line",
                file=sys.stderr,
            )
    sys.stdout.write(render_hash_file(parse_plain_list(text)))
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    """Check files or directories (default src/) and exit 1 on findings."""
    hashes = _load_for_cli(args.hashes)
    if hashes is None:
        return EXIT_OK
    paths = [Path(p) for p in args.paths] or [Path("src")]
    return _report([str(f) for f in scan_paths(paths, hashes)])


def _cmd_check_commits(args: argparse.Namespace) -> int:
    """Check the content of each commit of the given ranges or shas."""
    hashes = _load_for_cli(args.hashes)
    if hashes is None:
        return EXIT_OK
    results = check_commits(args.repo, args.revs, hashes, history=args.history)
    return _report([f"{sha[:12]} {finding}" for sha, finding in results])


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore.provenance.study_tokens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    hash_cmd = sub.add_parser("hash", help="print the hash file for a plain token list")
    hash_cmd.add_argument("plain_list", type=Path)
    hash_cmd.set_defaults(run=_cmd_hash)
    check = sub.add_parser("check", help="scan files or directories (default: src/)")
    check.add_argument("--hashes", type=Path, default=None, help=f"hash file (default: {DEFAULT_HASHES})")
    check.add_argument("paths", nargs="*")
    check.set_defaults(run=_cmd_check)
    commits = sub.add_parser("check-commits", help="scan src/ and examples/ in each commit of the given ranges or shas")
    commits.add_argument("--hashes", type=Path, default=None, help=f"hash file (default: {DEFAULT_HASHES})")
    commits.add_argument("--repo", type=Path, default=Path.cwd(), help="repository (default: current directory)")
    commits.add_argument("--history", action="store_true", help="a bare REV stands for its whole history")
    commits.add_argument("revs", nargs="+", metavar="REV", help="A..B range or a single commit")
    commits.set_defaults(run=_cmd_check_commits)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return 0 when clean, 1 on findings, 2 on an error."""
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (OSError, ValueError, GitError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
