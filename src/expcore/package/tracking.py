# Provenance: new
"""Which included files the study's repository does not hold as committed (#61).

The code identity vouches for the code, and the replay check and the regeneration for what is under `out/`, which is
generated and normally ignored. Any other file a package includes (a README, `package.toml`, a figure) has nothing
vouching for it but the commit it came from, so the build says which ones no commit holds: `untracked` (git does not
track it, ignored or not) and `modified` (it differs from HEAD, staged or not). They are warned about and listed in
`PACKAGE.json`, and an anonymised build, whose identity is a tree hash and can name no commit, refuses them unless the
author passes `--allow-uncommitted`.

A study outside any repository (a stamped snapshot) has no commit to compare with: `vcs` is `none`, the build says it
cannot tell, and nothing is refused, because no file is known to be uncommitted.
"""

import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from expcore.package.spec import PackageError
from expcore.provenance.gitobjects import GitError, run_git
from expcore.provenance.redact import Redactor

GIT, NO_VCS = "git", "none"
UNTRACKED, MODIFIED = "untracked", "modified"


@dataclass(frozen=True)
class Tracking:
    """What the repository says about the files asked about: the kind of repository and each uncommitted file."""

    vcs: str
    uncommitted: tuple[tuple[str, str], ...] = ()

    def entries(self) -> list[dict[str, str]]:
        """The uncommitted files as `PACKAGE.json` lists them."""
        return [{"path": path, "state": state} for path, state in self.uncommitted]


def _names(raw: bytes) -> set[str]:
    return {name for name in raw.decode("utf-8", errors="surrogateescape").split("\0") if name}


def tracking(root: Path, paths: Collection[str]) -> Tracking:
    """Say which of `paths`, relative to the study, the repository around it does not hold as committed."""
    try:
        tracked = _names(run_git(root, "ls-files", "-z"))
        changed = _names(run_git(root, "diff", "--name-only", "-z", "--relative", "HEAD"))
    except (GitError, OSError):
        return Tracking(NO_VCS)
    found = []
    for path in sorted(paths):
        if path not in tracked:
            found.append((path, UNTRACKED))
        elif path in changed:
            found.append((path, MODIFIED))
    return Tracking(GIT, tuple(found))


def note_tracking(
    root: Path,
    paths: Collection[str],
    redactor: Redactor,
    anonymised: bool,
    allow_uncommitted: bool,
    warn: Callable[[str], None] | None,
) -> Tracking:
    """Warn about the files of `paths` no commit holds, or refuse them in an anonymised package; return the state.

    Each path is named through `redactor`, the one the build's checks share, so one that holds a private name or a
    term of the anonymisation list is shown only by its hidden name, in the warning and in the refusal alike.
    """
    say = warn or (lambda line: print(f"warning: {line}", file=sys.stderr))
    state = tracking(root, paths)
    if state.vcs == NO_VCS:
        say("the study is not in a repository, so whether its included files are committed cannot be told")
    if not state.uncommitted:
        return state
    shown = ", ".join(f"{redactor.name(path)} ({kind})" for path, kind in state.uncommitted)
    if anonymised and not allow_uncommitted:
        raise PackageError(
            f"an anonymised package names no commit, so it cannot include files no commit holds: {shown}; commit them, "
            "leave them out, or pass --allow-uncommitted",
            private=redactor.hidden,
        )
    say(f"included but no commit holds them, listed in PACKAGE.json: {shown}")
    return state
