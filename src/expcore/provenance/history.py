# Provenance: new
"""The maintainer's P07 check: every imported module reproduces from the source blob its header names.

For each module under src/ with a `source:` header, in the committed tree: the header's blob must be the blob
the source repository holds at the named commit and path, and the module's import commit (the first commit that
added the file), minus the header, must equal the sanitisation of that blob under the private map. Runs only
where the source repositories and the map are present.
"""

from collections.abc import Mapping
from pathlib import Path

from expcore.provenance.gitobjects import GitError, read_blob, run_git
from expcore.provenance.headers import HeaderError, read_header, strip_header
from expcore.provenance.sanitise import SanitiseError, sanitise_checked
from expcore.provenance.sanitise_map import TokenMap


def parse_source_repos(spec: str) -> dict[str, Path]:
    """Parse `name=path` pairs separated by semicolons; raise ValueError on a malformed pair."""
    repos: dict[str, Path] = {}
    for pair in filter(None, (part.strip() for part in spec.split(";"))):
        name, separator, path = pair.partition("=")
        if not separator or not name.strip() or not path.strip():
            raise ValueError(f"expected name=path in {pair!r}")
        repos[name.strip()] = Path(path.strip())
    return repos


def _modules(core: Path, root: str) -> list[str]:
    """List the Python files committed under root at HEAD."""
    listing = run_git(core, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", root)
    return sorted(name for name in listing.decode("utf-8").split("\0") if name.endswith(".py"))


def _renamed_from(core: Path, commit: str, path: str) -> str | None:
    """Return the path a commit renamed to `path`: one it deleted in the same commit, never a file it kept.

    Rename detection only (`-M`, no `-C`), over the whole tree, so a new file that resembles one that stays is a new
    file, whatever git's copy detection would say.
    """
    out = run_git(core, "diff-tree", "-r", "-M", "--root", "--no-commit-id", "--name-status", "-z", commit)
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


def _import_commit(core: Path, path: str) -> tuple[str, str] | None:
    """Return (commit, path at that commit) of the commit that started the file's history at HEAD.

    That is the latest commit that added the path, or, when that commit renamed it from a path it deleted, the commit
    that started the earlier path's history, and so on. Git's copy detection (`--follow`) is not used: it would trace
    a hand-edited copy of an imported module to the clean import it resembles (#55).
    """
    rev, current = "HEAD", path
    while True:  # each round starts from an older commit, so the walk ends
        log = run_git(core, "log", "--topo-order", "--no-renames", "--diff-filter=A", "--format=%H", rev, "--", current)
        commits = log.decode("ascii").split()
        if not commits:
            return None
        commit = commits[0]
        earlier = _renamed_from(core, commit, current)
        if earlier is None:
            return commit, current
        rev, current = f"{commit}^", earlier


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
    if repo is None:
        return [f"{path}: source repository {header.source_repo!r} is not among the repositories provided"]
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
    source = run_git(repo, "cat-file", "blob", header.blob or "").decode("utf-8")
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
