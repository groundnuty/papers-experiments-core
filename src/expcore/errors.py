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

The kinds form a closed vocabulary: the core's eight, plus whatever a platform registers once at import time with
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

`budget_hold` is no failure of anything outside the agent either, but the campaign's own decision: a request the token
cap cannot account for would have taken the campaign past its budget, so it was not sent (`BudgetHold`, C26). It is
set aside like infrastructure, never treatable, and costs the run no attempt. A platform registers a kind of the same
sort, a **hold**, with `register_infra_kind(kind, description, treatable=False, charged=False)`: a start the platform
held back because what the run needs is held by another live attempt, or was left unfit by an earlier one, with
nothing sent. Such a kind is never treatable, and an attempt that ends in it costs its run no attempt
(`uncharged_kinds`, read by `expcore.campaign.attempts.classify`).

A failure may name its target, what failed, by a position label from the platform's configuration (`providers[1]`,
`TARGET_LABEL`). A condition whose manipulation takes one target down declares the kind with that target, and only a
failure of that target, while the run's fault injector held it down, is its treatment (`Outcome.is_treatment`). A kind
that is a fault on one target is registered with `requires_target=True` (`targeted_kinds`): a condition cannot declare
it without the target, and it is never scored as a treatment without one (C34).
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from collections.abc import Iterator, Mapping
from typing import NamedTuple

# endpoint: the model endpoint failed after the retries. key_rejected: HTTP 401 or 403 from the model API.
# sandbox: the agent's sandbox failed. knowledge: a tool server failed. unattributed_kill: a process was killed
# and no cause was recorded. ceiling: a call was still running when the whole-time ceiling passed, with the agent's
# own time under its cap, so something other than the agent held it up (L04). tool_plane_integrity: the tool plane
# failed the checks that it is what the condition declared (its token, its principal's privileges); not treatable.
# budget_hold: a request was not sent because its top-up would have passed the campaign's budget (C26); not treatable.
# post_check_unverified: the post-run health check was deferred, so the result was never verified (C11); not
# treatable.
CORE_KINDS = (
    "endpoint",
    "key_rejected",
    "sandbox",
    "knowledge",
    "unattributed_kill",
    "ceiling",
    "tool_plane_integrity",
    "budget_hold",
    "post_check_unverified",
)
TOOL_PLANE_INTEGRITY = "tool_plane_integrity"
BUDGET_HOLD = "budget_hold"
POST_CHECK_UNVERIFIED = "post_check_unverified"
# The fault injector's outage: a fault on one target, held to its declared target by the core whoever registers the kind.
# The name is the core's, as the injector's reading is (`expcore.records.injections`), so a study that declares a kind of
# this name of its own (`[study.kinds]`) in a process that never imported a platform is held to it too.
PROVIDER_OUTAGE = "provider_outage"
TARGETED_NAMES = frozenset({PROVIDER_OUTAGE})
# The core's kinds that no condition may declare as its treatment, and why.
CORE_UNTREATABLE: dict[str, str] = {
    TOOL_PLANE_INTEGRITY: "a run whose tool plane failed its integrity checks says nothing about the manipulation",
    BUDGET_HOLD: "a run the budget stopped says nothing about the manipulation, only about what the campaign could pay",
    POST_CHECK_UNVERIFIED: "a run whose post-run check verified nothing has a result nobody checked",
}

_KIND_RE = re.compile(r"[a-z][a-z0-9_]*")
# What an infrastructure failure may name as its target: a table and a position in it, as the platform's
# configuration orders its entries (`providers[1]`). A configured name never fits, so none can reach a record.
TARGET_LABEL = re.compile(r"[a-z][a-z0-9_]*\[(0|[1-9][0-9]*)\]")
_platform_kinds: dict[str, str] = {}  # process-wide: a platform registers its kinds once, when its module is imported
_platform_untreatable: dict[str, str] = {}
_platform_uncharged: dict[str, str] = {}  # a platform's holds: kinds that cost a run no attempt (`charged=False`)
_platform_targeted: dict[str, str] = {}  # a platform's faults on one target: kinds that need one (`requires_target`)


class StudyKind(NamedTuple):
    """An infrastructure kind a study declares in its study.toml (`[study.kinds]`)."""

    description: str
    treatable: bool = True


class _HeldKind(NamedTuple):
    """A study's kind in scope, and how many scopes hold it."""

    kind: StudyKind
    scopes: int


_scope_lock = threading.Lock()  # serialises every change to the study kinds in scope, and a platform's registration


