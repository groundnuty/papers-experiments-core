# Provenance: new
"""`[sandbox] content_check_provider` (O87): the provider the pre-campaign content check reads through.

The check (`precampaign.py`) reads a provisioned file whole and by range. A read through a provider that does not hold
the file's blocks makes it fetch and keep them, a replica, so a study whose states want every file empty at the agent's
local provider before a run cannot name a file that is fully local. With `content_check_provider = "<a provider's
layout name>"` the check reads through that provider instead, the file's owner, which holds it whole. Unset, it reads
through the local provider, as it always did.

The name is read with the sandbox table (`read`: a layout name, and only with `content_check_file`), and checked when
the platform loads, once its spaces are known (`check`): it must be one of `[providers]` and support the space the
check file lies in, the first space the data path's token reaches. A message names the key, never the provider.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

from expcore.config.loading import Section

if TYPE_CHECKING:
    from expcore_onedata.spaces import StudySpace

KEY = "content_check_provider"
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # a provider's name in `[providers]`


def read(section: Section, check_file: str | None) -> str | None:
    """The optional layout name; ConfigError naming the key when it is no name, or when no check file is declared."""
    name = section.optional_string(KEY)
    if name is None:
        return None
    if not NAME.fullmatch(name):
        raise section.fail(KEY, "must be the name of a provider declared in [providers]")
    if check_file is None:
        raise section.fail(KEY, "is read with content_check_file, which is not set")
    return name


def check(section: Section, name: str | None, providers: Collection[str], spaces: Sequence[StudySpace]) -> None:
    """Refuse a name that is no declared provider, or one that does not support the check file's space."""
    if name is None:
        return
    if name not in providers:
        raise section.fail(KEY, "names a provider that is not in [providers]")
    entry = next((space for space in spaces if "data_path" in space.tokens), spaces[0])
    if name not in entry.support.providers:
        raise section.fail(
            KEY,
            "names a provider that does not support the space the check file lies in (the first the data path reaches)",
        )


__all__ = ["KEY", "check", "read"]
