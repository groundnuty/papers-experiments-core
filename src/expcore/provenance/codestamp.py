# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/codestamp.py, blob 4993e3f08818462bea727987e7816de4561a791e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Which code a run came from (P01, P02, P03).

A run's code identity is its commit plus a hash of the tree under the scope's roots: the code and the data files the
code reads (P02). The commit comes from git when it works, else from `.code-stamp.json`, which a snapshot copied
without `.git` carries (the head and the tree hash, written by `write_stamp` where the repository is), else it is
UNKNOWN. Unknown counts as dirty, never as clean (P01). So does a git status that cannot be read, a stamp whose
head is not a git sha, and any file the tree hash covers that git does not track, ignored or not: the hash includes
it, the commit does not. A stamp
whose tree hash does not match the files beside it is stale. The tree hash ignores modification times, caches and
bytecode (P03). `require_known` refuses to start a study run on unknown, stale or, unless allowed, dirty code.

What the identity covers is a parameter, not a constant: a study names its own roots and the files under them that
are records rather than code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

STAMP = ".code-stamp.json"
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".venv", ".git"})
SKIP_SUFFIXES = frozenset({".pyc"})
UNKNOWN = "unknown"
_HEX = frozenset("0123456789abcdef")
_SHA_CHARS = 40


class CodeVersionUnknown(RuntimeError):
    """The code version is unknown, stale or dirty, and the caller asked for a version it can rely on."""


class CodeScopeError(ValueError):
    """The scope names nothing to hash, or a root that points outside the tree."""


@dataclass(frozen=True)
class CodeScope:
    """What a code identity covers.

    `roots` are files and directories relative to the tree root: the code, and the data files and configuration the
    code reads or is configured by. `not_hashed` are files under the roots that a run never reads, such as a dated
    log of amendments, so that adding a row to one does not change the identity. `skip_dirs` are never entered.
    """

    roots: tuple[str, ...]
    not_hashed: frozenset[str] = frozenset()
    skip_dirs: frozenset[str] = SKIP_DIRS

    def __post_init__(self) -> None:
        """Refuse an empty scope and any root that is absolute or climbs out of the tree."""
        if not self.roots:
            raise CodeScopeError("a code scope needs at least one root")
        for root in self.roots:
            path = PurePosixPath(root)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise CodeScopeError(f"root {root!r} must be a relative path inside the tree")

    @classmethod
    def of(cls, roots: Iterable[str], not_hashed: Collection[str] = (), skip_dirs: Collection[str] = SKIP_DIRS):
        """Build a scope from any iterables."""
        return cls(tuple(roots), frozenset(not_hashed), frozenset(skip_dirs))


def _files(root: Path, scope: CodeScope) -> list[Path]:
    """List the files the identity covers, in path order, never following caches or the excluded names."""
    found: list[Path] = []
    for name in scope.roots:
        path = root / name
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found += [
                f
                for f in path.rglob("*")
                if f.is_file()
                and not (set(f.relative_to(root).parts) & scope.skip_dirs)
                and f.suffix not in SKIP_SUFFIXES
            ]
    kept = [f for f in found if f.relative_to(root).as_posix() not in scope.not_hashed]
    return sorted(kept, key=lambda f: f.relative_to(root).as_posix())


def covered_files(root: Path | str, scope: CodeScope) -> list[str]:
    """Return the relative paths of the files the identity covers, in path order."""
    root = Path(root)
    return [f.relative_to(root).as_posix() for f in _files(root, scope)]


def tree_hash(root: Path | str, scope: CodeScope) -> str:
    """Return the sha256 over (relative path, sha256 of content) of every covered file, in path order.

    Independent of modification times and of git (P03). Raises CodeScopeError when no file is covered, because an
    identity over nothing would pass for any code.
    """
    root = Path(root)
    files = _files(root, scope)
    if not files:
        raise CodeScopeError(f"no file under {', '.join(scope.roots)} in {root}: the identity would cover no code")
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return digest.hexdigest()