def _hold_lock_for_fork() -> None:
    """Take the lock before a fork, so the child never copies a registration or a scope change half done. Nothing in
    this module forks while it holds the lock, so the forking thread only waits for a change under way to finish."""
    _scope_lock.acquire()


def _release_lock_in_parent() -> None:
    """Give the lock back in the parent once the fork is done."""
    _scope_lock.release()


def _fresh_lock_in_child() -> None:
    """A forked child starts with a free lock of its own rather than the copy its forking thread holds. The kinds in
    scope are kept, whole; the child's own scopes count from them."""
    global _scope_lock
    _scope_lock = threading.Lock()


if hasattr(os, "register_at_fork"):  # every platform the core supports; absent only where fork itself is
    os.register_at_fork(
        before=_hold_lock_for_fork, after_in_parent=_release_lock_in_parent, after_in_child=_fresh_lock_in_child
    )
_study_kinds: Mapping[str, _HeldKind] = {}  # the kinds in scope (`study_kinds`); replaced under the lock, never
# changed in place; readers take every mapping together under the lock (`_view`)


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
    """Hold a study's own kinds in the vocabulary for the block, however the block ends.

    A study's kinds are its own, so they last only while the study is loaded or run: the study loader and each step of
    the pipeline enter this scope. Scopes may overlap, as when one thread runs a study while another loads one: entering
    adds each kind with a count and leaving takes one off, so no scope can drop a kind another still holds, and nothing
    remains once all have left. Overlapping scopes that give one name different meanings are refused, naming the kind.

    While two studies overlap in one process each can see the other's kinds. That is accepted: it can only widen
    validation for that overlap and never rejects a valid record. The vocabulary is held for the process and not in a
    `ContextVar`, because a plain thread would not inherit one and a valid `InfraError` raised in it would be rejected.

    The contract that keeps readers consistent: a kind leaves only when no scope holds it, and every step of the
    pipeline validates a study's records inside that study's scope, so its kinds cannot leave mid-check; a platform's
    kinds never leave. Each reader takes one view (`vocabulary` gives the kinds and their treatability together). A
    caller that reads twice outside any scope while another study's scope changes can at worst refuse, or name the
    kinds of a different moment in its message; it can never accept a kind it should refuse.
    """
    held = dict(declared)  # what was entered is what is left, whatever the caller does with its mapping
    _enter_scope(held)
    try:
        yield
    finally:
        _leave_scope(held)


def _enter_scope(declared: Mapping[str, StudyKind]) -> None:
    """Count each kind in, or change nothing and raise ValueError when one cannot be held."""
    global _study_kinds
    with _scope_lock:
        held = dict(_study_kinds)
        for kind, entry in declared.items():
            check_study_kind(kind, entry.description)  # a platform imported since the study was read may have taken it
            current = held.get(kind)
            if current is not None and current.kind != entry:
                raise ValueError(f"infrastructure kind {kind!r} is declared differently by another study in scope")
            held[kind] = _HeldKind(entry, 1 if current is None else current.scopes + 1)
        _study_kinds = held


def _leave_scope(declared: Mapping[str, StudyKind]) -> None:
    """Count each kind out, removing the ones no scope holds any more."""
    global _study_kinds
    with _scope_lock:
        held = dict(_study_kinds)
        for kind in declared:
            current = held[kind]
            if current.scopes == 1:
                del held[kind]
            else:
                held[kind] = current._replace(scopes=current.scopes - 1)
        _study_kinds = held


