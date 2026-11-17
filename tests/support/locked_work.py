"""Entries an agent leaves locked in `/work` (its owner's bits cleared), for the tests of O71's collection.

An unprivileged test cannot make an entry the copy helper (which runs as the agent's own uid on the Apptainer backend)
is unable to restore, so `not_the_owner` runs the helper's source as though under another uid: nothing in the tree is
then its own to restore, and an entry it cannot open is the one the listing must name as the agent's.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from expcore_onedata.sandbox.hostops import CmdResult, run_bounded

AS_ANOTHER_UID = "import os\nos.geteuid = lambda: 2**31 - 7\n"
needs_unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 entry: nothing to restore")


def lock(path: Path) -> None:
    """What the agent did: clear every bit of a file or directory it owns."""
    os.chmod(path, 0)


def unlock_tree(path: str | Path) -> None:
    """Give a test's tree its owner's bits back (a failing test leaves some locked), never following a link."""
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        os.chmod(path, 0o700)
        for child in os.listdir(path):
            unlock_tree(os.path.join(path, child))
    elif stat.S_ISREG(info.st_mode):
        os.chmod(path, 0o600)


def locked_afterwards(tree: Callable[[], Path]) -> Iterator[None]:
    """A fixture body: whatever a test left locked under `tree()` is unlocked after it, so the harness can remove it."""
    yield
    root = tree()
    if root.exists():
        unlock_tree(root)


def not_the_owner(source: str) -> str:
    """The copy helper's `source` as run by another uid (the module's docstring)."""
    return AS_ANOTHER_UID + source


class LocalHost:
    """A host whose helper runs as this uid, with no privilege: the Apptainer node's own, as `copyout` sees it."""

    def exists(self, path: str | Path) -> bool:
        return os.path.exists(path)

    def run(self, argv, timeout_s: float, *, sudo: bool = False, input: str | None = None) -> CmdResult:
        return run_bounded(argv, timeout_s, input=input)
