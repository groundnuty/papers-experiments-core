# Provenance: new
"""The maintainer's P07 check: every imported module reproduces from the source blob its header names.

For each module under src/ with a `source:` header, in the committed tree: the header's blob must be the blob
the source repository holds at the named commit and path, and the module's import commit (the commit that started
its history: see `_import_commit`), minus the header, must equal the sanitisation of that blob under the private map.
Runs only where the source repositories and the map are present:

    python -m expcore.provenance.history --map MAP --source-repos "NAME=PATH;NAME=PATH" [--repo DIR] [--root src]

`--source-repos` defaults to `EXPCORE_SOURCE_REPOS`. The command prints each problem and exits 1, or exits 0 when
every module holds; 2 is an error (an unreadable map, an unknown repository setting, a git failure).
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from expcore.provenance.gitobjects import GitError, read_blob, run_git
from expcore.provenance.hashfile import hashes_from_tokens
from expcore.provenance.headers import HeaderError, read_header, strip_header
from expcore.provenance.redact import Redactor
from expcore.provenance.sanitise import SanitiseError, sanitise_checked
from expcore.provenance.sanitise_map import TokenMap, load_map
from expcore.provenance.study_tokens import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK

SOURCE_REPOS_VARIABLE = "EXPCORE_SOURCE_REPOS"


def parse_source_repos(spec: str) -> dict[str, Path]:
    """Parse `name=path` pairs separated by semicolons; raise ValueError on a malformed pair.

    The names are the earlier studies' repositories, which are private, so an error names a pair by its position only.
    """
    repos: dict[str, Path] = {}
    for number, pair in enumerate(filter(None, (part.strip() for part in spec.split(";"))), start=1):
        name, separator, path = pair.partition("=")
        if not separator or not name.strip() or not path.strip():
            raise ValueError(f"source repository {number} (counted from 1, between ';'): expected NAME=PATH")
        repos[name.strip()] = Path(path.strip())
    return repos


def _modules(core: Path, root: str) -> list[str]:
    """List the Python files committed under root at HEAD."""
    listing = run_git(core, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", root)
    return sorted(name for name in listing.decode("utf-8").split("\0") if name.endswith(".py"))


def _renamed_from(core: Path, commit: str, path: str, *, merge: bool) -> str | None:
    """Return the path a commit renamed to `path`: one it deleted in the same commit, never a file it kept.

    Rename detection only (`-M`, no `-C`), over the whole tree, so a new file that resembles one that stays is a new
    file, whatever git's copy detection would say. A merge is compared with its first parent.
    """
    against = (f"{commit}^1", commit) if merge else ("--root", commit)
    out = run_git(core, "diff-tree", "-r", "-M", "--no-commit-id", "--name-status", "-z", *against)
    fields = out.decode("utf-8").split("\0")
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        if status[:1] in "RC":
            old, new = fields[index + 1], fields[index + 2]
            if status[0] == "R" and new == path:
                return old
            index += 3
        else:
            index += 2
    return None


def _holds(core: Path, rev: str, path: str) -> bool:
    """Whether a commit's tree holds the path."""
    try:
        run_git(core, "cat-file", "-e", "--end-of-options", f"{rev}:{path}")
    except GitError:
        return False
    return True


def _latest_add(core: Path, rev: str, path: str) -> tuple[str, bool] | None:
    """The latest commit in the path's history from `rev` that added it, and whether it is a merge.

    The history is git's default one for the path: a merge whose file is exactly one parent's (a branch brought up to
    date with main, say) is not part of it, since the file came from that parent. A merge that is part of it, holding
    the file as no parent does, added it when its first parent did not hold it: a module a merge adds, or adds back
    edited after it was deleted, starts its history there.
    """
    added = set(
        run_git(core, "log", "--no-renames", "--diff-filter=A", "--format=%H", rev, "--", path).decode().split()
    )
    for line in run_git(core, "log", "--topo-order", "--format=%H %P", rev, "--", path).decode().splitlines():
        commit, *parents = line.split()
        if len(parents) < 2 and commit in added:
            return commit, False
        if len(parents) >= 2 and not _holds(core, f"{commit}^1", path) and _holds(core, commit, path):
            return commit, True
    return None


def _import_commit(core: Path, path: str) -> tuple[str, str] | None:
    """Return (commit, path at that commit) of the commit that started the file's history at HEAD.

    That is the latest commit that added the path (`_latest_add`, merges included), or, when that commit renamed it
    from a path it deleted, the commit that started the earlier path's history, and so on. Git's copy detection
    (`--follow`) is not used: it would trace a hand-edited copy of an imported module to the clean import it
    resembles (#55).
    """
    rev, current = "HEAD", path
    while True:  # each round starts from an older commit, so the walk ends
        found = _latest_add(core, rev, current)
        if found is None:
            return None
        commit, merge = found
        earlier = _renamed_from(core, commit, current, merge=merge)
        if earlier is None:
            return commit, current
        rev, current = f"{commit}^1", earlier


def _source_blob(repo: Path, commit: str, path: str) -> str | None:
    """Return the blob id the source repository holds at the commit and path, or None when it holds none."""
    try:
        return run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{commit}:{path}").decode("ascii").strip()
    except GitError:
        return None