def register_infra_kind(
    kind: str, description: str, *, treatable: bool = True, charged: bool = True, requires_target: bool = False
) -> None:
    """Add a platform's infrastructure kind to the vocabulary, for the whole process; doing so again is harmless.

    A platform calls it once, when its module is imported. A study that declares one of the kinds as a treatment lists
    that module in study.toml (`[study] platforms`), so it is imported before the conditions are validated.

    A kind registered with `treatable=False` can never be a condition's treatment: the factor schema refuses a
    declaration of it. Registering a kind again with the other answer is refused, so it cannot be made treatable.
    A kind registered with `charged=False` is a hold: an attempt that ends in it is set aside and costs its run no
    attempt, as `budget_hold` does (`uncharged_kinds`); it must be untreatable, since a held start is never a
    manipulation, and it cannot be made charged, or uncharged, by registering it again. A kind of the study in scope
    (`study_kinds`) is refused: the two would be one kind with two owners.

    A kind registered with `requires_target=True` is a fault on one target (a provider taken down), listed by
    `targeted_kinds`. Its failure names a target and a condition cannot declare it as its treatment without naming the
    target it manipulates: the plain name would score any target's failure as the manipulation, so the factor schema
    refuses it and `Outcome.is_treatment` never scores such a kind that has no declared target. It must be treatable,
    since a kind that is never a treatment has no declaration to name a target in, and it cannot be made targetless,
    or a plain kind targeted, by registering it again. The name `provider_outage` is held to a target whoever registers
    it, and is refused without `requires_target=True`.
    """
    _check_name(kind, description)
    if not charged and treatable:
        raise ValueError(f"infrastructure kind {kind!r} costs no attempt, so it must be registered treatable=False")
    if requires_target and not treatable:
        raise ValueError(f"infrastructure kind {kind!r} is never a treatment, so it names no target to require")
    if kind in TARGETED_NAMES and not requires_target:
        raise ValueError(
            f"infrastructure kind {kind!r} is a fault on one target, so it must be registered requires_target=True"
        )
    with _scope_lock:
        if kind in _study_kinds:
            raise ValueError(f"infrastructure kind {kind!r} is declared by the study in scope ([study.kinds])")
        if kind in _platform_kinds and (kind in _platform_untreatable) == treatable:
            held = "not treatable" if kind in _platform_untreatable else "treatable"
            raise ValueError(f"infrastructure kind {kind!r} is already registered as {held}")
        if kind in _platform_kinds and (kind in _platform_uncharged) == charged:
            held = "costing no attempt" if kind in _platform_uncharged else "charged"
            raise ValueError(f"infrastructure kind {kind!r} is already registered as {held}")
        if kind in _platform_kinds and (kind in _platform_targeted) != requires_target:
            held = "requiring a target" if kind in _platform_targeted else "not requiring a target"
            raise ValueError(f"infrastructure kind {kind!r} is already registered as {held}")
        if kind not in _platform_kinds:
            # whether it is treatable, whether it is charged and whether it requires a target are written first, so a
            # reader without the lock never sees the kind without them
            if treatable:
                _platform_untreatable.pop(kind, None)
            else:
                _platform_untreatable[kind] = description
            if charged:
                _platform_uncharged.pop(kind, None)
            else:
                _platform_uncharged[kind] = description
            if requires_target:
                _platform_targeted[kind] = description
            else:
                _platform_targeted.pop(kind, None)
            _platform_kinds[kind] = description


def uncharged_kinds() -> frozenset[str]:
    """Return the platforms' holds: the registered kinds an attempt may end in without costing its run an attempt
    (`register_infra_kind(..., charged=False)`). The core's own are `expcore.campaign.attempts.UNCHARGED_KINDS`."""
    with _scope_lock:
        return frozenset(k for k in _platform_uncharged if k in _platform_kinds)


class _View(NamedTuple):
    """The vocabulary at one moment: the platforms' kinds, those of them that are untreatable or require a target, and
    the study kinds in scope. Readers compute from one view, so they never mix two moments."""

    platform: Mapping[str, str]
    untreatable: Mapping[str, str]
    study: Mapping[str, _HeldKind]
    targeted: frozenset[str] = frozenset()

    def kinds(self) -> tuple[str, ...]:
        return (*CORE_KINDS, *self.platform, *self.study)

    def untreatable_kinds(self) -> dict[str, str]:
        return {
            **CORE_UNTREATABLE,
            **{k: why for k, why in self.untreatable.items() if k in self.platform},
            **{k: held.kind.description for k, held in self.study.items() if not held.kind.treatable},
        }


def _view() -> _View:
    """Take one view of the vocabulary under the lock: registration and scopes change it under the same lock."""
    with _scope_lock:
        targeted = frozenset(k for k in _platform_targeted if k in _platform_kinds) | TARGETED_NAMES
        return _View(dict(_platform_kinds), dict(_platform_untreatable), _study_kinds, targeted)


def infra_kinds() -> tuple[str, ...]:
    """Return every kind in the vocabulary now: the core's, the registered ones in order, then the study's in scope."""
    return _view().kinds()


def untreatable_kinds() -> dict[str, str]:
    """Return every kind that cannot be a treatment now, with the reason each gives."""
    return _view().untreatable_kinds()


def vocabulary() -> tuple[tuple[str, ...], dict[str, str]]:
    """Return every kind and the untreatable ones with their reasons, from one view, for a caller that needs both."""
    view = _view()
    return view.kinds(), view.untreatable_kinds()


class Vocabulary(NamedTuple):
    """The vocabulary a condition's treatment is checked against, from one view: every kind, the ones that can never be
    a treatment with their reasons, and the ones that need a declared target (`register_infra_kind(requires_target)`)."""

    kinds: tuple[str, ...]
    untreatable: dict[str, str]
    targeted: frozenset[str]


