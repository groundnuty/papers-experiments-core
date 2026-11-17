"""The core's identity as the suite sees it: the checkout's own, read once, as if it were committed.

The core records its own identity (`expcore.provenance.core_identity`), read live from git when it runs from a
checkout, and a study command refuses a dirty core unless it allows dirt. The suite runs from the checkout being
changed, so without this every campaign test would pass in CI, where the checkout is clean, and fail on a developer's
machine with uncommitted changes. Every test therefore sees the checkout's identity read once at the start, with its
dirt left out: the same commit, git tree and digest of the source in use, `dirty` false. In CI that is exactly the real
identity. The tests of the identity itself call its readers on repositories of their own, or replace `current` with
the identity they need, and the install tests run real installs in fresh environments, where nothing is pinned.

`apply` pins it in a process the suite starts (`pinned_expcore.py`, `stand_in_runner.py`), so that a command run as
`python -m expcore` sees the identity the test does.
"""

import dataclasses
from functools import cache

from expcore.provenance import core_identity

STAND_IN_COMMIT = "c0de" * 10  # for a tree with no readable commit (an export without .git): a clean stand-in
STAND_IN_TREE = "7ee5" * 10
_real = core_identity.current


@cache
def pinned() -> core_identity.CoreIdentity:
    """The checkout's identity, read once, with its dirt left out; a stand-in commit when it has none."""
    real = _real()
    if not real.known:
        real = dataclasses.replace(real, commit=STAND_IN_COMMIT, tree=STAND_IN_TREE)
    return dataclasses.replace(real, dirty=False, why=None, stale=False, source="checkout")


def apply() -> None:
    """Pin the identity in this process for good: for a process the suite starts to run one command."""
    core_identity.current = pinned