def _check_module(core: Path, path: str, sources: Mapping[str, Path], token_map: TokenMap) -> list[str]:
    """Return the problems of one module; empty when it is new, or imported and exactly reproducible."""
    try:
        header = read_header(read_blob(core, "HEAD", path).decode("utf-8"))
    except HeaderError as error:
        return [f"{path}: unreadable provenance header: {error}"]
    if header is None:
        return [f"{path}: has no provenance header"]
    if header.new:
        return []
    repo = sources.get(header.source_repo or "")
    if repo is None:  # the name is the earlier study's, private: the module's own path says which header to read
        return [f"{path}: the source repository its header names is not among the ones provided"]
    if _source_blob(repo, header.commit or "", header.path or "") != header.blob:
        return [f"{path}: header blob is not the blob of the named source commit and path"]
    added = _import_commit(core, path)
    if added is None:
        return [f"{path}: no commit adding the file was found"]
    commit, then = added
    imported = read_blob(core, commit, then).decode("utf-8")
    try:
        at_import = read_header(imported)
    except HeaderError as error:
        return [f"{path}: import commit {commit[:12]} has an unreadable header: {error}"]
    if at_import is None or at_import.new or at_import.blob != header.blob:
        return [f"{path}: import commit {commit[:12]} does not carry the source header that is current now"]
    if at_import.map_sha256 != token_map.sha256:
        return [f"{path}: import commit {commit[:12]} names a different sanitising map than the one provided"]
    try:
        source = run_git(repo, "cat-file", "blob", header.blob or "").decode("utf-8")
    except GitError:  # its message names the source repository's path
        return [f"{path}: the source blob cannot be read from the source repository its header names"]
    try:
        expected = sanitise_checked(source, token_map.entries, filename=path)
    except SanitiseError as error:
        return [f"{path}: the source no longer sanitises cleanly: {error}"]
    if strip_header(imported) != expected:
        return [f"{path}: import commit {commit[:12]} differs from the sanitised source blob"]
    return []


def verify_history(core: Path, sources: Mapping[str, Path], token_map: TokenMap, *, root: str = "src") -> list[str]:
    """Check every module under root of the core repository; return the problems found (empty when all hold)."""
    problems: list[str] = []
    for path in _modules(core, root):
        problems.extend(_check_module(core, path, sources, token_map))
    return problems


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore.provenance.history", description=__doc__.split("\n\n")[0])
    parser.add_argument("--map", type=Path, required=True, help="the private sanitising map the imports were made with")
    parser.add_argument(
        "--source-repos",
        default=None,
        help=f"NAME=PATH pairs separated by ';', one per source repository (default: ${SOURCE_REPOS_VARIABLE})",
    )
    parser.add_argument("--repo", type=Path, default=Path(), help="the repository to check (default: here)")
    parser.add_argument("--root", default="src", help="the directory whose modules are checked (default: src)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check; return 0 when every module holds, 1 when one does not, 2 on an error or when there is no module.

    Every line goes through one redactor that knows the map's tokens (`provenance.redact`), so no message shows a name
    the map hides, whatever a header or an error holds; a source repository is never named, only the module.
    """
    args = build_parser().parse_args(argv)
    spec = args.source_repos if args.source_repos is not None else os.environ.get(SOURCE_REPOS_VARIABLE, "")
    try:
        sources = parse_source_repos(spec)
        if not sources:
            raise ValueError(f"name the source repositories with --source-repos or {SOURCE_REPOS_VARIABLE}")
        token_map = load_map(args.map)
    except (OSError, ValueError) as error:  # MapError is a ValueError; neither message holds a value of the map
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    redactor = Redactor(hashes_from_tokens(entry.token for entry in token_map.entries))
    try:
        modules = _modules(args.repo, args.root)
        problems = verify_history(args.repo, sources, token_map, root=args.root)
    except GitError:  # its message names a repository's path
        print("error: the repository checked cannot be read; is --repo a repository with a commit?", file=sys.stderr)
        return EXIT_ERROR
    if not modules:
        print(
            redactor.scrub(f"error: no Python module under {args.root} at HEAD, so nothing was checked"),
            file=sys.stderr,
        )
        return EXIT_ERROR
    for problem in problems:
        print(redactor.scrub(f"FAIL {problem}"))
    if not problems:
        imported = len(modules) - len(_new_modules(args.repo, modules))
        print(redactor.scrub(f"ok: every module under {args.root} holds ({len(modules)} modules, {imported} imported)"))
    return EXIT_FINDINGS if problems else EXIT_OK


def _new_modules(core: Path, modules: Sequence[str]) -> list[str]:
    """The modules whose header says they were written fresh (`# Provenance: new`)."""
    new = []
    for path in modules:
        try:
            header = read_header(read_blob(core, "HEAD", path).decode("utf-8"))
        except (HeaderError, UnicodeDecodeError):
            continue
        if header is not None and header.new:
            new.append(path)
    return new


if __name__ == "__main__":
    raise SystemExit(main())
