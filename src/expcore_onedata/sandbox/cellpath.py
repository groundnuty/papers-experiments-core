# Provenance: new
"""A path in the agent's cell, as the cell resolves it by name alone (O73).

The sandbox keeps a short list of paths that are its own (`apptainer.settings.OWN_TARGETS`, `inputs.TAKEN`, the data
mount and the trust paths) and refuses a study's bind or input at, over or under one of them. A path has more than one
spelling: `/etc/./hostname`, `/etc//hostname` and `/etc/hostname` are one file, as are `//work` and `/./tmp` the
directories they name, and `/.` and `//` the root. A check that compared the text let each of the first spellings
through to the runtime, which resolved it to the protected path (found by Codex, 17 Nov 2026). Every such comparison is
made on `normal(path)`.
"""

from __future__ import annotations

import posixpath


def normal(path: str) -> str:
    """`path` with its `.` and empty parts removed and each `x/..` collapsed (`posixpath.normpath`), and a leading `//`,
    which `normpath` keeps because POSIX leaves it to the system, made one `/`: `/etc/./hostname` and `/etc//hostname`
    are `/etc/hostname`, `//work` is `/work`, `/.` and `//` are `/`. A path that is not absolute is only normalised; it
    is the caller's to refuse. A pure function of the text: no link is followed and no file is read."""
    if not path.startswith("/"):
        return posixpath.normpath(path)
    return "/" + posixpath.normpath(path).lstrip("/")


__all__ = ["normal"]
