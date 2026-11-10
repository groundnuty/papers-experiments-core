# Provenance: new
"""Study-token hashing and the check that no commit of this public repository contains a study's name.

A commit is checked in its files and in its message: both are published with the repository.

The plain token list stays private. The repository holds only SHA-256 hashes, and every finding reports a
hash prefix, never the text that matched, because CI logs are public. Hashing and candidate extraction live in
`candidates` and `hashfile`, scanning in `scanner`; this module finds the files to scan and is the command line.
"""

import argparse
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from expcore.provenance.acknowledged import (
    DEFAULT_ACKNOWLEDGED,
    load_acknowledgements,
    refuse_current_commit,
    split_acknowledged,
)
from expcore.provenance.candidates import (
    MAX_CANDIDATE_CHARS,
    MAX_COMPONENTS,
    WHOLE_ONLY_BELOW,
    candidates,
    is_matchable,
    normalise,
    token_hash,
)
from expcore.provenance.gitobjects import (
    GitError,
    changed_files,
    commit_message,
    read_blob,
    resolve_commits,
    run_git,
)
from expcore.provenance.hashfile import (
    Hashes,
    hashes_from_tokens,
    parse_hash_file,
    parse_plain_entries,
    parse_plain_list,
    render_hash_file,
)
from expcore.provenance.scanner import Finding, scan_message, scan_source

__all__ = [
    "MAX_CANDIDATE_CHARS",
    "MAX_COMPONENTS",
    "WHOLE_ONLY_BELOW",
    "Finding",
    "Hashes",
    "MissingHashesError",
    "candidates",
    "check_commits",
    "hashes_from_tokens",
    "is_matchable",
    "load_hashes",
    "main",
    "normalise",
    "parse_hash_file",
    "parse_plain_list",
    "render_hash_file",
    "repository_files",
    "scan_message",
    "scan_paths",
    "scan_source",
    "token_hash",
]

DEFAULT_HASHES = Path("tests/data/study_tokens.sha256")
EXCLUDED_PATHS = frozenset({DEFAULT_HASHES.as_posix()})
SKIPPED_DIRS = frozenset({"__pycache__", ".git", ".venv"})

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


class MissingHashesError(ValueError):
    """The hash file is absent or empty, so the check cannot say anything."""


def load_hashes(path: Path) -> Hashes:
    """Load a hash file from disk."""
    return parse_hash_file(path.read_text(encoding="utf-8"), str(path))


def _decode(data: bytes) -> str | None:
    """Decode file bytes as UTF-8 (BOM dropped, bad bytes replaced); None for binary content."""
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace")


def _display(path: Path) -> str:
    """Show a path relative to the working directory when it lies inside it."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _walk(root: Path) -> list[Path]:
    """List the files below a directory, skipping caches and the git directory."""
    files = (p for p in root.rglob("*") if p.is_file())
    return sorted(p for p in files if not SKIPPED_DIRS.intersection(p.relative_to(root).parts))


def repository_files(root: Path = Path()) -> list[Path]:
    """List tracked and untracked-but-not-ignored files under root (all files if root is not in a git work tree)."""
    try:
        listing = run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except (GitError, OSError):
        return _walk(root)
    names = sorted({os.fsdecode(name) for name in listing.split(b"\0") if name})
    return [root / name for name in names if (root / name).is_file()]


def _scan_bytes(path: str, data: bytes, hashes: Hashes) -> list[Finding]:
    """Scan a file's path always, and its content unless it is binary."""
    text = _decode(data)
    return scan_source(path, "" if text is None else text, hashes)


def scan_paths(paths: Iterable[Path], hashes: Hashes) -> list[Finding]:
    """Scan files and directories (recursively); raise FileNotFoundError for a path that does not exist."""
    findings: list[Finding] = []
    for root in paths:
        if not root.exists():
            raise FileNotFoundError(f"no such path: {root}")
        for file in [root] if root.is_file() else _walk(root):
            shown = _display(file)
            if shown not in EXCLUDED_PATHS:
                findings.extend(_scan_bytes(shown, file.read_bytes(), hashes))
    return findings


def check_commits(
    repo: Path, revs: Sequence[str], hashes: Hashes, *, history: bool = False
) -> list[tuple[str, Finding]]:
    """Scan every file each commit added or modified, at that commit's content, and each commit's full message.

    A message is prose: it is checked against the `all`-scope hashes only. Its findings come first, with the path
    `<commit message>`.
    """
    results: list[tuple[str, Finding]] = []
    for sha in resolve_commits(repo, revs, history=history):
        results.extend((sha, f) for f in scan_message(commit_message(repo, sha), hashes))
        for path in changed_files(repo, sha):
            if path not in EXCLUDED_PATHS:
                results.extend((sha, f) for f in _scan_bytes(path, read_blob(repo, sha, path), hashes))
    return results


