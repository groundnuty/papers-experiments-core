# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/errors.py, blob 862e0cee71a5309cd43f205af2ffed6ba472b9f3
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Infrastructure failures, as opposed to anything the agent or the model did.

`InfraError` is raised when the model endpoint keeps failing after the transport retries (5xx, connection,
timeout), when its key is refused, or when the sandbox or a tool server crashes for reasons outside the agent's
code. The campaign decides whether to re-run the attempt.

Never an InfraError: agent-code exceptions (non-zero exit codes), turn, time or token caps, report outcomes, slowness,
and a context that outgrew the model's window (`ContextOverflow`).

The kinds form a closed vocabulary: the core's six, plus whatever a platform registers once at import time with
`register_infra_kind`. An unknown kind is a programming error and raises at once, so a typo never reaches a record.
"""

from __future__ import annotations

import re

# endpoint: the model endpoint failed after the retries. key_rejected: HTTP 401 or 403 from the model API.
# sandbox: the agent's sandbox failed. knowledge: a tool server failed. unattributed_kill: a process was killed
# and no cause was recorded. ceiling: a call was still running when the whole-time ceiling passed, with the agent's
# own time under its cap, so something other than the agent held it up (L04).
CORE_KINDS = ("endpoint", "key_rejected", "sandbox", "knowledge", "unattributed_kill", "ceiling")

_KIND_RE = re.compile(r"[a-z][a-z0-9_]*")
_platform_kinds: dict[str, str] = {}


def register_infra_kind(kind: str, description: str) -> None:
    """Add a platform's infrastructure kind to the vocabulary; registering the same kind again is harmless."""
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"infrastructure kind {kind!r} must be lower-case letters, digits and underscores")
    if kind in CORE_KINDS:
        raise ValueError(f"infrastructure kind {kind!r} is one of the core's own kinds")
    if not description.strip():
        raise ValueError(f"infrastructure kind {kind!r} needs a description")
    _platform_kinds.setdefault(kind, description)


def infra_kinds() -> tuple[str, ...]:
    """Return every kind in the vocabulary now: the core's kinds, then the registered ones in registration order."""
    return (*CORE_KINDS, *_platform_kinds)


class RequestAbandoned(Exception):
    """A request the endpoint accepted but did not answer before the client's read timeout.

    The read timeout is never shorter than the run's time cap, and a request that went out is never resent.
    The loop ends the run at its time cap. This is not an InfraError, because slowness is never infrastructure;
    the usage of the abandoned request is unknown and recorded as such.
    """


class ContextOverflow(Exception):
    """The conversation outgrew the model's context window (HTTP 400 or 413 naming the context).

    The agent's own outcome, like a turn cap: not retried, not infrastructure. Only the status and the matched
    pattern are kept, never the provider's text, which can echo request content.
    """

    def __init__(self, status: int | None, pattern: str):
        super().__init__(f"context overflow (HTTP {status if status is not None else '?'}, {pattern})")
        self.status = status
        self.pattern = pattern


class InfraError(RuntimeError):
    """An infrastructure failure of one of the registered kinds, with a short detail that never holds a secret."""

    def __init__(self, kind: str, detail: str):
        if kind not in infra_kinds():
            raise ValueError(
                f"unknown infrastructure kind {kind!r}; known kinds: {', '.join(infra_kinds())} "
                "(a platform adds its own with register_infra_kind)"
            )
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
