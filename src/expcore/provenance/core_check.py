# Provenance: new
"""The core's own identity holds a study command to the rule the study's code identity follows (P01).

`require_core` is called where `codestamp.require_known` is, for `run`, `check`, `merge` and `analyze`, with the same
`allow_dirty`: a core whose commit is unknown, or an installed core whose source differs from the build that names it
(stale), is refused always; a dirty core (a checkout with uncommitted changes, or a wheel built from one) is refused
unless `allow_dirty`, which a pilot may pass, and its environment then records it dirty. So a scored or merged result
never comes from a dirty core unless the command allowed it, and every comparison of the environment sees the dirt,
since the entry says it. A replication package's own check is the exception: it runs on the core its wheels install,
pinned by hash, and its build refuses a check recorded under a dirty or unknown core (`recorded_core_problem`).
"""

from collections.abc import Mapping
from typing import Any

from expcore.provenance import core_identity
from expcore.provenance.codestamp import CodeVersionUnknown
from expcore.provenance.environment import core_entry, is_earlier_form

INSTALL_HINT = "install the core from its repository: a checkout, a git URL, or a wheel built from either"


class CoreVersionUnknown(CodeVersionUnknown):
    """The core in use is unknown, stale or dirty, and the command asked for a core it can rely on."""


def require_core(what: str, *, allow_dirty: bool = False) -> core_identity.CoreIdentity:
    """Refuse to start `what` on a core that cannot be vouched for, and return its identity otherwise."""
    found = core_identity.current()
    if not found.known:
        raise CoreVersionUnknown(
            f"{what}: refusing to start: the core's commit is unknown ({found.why}); {INSTALL_HINT}"
        )
    if found.stale:
        raise CoreVersionUnknown(
            f"{what}: refusing to start: the installed core is stale ({found.why}); install it again"
        )
    if found.dirty and not allow_dirty:
        raise CoreVersionUnknown(
            f"{what}: refusing to start on a dirty core ({found.why}). Commit it and install it again, or allow dirty."
        )
    return found


def recorded_core_problem(env: object) -> str | None:
    """Why a recorded environment's core cannot back a package, or None: its commit is unknown, or it was dirty. An
    environment of the earlier form, or one without sources (an anonymised package's), records no dirt to read."""
    entry: Mapping[str, Any] | None = core_entry(env)
    if entry is None or is_earlier_form(env):
        return None
    commit = entry.get("commit")
    if not core_identity.is_sha(commit):
        return "the core's commit is unknown"
    if entry.get("dirty") is not False:
        return f"the core ({str(commit)[:12]}) had uncommitted changes"
    return None


__all__ = ["CoreVersionUnknown", "recorded_core_problem", "require_core"]