def _missing(reason: str, allow: bool) -> None:
    """Refuse to run without hashes, or (when allowed) warn that the check is skipped."""
    if not allow:
        raise MissingHashesError(f"{reason}; the check cannot run (pass --allow-missing-hashes to skip it)")
    print(f"warning: {reason}; study-token check skipped", file=sys.stderr)


def _load_for_cli(explicit: Path | None, allow_missing: bool) -> Hashes | None:
    """Load the named or default hash file; None only when it is absent or empty and that is allowed."""
    path = explicit or DEFAULT_HASHES
    try:
        hashes = load_hashes(path)
    except FileNotFoundError:
        _missing(f"{path} not found", allow_missing)
        return None
    if not hashes:
        _missing(f"{path} holds no hashes", allow_missing)
        return None
    origin = "the default list" if explicit is None else "the list given with --hashes"
    print(f"study-token check: {count_hashes(hashes)}, from {origin}", file=sys.stderr)  # never the path
    return hashes


def count_hashes(hashes: Hashes) -> str:
    """How many hashes a loaded file holds, of either match: `1 hash`, `12 hashes`."""
    count = len(hashes.anywhere) + len(hashes.whole)
    return f"{count} hash" if count == 1 else f"{count} hashes"


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
    entries = parse_plain_entries(text)
    for entry in entries:
        if not is_matchable(entry.token):
            print(
                f"warning: {args.plain_list}:{entry.line}: token has characters outside letters, digits and _ . - "
                "or edge separators; it can match only as an entire literal or line",
                file=sys.stderr,
            )
    sys.stdout.write(render_hash_file((entry.token, entry.scope, entry.whole) for entry in entries))
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    """Check files or directories (default: the whole repository) and exit 1 on findings."""
    hashes = _load_for_cli(args.hashes, args.allow_missing_hashes)
    if hashes is None:
        return EXIT_OK
    findings = scan_paths([Path(p) for p in args.paths], hashes) if args.paths else _scan_repository(hashes)
    return _report([str(f) for f in findings])


def _scan_repository(hashes: Hashes) -> list[Finding]:
    """Scan every tracked or untracked-but-not-ignored text file under the working directory."""
    return scan_paths(repository_files(Path()), hashes)


def _cmd_check_commits(args: argparse.Namespace) -> int:
    """Check the content of each commit of the given ranges or shas."""
    hashes = _load_for_cli(args.hashes, args.allow_missing_hashes)
    if hashes is None:
        return EXIT_OK
    results = check_commits(args.repo, args.revs, hashes, history=args.history)
    source = args.acknowledged or DEFAULT_ACKNOWLEDGED
    entries = load_acknowledgements(source, required=args.acknowledged is not None)
    refuse_current_commit(entries, args.repo, str(source))
    remaining, acknowledged = split_acknowledged(results, entries)
    if acknowledged:
        print(f"{len(acknowledged)} finding(s) acknowledged in {source}", file=sys.stderr)
    return _report([f"{sha[:12]} {finding}" for sha, finding in remaining])


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore.provenance.study_tokens", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    hash_cmd = sub.add_parser("hash", help="print the hash file for a plain token list")
    hash_cmd.add_argument("plain_list", type=Path)
    hash_cmd.set_defaults(run=_cmd_hash)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hashes", type=Path, default=None, help=f"hash file (default: {DEFAULT_HASHES})")
    common.add_argument(
        "--allow-missing-hashes",
        action="store_true",
        help="warn and exit 0 when the hash file is absent or empty (never used by CI or make)",
    )
    check = sub.add_parser("check", parents=[common], help="scan files or directories (default: the repository)")
    check.add_argument("paths", nargs="*")
    check.set_defaults(run=_cmd_check)
    commits = sub.add_parser("check-commits", parents=[common], help="scan each commit of the given ranges or shas")
    commits.add_argument("--repo", type=Path, default=Path.cwd(), help="repository (default: current directory)")
    commits.add_argument("--history", action="store_true", help="a bare REV stands for its whole history")
    commits.add_argument(
        "--acknowledged",
        type=Path,
        default=None,
        help=f"findings in already-pushed commits to accept (default: {DEFAULT_ACKNOWLEDGED} if present)",
    )
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