def treatment_vocabulary() -> Vocabulary:
    """Return what a condition's treatment declaration is checked against, all from one view: a scope leaving between
    two reads cannot let an untreatable kind through, and a kind cannot be known in one moment and targeted in another."""
    view = _view()
    return Vocabulary(view.kinds(), view.untreatable_kinds(), view.targeted)


def targeted_kinds() -> frozenset[str]:
    """Return the kinds that are a fault on one target: a condition declares them as its treatment only with the target
    it manipulates (`register_infra_kind(..., requires_target=True)`), and `Outcome.is_treatment` never scores one that
    has none. They are the platforms' registered with `requires_target=True` and the names the core holds to a target
    whoever registers them (`TARGETED_NAMES`, the provider outage), a study's own kind of such a name included."""
    return _view().targeted


def is_treatable(kind: str) -> bool:
    """Tell whether a condition may declare the kind as its treatment; a kind outside the vocabulary is refused. Both
    answers come from one view, so a kind cannot be known in one moment and its treatability read in another."""
    view = _view()
    known = view.kinds()
    if kind not in known:
        raise ValueError(f"unknown infrastructure kind {kind!r}; known kinds: {', '.join(known)}")
    return kind not in view.untreatable_kinds()


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


def check_target(target: object) -> str | None:
    """Return a target label when it is None or one of the form `<table>[<position>]`; raise ValueError otherwise.

    The refusal never echoes the rejected value, which may well be a configured name (O12).
    """
    if target is not None and (not isinstance(target, str) or not TARGET_LABEL.fullmatch(target)):
        raise ValueError("a target is a position label such as 'providers[1]', never a configured name")
    return target


class InfraError(RuntimeError):
    """An infrastructure failure of one of the registered kinds, with a short detail that never holds a secret.

    `target` names what failed when the platform can attribute the failure, as an opaque position label from its
    configuration (`providers[1]`), never as the name it is configured under. A condition that declares a kind with a
    target is scored only for a failure of that target while the run's fault injector held it down (`Outcome`).

    `billed` is the (input, output) counts of a reply the endpoint answered, and so billed, before the failure was found
    in it (a 200 without choices, say): the recording tells the attempt's spend guard of it (C26, C27). None when no
    reply came back.
    """

    def __init__(
        self,
        kind: str,
        detail: str,
        *,
        target: str | None = None,
        billed: tuple[int | None, int | None] | None = None,
    ):
        known = infra_kinds()  # one view, for the test and the message alike
        if kind not in known:
            raise ValueError(
                f"unknown infrastructure kind {kind!r}; known kinds: {', '.join(known)} "
                "(a platform adds its own with register_infra_kind)"
            )
        check_target(target)
        super().__init__(f"{kind}: {detail}" if target is None else f"{kind} ({target}): {detail}")
        self.kind = kind
        self.detail = detail
        self.target = target
        self.billed = billed


class CallTimedOut(InfraError):
    """A tool call outlasted its `timeout_s` without the tool server answering it (L16): infrastructure of kind
    `knowledge`.

    It is its own class so that a caller that bounds a call itself, as the day-0 outage check does, can tell the bound's
    expiry from a server that closed or failed, and so that the loop can tell it from a failure it has no other
    explanation for: inside the loop the bound is the agent's time left, so an expiry that arrives with the agent's
    time used up is the loop's own cut and is judged as one (`Watcher`), unless the call is to a tool server and the
    study declared a per-call bound for those (L17) that is the shorter, which is then the call's own failure. `tool`
    and `bound_s` are what the detail says.
    """

    def __init__(self, tool: str, bound_s: float):
        super().__init__("knowledge", f"the tool server did not answer a call to {tool!r} within {bound_s:g} s")
        self.tool = tool
        self.bound_s = bound_s


class BudgetHold(InfraError):
    """A request was not sent: the top-up it needed would have taken the campaign past its budget (C26).

    An infrastructure failure of kind `budget_hold`, so the attempt ends set aside and is never scored; the run is
    not charged an attempt for it (`expcore.campaign.attempts.UNCHARGED_KINDS`).
    """

    def __init__(self, detail: str):
        super().__init__(BUDGET_HOLD, detail)


class FirstTurnRefusal(InfraError):
    """An instrument refused the attempt at the agent's first turn (C31), before the loop sent its first request.

    An infrastructure failure of the kind the instrument refused with, which is never one a condition can declare as
    its treatment. Raised by `first_turn_instruments`; the scheduler reads it as the sign that the attempt sent
    nothing, so it costs no tokens, unless its backend says it sent a request after all.
    """