def hashed_root_of(path: Path | str, tree_root: Path | str, scope: CodeScope) -> str | None:
    """Return the root of the scope that `path` sits in under `tree_root` (symlinks resolved), else None.

    A file there is part of the tree hash, so a configuration edited there changes the identity.
    """
    try:
        rel = Path(path).resolve().relative_to(Path(tree_root).resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts and rel.parts[0] in {PurePosixPath(r).parts[0] for r in scope.roots} else None


def is_sha(head: object) -> bool:
    """Tell whether a value is a full git sha: forty lowercase hexadecimal characters."""
    return isinstance(head, str) and len(head) == _SHA_CHARS and not set(head) - _HEX


def _git(repo: Path, *args: str, raw: bool = False) -> str | None:
    """Run git in the repository; None when git cannot run or fails, never an empty string for a failure.

    The output is stripped of surrounding whitespace unless raw, which NUL-delimited listings need, because a file
    name may start or end with a space.
    """
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout if raw else done.stdout.strip()


def write_stamp(
    dest: Path | str,
    head: str,
    scope: CodeScope,
    tree_root: Path | str | None = None,
    dirty: bool = False,
    created_utc: str | None = None,
):
    """Write DEST/.code-stamp.json for the tree at `tree_root` (default DEST) with the given git head.

    The head must be a full git sha. `dirty` says the source had uncommitted changes when it was stamped: the files are
    what is stamped, but they are not the head's. `created_utc` is the time the stamp gives, now by default; a
    replication package passes a time taken from its records, so two builds of one tree write the same stamp.
    """
    dest = Path(dest)
    if not is_sha(head):
        raise ValueError(f"not a git sha: {head!r}")
    stamp = {
        "head": head,
        "tree_sha256": tree_hash(tree_root or dest, scope),
        "created_utc": dt.datetime.now(dt.UTC).isoformat() if created_utc is None else created_utc,
    }
    if dirty:
        stamp["source_dirty"] = True  # absent in a clean stamp and in every older one: both read as clean
    (dest / STAMP).write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    return stamp


def read_stamp(root: Path | str) -> dict | None:
    """Read the stamp beside the files; None when it is absent, unreadable, or lacks a git sha head and a tree hash.

    A head that is empty or not a sha names no code, so such a stamp is read as no stamp: the version is unknown (P01).
    """
    try:
        stamp = json.loads((Path(root) / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(stamp, dict) and is_sha(stamp.get("head")) and isinstance(stamp.get("tree_sha256"), str)
    return stamp if ok else None


def _untracked_hashed(root: Path, scope: CodeScope) -> list[str] | None:
    """List the files the tree hash covers that git does not track, ignored or not; None when git cannot list them.

    The hashed set minus `git ls-files`. A new file under a root changes the hash while `git status
    --untracked-files=no` stays silent about it, and so does a file a .gitignore names. Caches and the scope's
    `not_hashed` files are not in the hashed set, so they are in neither side.
    """
    listing = _git(root, "ls-files", "-z", raw=True)
    if listing is None:
        return None
    tracked = {name for name in listing.split("\0") if name}
    return [name for name in (f.relative_to(root).as_posix() for f in _files(root, scope)) if name not in tracked]


def _dirty_reason(root: Path, scope: CodeScope) -> str | None:
    """Say why a git checkout is dirty, else None.

    Dirty is a change to a tracked file, a hashed file git does not track, or a git that cannot say.
    """
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status is None:
        return "git status could not be read"
    if status:
        return "uncommitted changes"
    untracked = _untracked_hashed(root, scope)
    if untracked is None:
        return "git status could not be read"
    if untracked:
        more = f" and {len(untracked) - 3} more" if len(untracked) > 3 else ""
        return f"hashed files that git does not track: {', '.join(untracked[:3])}{more}"
    return None


def version(root: Path | str, scope: CodeScope, *, use_git: bool = True) -> dict:
    """Return the code identity: {"commit", "dirty", "source": git|stamp|none, "stale", "why", "tree_sha256"}.

    Unknown is dirty. A git status that fails is dirty, not clean, and so is a hashed file git does not track.
    `tree_sha256` is recomputed on every call, so the identity is re-verified each time it is asked for (P02);
    it is None only when the version is unknown. With `use_git` false only the stamp names the commit: a replication
    package has no repository of its own and may be unpacked inside another one, whose head is not its code.
    """
    root = Path(root)
    head = _git(root, "rev-parse", "HEAD") if use_git else None
    if head:
        why = _dirty_reason(root, scope)
        return {
            "commit": head,
            "dirty": why is not None,
            "source": "git",
            "stale": False,
            "why": why,
            "tree_sha256": tree_hash(root, scope),
        }
    stamp = read_stamp(root)
    if stamp is None:
        return {
            "commit": UNKNOWN,
            "dirty": True,
            "source": "none",
            "stale": False,
            "why": f"no git repository and no readable {STAMP}",
            "tree_sha256": None,
        }
    tree = tree_hash(root, scope)
    stale = tree != stamp["tree_sha256"]
    source_dirty = stamp.get("source_dirty") is True
    why = None
    if stale:
        why = f"the files differ from the stamped tree ({stamp['head'][:12]})"
    elif source_dirty:
        why = f"stamped from a source with uncommitted changes ({stamp['head'][:12]})"
    return {
        "commit": stamp["head"],
        "dirty": stale or source_dirty,
        "source": "stamp",
        "stale": stale,
        "why": why,
        "tree_sha256": tree,
    }


def verify_identity(root: Path | str, scope: CodeScope, commit: str, tree_sha256: str | None) -> list[str]:
    """Check that the tree at `root` is the code a record names; return the problems, empty when it is.

    The commit and the tree hash both have to match: code files and the data files they read are re-hashed now.
    """
    now = version(root, scope)
    problems = []
    if now["commit"] != commit:
        problems.append(f"the code is at commit {now['commit']}, the record names {commit}")
    if tree_sha256 is None or now["tree_sha256"] != tree_sha256:
        problems.append("the hash of the code and data files differs from the one the record names")
    return problems


def require_known(what: str, root: Path | str, scope: CodeScope, *, allow_dirty: bool = False) -> dict:
    """Refuse to start `what` on code that cannot be vouched for, and return the identity otherwise (P01).

    Refused always: an unknown version, and a stamp whose files differ from it (it names code that is not the code
    being run). Refused unless `allow_dirty`: uncommitted changes, an unreadable git status, or a stamp made from a
    dirty source. A pilot may pass `allow_dirty`; its record then says dirty.
    """
    v = version(root, scope)
    if v["commit"] == UNKNOWN:
        raise CodeVersionUnknown(
            f"{what}: refusing to start: the code version is unknown ({v['why']}). Run from a git checkout, "
            f"or stamp the snapshot with write_stamp where the repository is."
        )
    if v["stale"]:
        raise CodeVersionUnknown(
            f"{what}: refusing to start: the stamp is stale ({v['why']}). Stamp the snapshot again "
            f"from the checkout whose code is being run."
        )
    if v["dirty"] and not allow_dirty:
        raise CodeVersionUnknown(f"{what}: refusing to start on dirty code ({v['why']}). Commit it, or allow dirty.")
    return v
