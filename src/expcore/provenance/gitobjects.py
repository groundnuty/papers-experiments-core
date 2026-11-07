# Provenance: new
"""Small wrappers over the git command line: blob ids, commit ranges, and file content at a commit."""

import hashlib
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed or was given an unusable revision."""


def blob_id(data: bytes) -> str:
    """Return the git blob id (SHA-1 of 'blob <length>' NUL content) of the given bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def run_git(repo: Path, *args: str) -> bytes:
    """Run git in the repository and return stdout; raise GitError with git's message on failure."""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {args[0]} failed in {repo}: {detail}")
    return result.stdout


def resolve_commits(repo: Path, revs: Sequence[str]) -> list[str]:
    """Expand ranges (A..B) to their commits, oldest first; a bare revision names just that commit."""
    commits: list[str] = []
    for rev in revs:
        if rev.startswith("-"):
            raise GitError(f"revision {rev!r} looks like an option")
        if ".." in rev:
            out = run_git(repo, "rev-list", "--reverse", "--end-of-options", rev)
        else:
            out = run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
        commits.extend(out.decode("ascii").split())
    return list(dict.fromkeys(commits))


def changed_files(repo: Path, sha: str) -> list[str]:
    """List the files a commit added, modified or retyped (against every parent), skipping submodules."""
    out = run_git(repo, "diff-tree", "--root", "-r", "-m", "--no-renames", "--no-commit-id", "--raw", "-z", sha)
    fields = out.split(b"\0")
    paths: list[str] = []
    for meta, raw_path in zip(fields[0::2], fields[1::2], strict=False):
        parts = meta.decode("ascii").lstrip(":").split()
        new_mode, status = parts[1], parts[4][0]
        if status in "AMT" and new_mode != "160000":
            paths.append(os.fsdecode(raw_path))
    return list(dict.fromkeys(paths))


def read_blob(repo: Path, sha: str, path: str) -> bytes:
    """Return a file's bytes as committed in the given commit."""
    return run_git(repo, "cat-file", "blob", f"{sha}:{path}")
