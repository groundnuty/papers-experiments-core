# Provenance: new
"""A campaign an earlier core recorded keeps the core's entry in the form that core recorded (C06).

The environment now names the core by its commit, its git tree, whether it was dirty and a digest of its Python
source (`provenance.core_identity`). A core before that named it by how it was installed (`environment.source_of`):
a git URL's commit, an editable checkout's digest, or nothing about a wheel or a directory. A store it recorded holds
that form (`environment.is_earlier_form`), and comparing it with the current form would refuse every such store.

So the earlier form is accepted for that campaign's own continuation and nothing else: its resume, its check, its
analysis and its package compare it with the environment in use computed in the same earlier form, exactly as the
earlier core did, so nothing is weaker than it was, and the start says so on stderr. A new campaign always records the
current form, and the core in use is still held to its own identity (`core_check.require_core`). A new merge takes
no shard of the earlier form (`study.merge.refuse_earlier_form`, and `campaign.merge.check_core` for a mix of the
two): for a wheel or a directory install that form names no code, so two such shards may have run two cores and still
compare equal. A batch an earlier core merged is checked and analysed in the earlier form, like a store.
"""

import sys
from collections.abc import Callable, Mapping
from typing import Any

from expcore.provenance.environment import CORE_DISTRIBUTION, is_earlier_form

EARLIER_NOTE = "the store recorded the core in its earlier form, by how it was installed"


def earlier_core_hint(recorded: Mapping[str, Any] | None, differences: tuple[str, ...]) -> str:
    """What a refused resume of a store of the earlier form adds, when the core's entry is among what differs: the
    earlier form is compared as the earlier core compared it, so the core in use is not the one the store names."""
    if recorded is None or not is_earlier_form(recorded.get("environment")):
        return ""
    if not any(line.startswith(f"environment.sources.{CORE_DISTRIBUTION}") for line in differences):
        return ""
    return (
        f" ({EARLIER_NOTE}, and that form is compared as the core that recorded it compared it: the core in use is "
        "not the code the store names; finish the campaign with that core, or start a new store)"
    )


def earlier_core_fingerprint(
    recorded: Mapping[str, Any] | None, fingerprint: dict[str, Any], earlier: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """The fingerprint a start presents: the campaign's, or, for a store whose fingerprint holds the core in the earlier
    form, the campaign's with its environment in that form (`earlier`, computed only then)."""
    if recorded is None or not is_earlier_form(recorded.get("environment")):
        return fingerprint
    print(f"run: {EARLIER_NOTE}; it resumes under that form", file=sys.stderr)
    return {**fingerprint, "environment": earlier()}


__all__ = ["EARLIER_NOTE", "earlier_core_fingerprint", "earlier_core_hint"]
