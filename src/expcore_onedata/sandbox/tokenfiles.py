# Provenance: new
"""The host's copies of the data-path token, and what a teardown finds where one should be gone (O05).

A copy is written for the one container that reads it at its start and removed as soon as that container has started
(`DockerSandbox._remove_token_files`). Most of the time `rm -f` leaves nothing. When it leaves something, this module
says what, from the path's own metadata (`lstat`) and never its content: its kind, size and owner. One case is handled
rather than failed: an empty directory at the token's path. A bind written `-v SOURCE:PATH` makes a missing source as a
directory owned by root, which is what a `docker cp` from a running container did to the removed token's path; the
sandbox now binds a token with `--mount`, which refuses a missing source (`containers.bind`), and this is the net under
that. Only an empty directory is removed (`rmdir`, which cannot remove anything else); a file, a link or a directory
with an entry in it still fails the teardown, since it may hold the token or something an operator must look at.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.hostops import HostOps

EMPTY_DIRECTORY_REMOVED = "empty directory left by a bind; removed"
EMPTY_DIRECTORY_STUCK = "empty directory left by a bind; could not be removed"
DIRECTORY_LEFT = "directory with entries; left in place"
LEFT = "left in place"
REMOVE_TIMEOUT_S = 20


def kind(mode: int) -> str:
    """What a path is, from its `lstat` mode: file, directory, symlink or other."""
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def facts(path: Path) -> dict[str, Any]:
    """The kind, size and owner's uid of what is at `path`, never followed and never read. A path that cannot be
    examined is of kind `unknown`, with no size and no owner."""
    try:
        found = os.lstat(path)
    except OSError:
        return {"type": "unknown", "size": None, "uid": None}
    return {"type": kind(found.st_mode), "size": found.st_size, "uid": found.st_uid}


def is_empty_directory(path: Path) -> bool:
    """Whether `path` is a directory with no entry, from one non-recursive listing of its names. A directory that
    cannot be listed is not shown empty."""
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def remove(host: HostOps, path: Path, run_directory: Path) -> dict[str, Any] | None:
    """Remove what is at `path` (`rm -f` as root), verified. Nothing left: None. Otherwise what was found, as a record
    holds it: the path relative to the run's directory (never the work root, a configured value), the kind, size and
    owner, and the `action` taken, `EMPTY_DIRECTORY_REMOVED` when it was an empty directory that `rmdir` then removed.
    Any other `action` is a failure: the copy is still there."""
    host.run(["rm", "-f", str(path)], REMOVE_TIMEOUT_S, sudo=True)
    if not os.path.lexists(path):
        return None
    try:
        shown = path.relative_to(run_directory).as_posix()
    except ValueError:
        shown = path.name
    found = {"path": shown, **facts(path)}
    if found["type"] != "directory":
        return {**found, "action": LEFT}
    if not is_empty_directory(path):
        return {**found, "action": DIRECTORY_LEFT}
    host.run(["rmdir", str(path)], REMOVE_TIMEOUT_S, sudo=True)
    gone = not os.path.lexists(path)
    return {**found, "action": EMPTY_DIRECTORY_REMOVED if gone else EMPTY_DIRECTORY_STUCK}


def stuck(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Those of `found` that are still on the host."""
    return [item for item in found if item["action"] != EMPTY_DIRECTORY_REMOVED]
