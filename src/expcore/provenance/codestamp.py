# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/codestamp.py, blob 4993e3f08818462bea727987e7816de4561a791e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Which code a run came from, when the tree has no .git (2 Nov): `bin/vmsync` and `vm2sync` copy a worktree without .git, and
`runner.harness_commit()` fell back silently to "unknown" with "not dirty", so every run from a synced tree recorded no code version and
looked clean. A snapshot now carries `.code-stamp.json` (the head and a hash of the source tree, written by `tools/stamp_code.py` where
the git repository is) and this module reads it: the commit comes from git when it works, else from the stamp, else it is UNKNOWN (and
unknown counts as dirty, never as clean). A stamp whose tree hash does not match the files beside it is dirty. Campaign start and the
baselines refuse an unknown version (`require_known`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

STAMP = ".code-stamp.json"
ROOTS = (
    "dataset_76",
    "config",
    "protocol",
    "tools",
    "pyproject.toml",
    "uv.lock",
)  # what a run executes or is configured by
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".venv", ".git"}
NOT_HASHED = {
    "protocol/AMENDMENTS.md"
}  # a dated row is a record: nothing a run executes reads it, so a row reaching one host's tree must not hold that host (the lead, 3 Nov)
UNKNOWN = "unknown"


class CodeVersionUnknown(SystemExit):
    pass


def tree_hash(root: Path | str) -> str:
    """sha256 over (relative path, sha256 of content) of every file under ROOTS, in path order. Independent of mtimes and of git."""
    root = Path(root)
    h = hashlib.sha256()
    files: list[Path] = []
    for r in ROOTS:
        p = root / r
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files += [
                f
                for f in p.rglob("*")
                if f.is_file() and not (set(f.relative_to(root).parts) & SKIP_DIRS) and f.suffix != ".pyc"
            ]
    for f in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        if f.relative_to(root).as_posix() in NOT_HASHED:
            continue
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def hashed_root_of(path: Path | str, tree_root: Path | str) -> str | None:
    """The entry of ROOTS that `path` sits in under `tree_root` (symlinks resolved), else None. A file there is part of the tree hash: a launch config edited
    there changes the dispatcher's hash, and the other host, whose tree differs, is held."""
    try:
        rel = Path(path).resolve().relative_to(Path(tree_root).resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts and rel.parts[0] in ROOTS else None


def _git(repo: Path, *a: str) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def write_stamp(dest: Path | str, head: str, tree_root: Path | str | None = None, dirty: bool = False) -> dict:
    """Write DEST/.code-stamp.json for the tree at `tree_root` (default DEST) with the given git head. The head must be a git sha. `dirty`: the
    source had uncommitted changes when it was stamped (`--allow-dirty`): the files are what is stamped, but they are not the head's."""
    dest = Path(dest)
    if len(head) < 7 or any(c not in "0123456789abcdef" for c in head):
        raise ValueError(f"not a git sha: {head!r}")
    s = {
        "head": head,
        "tree_sha256": tree_hash(tree_root or dest),
        "created_utc": dt.datetime.now(dt.UTC).isoformat(),
    }
    if dirty:
        s["source_dirty"] = True  # absent in a clean stamp and in every older one: both read as clean
    (dest / STAMP).write_text(json.dumps(s, indent=1))
    return s


def read_stamp(root: Path | str) -> dict | None:
    try:
        s = json.loads((Path(root) / STAMP).read_text())
    except (OSError, ValueError):
        return None
    return (
        s if isinstance(s, dict) and isinstance(s.get("head"), str) and isinstance(s.get("tree_sha256"), str) else None
    )


def version(root: Path | str) -> dict:
    """{"commit", "dirty", "source": git|stamp|none, "why"}. Unknown is dirty."""
    root = Path(root)
    head = _git(root, "rev-parse", "HEAD")
    if head:
        return {
            "commit": head,
            "dirty": bool(_git(root, "status", "--porcelain", "--untracked-files=no")),
            "source": "git",
            "why": None,
        }
    st = read_stamp(root)
    if st is None:
        return {"commit": UNKNOWN, "dirty": True, "source": "none", "why": f"no git repository and no readable {STAMP}"}
    same = tree_hash(root) == st["tree_sha256"]
    src_dirty = st.get("source_dirty") is True
    why = (
        None
        if same and not src_dirty
        else (
            f"the files differ from the stamped tree ({st['head'][:12]})"
            if not same
            else f"stamped from a source with uncommitted changes ({st['head'][:12]})"
        )
    )
    return {"commit": st["head"], "dirty": not same or src_dirty, "source": "stamp", "stale": not same, "why": why}


def require_known(what: str, root: Path | str) -> dict:
    """Refuse to start `what` on an unknown code version, or on a stamped tree whose files differ from its stamp (a stale stamp names code
    that is not the code being run). A git checkout with uncommitted changes, or a stamp made from one (`--allow-dirty`), is allowed: its record says dirty."""
    v = version(root)
    if (
        v["source"] == "stamp" and v["dirty"] and v.get("stale", True)
    ):  # the stamp names a commit the files beside it are not: a stale stamp must not pass
        # (a stamp of a source that was dirty is not stale: like a dirty git checkout it is allowed, and its record says dirty)
        raise CodeVersionUnknown(
            f"{what}: refusing to start: the stamp is STALE ({v['why']}). Re-stamp the snapshot "
            f"(tools/stamp_code.py) from the checkout whose code is being run."
        )
    if v["commit"] == UNKNOWN:
        raise CodeVersionUnknown(
            f"{what}: refusing to start: the code version is unknown ({v['why']}). Snapshot the tree with the stamp "
            f"(tools/stamp_code.py) or run from a git checkout."
        )
    return v
