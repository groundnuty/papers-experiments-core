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
`register_infra_kind`, for the whole process, and while a study is loaded or run, what it declares in its study.toml
(`[study.kinds]`, held by `study_kinds`). A study lists the platforms whose kinds it names (`[study] platforms`), and
the study loader imports them before it validates the conditions. An unknown kind is a programming error and raises at
once, so a typo never reaches a record.

Each kind is treatable or not. A condition whose manipulation is a fault may declare the kind of that fault as its
treatment, so an attempt that ends in it is scored instead of set aside (`FactorSchema`). A kind that reports a
failure of the harness's own integrity checks must never be scored that way, because a run that failed them says
nothing about the manipulation: `tool_plane_integrity` is not treatable, and a platform registers further kinds
that are not with `register_infra_kind(kind, description, treatable=False)`. The factor schema refuses to load a
condition that declares one, and the scheduler refuses a campaign whose health or identity kind a condition declares.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping
from typing import NamedTuple

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
_platform_kinds: dict[str, str] = {}  # process-wide: a platform registers its kinds once, when its module is imported
_platform_untreatable: dict[str, str] = {}


class StudyKind(NamedTuple):
    """An infrastructure kind a study declares in its study.toml (`[study.kinds]`)."""

    description: str
    treatable: bool = True


_study_kinds: Mapping[str, StudyKind] = {}  # the kinds of the study in scope (`study_kinds`), for as long as it is


def _check_name(kind: str, description: str) -> None:
    """Refuse a kind no one can add: a malformed name, a core kind or a blank description."""
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"infrastructure kind {kind!r} must be lower-case letters, digits and underscores")
    if kind in CORE_KINDS:
        raise ValueError(f"infrastructure kind {kind!r} is one of the core's own kinds")
    if not description.strip():
        raise ValueError(f"infrastructure kind {kind!r} needs a description")


def check_study_kind(kind: str, description: str) -> None:
    """Refuse a kind a study cannot declare: one no one can add, or one a platform has registered."""
    _check_name(kind, description)
    if kind in _platform_kinds:
        raise ValueError(f"infrastructure kind {kind!r} is a platform's kind; a study cannot declare it too")


@contextlib.contextmanager
def study_kinds(declared: Mapping[str, StudyKind]) -> Iterator[None]:
    """Hold a study's own kinds in the vocabulary for the block, and restore the vocabulary however the block ends.

    A study's kinds are its own, so they last only while the study is loaded or run: the study loader and each step of
    the pipeline enter this scope. Worker threads see it, since a run's attempts need it. One study is in scope at a
    time: a nested scope replaces the kinds of the one around it, which come back when it ends.
    """
    global _study_kinds
    for kind, entry in declared.items():
        check_study_kind(
            kind, entry.description
        )  # a platform imported since the study was read may have taken the name
    previous, _study_kinds = _study_kinds, dict(declared)
    try:
        yield
    finally:
        _study_kinds = previous


def register_infra_kind(kind: str, description: str, *, treatable: bool = True) -> None:
    """Add a platform's infrastructure kind to the vocabulary, for the whole process; doing so again is harmless.

    A platform calls it once, when its module is imported. A study that declares one of the kinds as a treatment lists
    that module in study.toml (`[study] platforms`), so it is imported before the conditions are validated.

    A kind registered with `treatable=False` can never be a condition's treatment: the factor schema refuses a
    declaration of it. Registering a kind again with the other answer is refused, so it cannot be made treatable.
    A kind of the study in scope (`study_kinds`) is refused: the two would be one kind with two owners.
    """
    _check_name(kind, description)
    if kind in _study_kinds:
        raise ValueError(f"infrastructure kind {kind!r} is declared by the study in scope ([study.kinds])")
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
    """Return every kind in the vocabulary now: the core's, the registered ones in order, then the study's in scope."""
    return (*CORE_KINDS, *_platform_kinds, *_study_kinds)


def untreatable_kinds() -> dict[str, str]:
    """Return every kind that cannot be a treatment now, with the reason each gives."""
    return {
        **CORE_UNTREATABLE,
        **{k: why for k, why in _platform_untreatable.items() if k in _platform_kinds},
        **{k: entry.description for k, entry in _study_kinds.items() if not entry.treatable},
    }


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
