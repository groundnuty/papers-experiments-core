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

The kinds form a closed vocabulary: the core's seven, plus whatever a platform registers once at import time with
`register_infra_kind` and whatever a study declares in its study.toml (`[study.kinds]`, through `register_study_kind`).
A study lists the platforms whose kinds it names (`[study] platforms`), and the study loader imports them and registers
the study's kinds before it validates the conditions. An unknown kind is a programming error and raises at once, so a
typo never reaches a record.

Each kind is treatable or not. A condition whose manipulation is a fault may declare the kind of that fault as its
treatment, so an attempt that ends in it is scored instead of set aside (`FactorSchema`). A kind that reports a
failure of the harness's own integrity checks must never be scored that way, because a run that failed them says
nothing about the manipulation: `tool_plane_integrity` is not treatable, and a platform registers further kinds
that are not with `register_infra_kind(kind, description, treatable=False)`. The factor schema refuses to load a
condition that declares one, and the scheduler refuses a campaign whose health or identity kind a condition declares.
"""

from __future__ import annotations

import re

# endpoint: the model endpoint failed after the retries. key_rejected: HTTP 401 or 403 from the model API.
# sandbox: the agent's sandbox failed. knowledge: a tool server failed. unattributed_kill: a process was killed
# and no cause was recorded. ceiling: a call was still running when the whole-time ceiling passed, with the agent's
# own time under its cap, so something other than the agent held it up (L04). tool_plane_integrity: the tool plane
# failed the checks that it is what the condition declared (its token, its principal's privileges); not treatable.
CORE_KINDS = (
    "endpoint",
    "key_rejected",
    "sandbox",
    "knowledge",
    "unattributed_kill",
    "ceiling",
    "tool_plane_integrity",
)
TOOL_PLANE_INTEGRITY = "tool_plane_integrity"
# The core's kinds that no condition may declare as its treatment, and why.
CORE_UNTREATABLE: dict[str, str] = {
    TOOL_PLANE_INTEGRITY: "a run whose tool plane failed its integrity checks says nothing about the manipulation",
}

_KIND_RE = re.compile(r"[a-z][a-z0-9_]*")
_platform_kinds: dict[str, str] = {}  # every registered kind, a platform's or a study's, with its description
_platform_untreatable: dict[str, str] = {}
_study_kinds: dict[str, tuple[str, bool]] = {}  # the registered kinds a study declared, with what it declared


def register_infra_kind(kind: str, description: str, *, treatable: bool = True) -> None:
    """Add a platform's infrastructure kind to the vocabulary; registering the same kind again is harmless.

    A platform calls it once, when its module is imported. A study that declares one of the kinds as a treatment lists
    that module in study.toml (`[study] platforms`), so it is imported before the conditions are validated.

    A kind registered with `treatable=False` can never be a condition's treatment: the factor schema refuses a
    declaration of it. Registering a kind again with the other answer is refused, so it cannot be made treatable.
    A kind a study declared (`register_study_kind`) is refused: the two would be one kind with two owners.
    """
    if kind in _platform_kinds and kind in _study_kinds:
        raise ValueError(f"infrastructure kind {kind!r} is declared by a study ([study.kinds]), not a platform's")
    _study_kinds.pop(kind, None)  # a study's entry for a kind no longer registered says nothing
    _register(kind, description, treatable)


def register_study_kind(kind: str, description: str, *, treatable: bool = True) -> None:
    """Add a kind a study declares in study.toml (`[study.kinds]`); the study loader calls it before validating.

    A core kind or a platform's is refused: a study's kind is its own. Declaring it again with the same description
    and answer is harmless, so a study loads again in the same process; another description or answer is refused.
    """
    if kind in _platform_kinds:
        held = _study_kinds.get(kind)
        if held is None:
            raise ValueError(f"infrastructure kind {kind!r} is a platform's kind; a study cannot declare it too")
        if held != (description, treatable):
            raise ValueError(f"infrastructure kind {kind!r} is already declared by a study with another description "
                             "or treatability")  # fmt: skip
        return
    _register(kind, description, treatable)
    _study_kinds[kind] = (description, treatable)


def _register(kind: str, description: str, treatable: bool) -> None:
    """Check a kind and add it to the vocabulary, whoever declares it."""
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"infrastructure kind {kind!r} must be lower-case letters, digits and underscores")
    if kind in CORE_KINDS:
        raise ValueError(f"infrastructure kind {kind!r} is one of the core's own kinds")
    if not description.strip():
        raise ValueError(f"infrastructure kind {kind!r} needs a description")
    if kind in _platform_kinds and (kind in _platform_untreatable) == treatable:
        held = "not treatable" if kind in _platform_untreatable else "treatable"
        raise ValueError(f"infrastructure kind {kind!r} is already registered as {held}")
    if kind not in _platform_kinds:
        _platform_kinds[kind] = description
        if treatable:
            _platform_untreatable.pop(kind, None)
        else:
            _platform_untreatable[kind] = description


def infra_kinds() -> tuple[str, ...]:
    """Return every kind in the vocabulary now: the core's kinds, then the registered ones in registration order."""
    return (*CORE_KINDS, *_platform_kinds)


def untreatable_kinds() -> dict[str, str]:
    """Return every kind that cannot be a treatment now, with the reason each gives: the core's, then the registered."""
    return {**CORE_UNTREATABLE, **{k: why for k, why in _platform_untreatable.items() if k in _platform_kinds}}


def is_treatable(kind: str) -> bool:
    """Tell whether a condition may declare the kind as its treatment; a kind outside the vocabulary is refused."""
    if kind not in infra_kinds():
        raise ValueError(f"unknown infrastructure kind {kind!r}; known kinds: {', '.join(infra_kinds())}")
    return kind not in untreatable_kinds()


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
