"""O82: a health check can be told which attempt it judges and in which phase (`CheckContext`).

The gate hands the context to a check that declares a parameter named `context`, by keyword, and calls every other check
with no argument, exactly as before. The dispatcher's own check, and a call that names no attempt, hand it None.
"""

import dataclasses
import functools
import threading
from unittest import mock

import pytest
from campaign_support import planned_store

from expcore.campaign.healthgate import (
    FOUND,
    GO,
    POST_RUN,
    PRE_RUN,
    CheckContext,
    Health,
    HealthGate,
    takes_context,
)
from expcore.config.loading import ConfigError


def gate_for(tmp_path, check, **options):
    store, _, _ = planned_store(tmp_path)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    gate = HealthGate(store, check, lambda: next(clock), **options)
    gate.load()
    return gate, store


def said(context):
    """What a check answers about the context it was given: its whole identity, as text."""
    return f"{context.phase} {context.run_key} {context.attempt} {context.attempt_id}"


class Meeting:
    """An owner-style check: it keeps each context it is given, waits until `parties` checks are inside it at once, and
    answers with the identity it was given."""

    def __init__(self, parties):
        self.barrier = threading.Barrier(parties, timeout=20)
        self.seen: list[CheckContext | None] = []

    def __call__(self, context):
        self.seen.append(context)  # list.append is atomic
        self.barrier.wait()
        return Health(True, said(context))


# --- the context itself ------------------------------------------------------------------------------------------


def test_o82_a_context_names_the_run_the_attempt_and_the_phase_and_cannot_change():
    """O82: a frozen record of the run key, the attempt's number, the phase and the store's id (None before the
    attempt's row exists), importable from the module where `HealthCheck` lives."""
    pre = CheckContext("run-a", 2, PRE_RUN)
    post = CheckContext("run-a", 2, POST_RUN, 41)
    assert (pre.run_key, pre.attempt, pre.phase, pre.attempt_id) == ("run-a", 2, "pre_run", None)
    assert (post.phase, post.attempt_id) == ("post_run", 41)
    assert pre != post
    with pytest.raises(dataclasses.FrozenInstanceError):
        pre.attempt = 3


@pytest.mark.parametrize(
    "fields",
    [
        ("run-a", 1, "during", None),
        ("run-a", 0, PRE_RUN, None),
        ("run-a", True, PRE_RUN, None),
        ("run-a", "1", PRE_RUN, None),
        ("", 1, PRE_RUN, None),
        (7, 1, PRE_RUN, None),
        ("run-a", 1, POST_RUN, 0),
        ("run-a", 1, POST_RUN, True),
        ("run-a", 1, PRE_RUN, 5),
        ("run-a", 1, POST_RUN, None),
    ],
)
def test_o82_a_context_that_names_nothing_real_is_refused(fields):
    """O82: a phase that is neither, a number or id that is not a positive whole number, a pre-run context with an
    attempt row it cannot have, or a post-run one without it, raises when it is made."""
    with pytest.raises(ValueError, match="CheckContext"):
        CheckContext(*fields)


# --- which checks receive it -------------------------------------------------------------------------------------


def plain(): ...


def named(context): ...


def defaulted(context=None): ...


def keyword_only(*, context): ...


def after_others(first=1, *, context, second=2): ...


class Method:
    def __call__(self, context): ...

    def other(self): ...


def binds_a_client(function):
    """A decorator whose wrapper takes no argument of its own (`*args`) and gives the function one it requires."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return function("client", *args, **kwargs)

    return wrapper


def binds_the_context(function):
    """A decorator whose wrapper takes no argument and gives the function the `context` it declares."""

    @functools.wraps(function)
    def wrapper():
        return function(context="bound")

    return wrapper


def forwards_the_context(function):
    """A decorator whose wrapper declares `context` itself and passes it on."""

    @functools.wraps(function)
    def wrapper(context):
        return function(context)

    return wrapper


@binds_a_client
def decorated_with_a_required_parameter(client):
    return Health(True, f"bound {client}")


@binds_the_context
def decorated_that_declares_context(context):
    return Health(True, f"bound {context}")


@forwards_the_context
def decorated_through_its_wrapper(context):
    return Health(True, "none" if context is None else said(context))


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        (decorated_with_a_required_parameter, False),  # the wrapper's own signature, not the function it wraps
        (decorated_that_declares_context, False),
        (decorated_through_its_wrapper, True),
        (lambda: None, False),
        (plain, False),
        (named, True),
        (defaulted, True),
        (keyword_only, True),
        (after_others, True),
        (Method(), True),
        (Method().other, False),
        (functools.partial(plain), False),
        (lambda timeout=5: None, False),  # another name, even one with a default: never handed anything
        (lambda *args, **kwargs: None, False),
        (mock.Mock(), False),
    ],
)
def test_o82_a_check_gets_the_context_when_it_declares_a_parameter_named_context(check, expected):
    """O82: the rule is one name, `context`; any other signature that can be called with no argument is called so."""
    assert takes_context(check) is expected


def test_o82_a_callable_whose_signature_cannot_be_read_is_called_as_before():
    """O82: a signature the interpreter cannot give leaves the check as it was: no argument."""
    with mock.patch("inspect.signature", side_effect=ValueError("no signature found")):
        assert takes_context(plain) is False


def test_o82_a_decorated_check_opts_in_through_the_wrappers_own_signature(tmp_path):
    """O82: the rule reads the callable's own signature, never the function a `functools.wraps` wrapper wraps: a
    wrapper that takes no argument and supplies a required one, or the `context` the function declares, is called as
    before and not refused or handed a keyword it cannot take; one that declares `context` receives it."""
    for index, check in enumerate((decorated_with_a_required_parameter, decorated_that_declares_context)):
        gate, _ = gate_for(tmp_path / str(index), check)
        verdict = gate.before_run("run-a", attempt=2)
        assert verdict.verdict == GO
        assert verdict.detail == ("bound client", "bound bound")[index]
    gate, _ = gate_for(tmp_path / "forwards", decorated_through_its_wrapper)
    assert gate.before_run("run-a", attempt=2).detail == "pre_run run-a 2 None"


def positional_only(context, /): ...


def starred(*context): ...


def double_starred(**context): ...


def other_name(ctx): ...


def with_a_second_required(context, other): ...


def with_a_required_keyword(*, context, other): ...


def required_keyword_other(*, other): ...


def required_after_default(first=1, *rest, required): ...


@pytest.mark.parametrize(
    ("check", "named_in_error"),
    [
        (positional_only, "context"),
        (starred, "context"),
        (double_starred, "context"),
        (other_name, "ctx"),
        (with_a_second_required, "other"),
        (with_a_required_keyword, "other"),
        (required_keyword_other, "other"),
        (required_after_default, "required"),
    ],
)
def test_o82_an_ambiguous_signature_is_refused_when_the_gate_is_built(tmp_path, check, named_in_error):
    """O82: a `context` that cannot be passed by keyword, or a required parameter the gate would never fill, raises a
    ConfigError that names the parameter at construction, before a campaign starts and not at the first call, where it
    used to read as a failed check for ever."""
    store, _, _ = planned_store(tmp_path)
    with pytest.raises(ConfigError, match=named_in_error) as refusal:
        HealthGate(store, check, lambda: "2026-01-01T00:00:00Z")
    assert "context" in str(refusal.value)
    with pytest.raises(ConfigError):
        takes_context(check)


# --- a check with no argument is called as before ----------------------------------------------------------------


def test_o82_a_check_with_no_argument_is_called_exactly_as_before(tmp_path):
    """O82: a zero-argument check, with or without an identity at the call, is called with nothing, before and after."""
    calls = []

    class Plain:
        def __call__(self):
            calls.append(((), {}))
            return Health(True, "plain")

    gate, _ = gate_for(tmp_path, Plain())
    assert gate.before_run("run-a").verdict == GO
    assert gate.before_run("run-a", attempt=2).verdict == GO
    assert gate.after_run()[0] == Health(True, "plain")
    assert gate.after_run("run-a", attempt=2, attempt_id=9)[0] == Health(True, "plain")
    assert calls == [((), {})] * 4


def test_o82_a_check_with_variadic_parameters_or_another_default_is_called_with_nothing(tmp_path):
    """O82: `*args, **kwargs` and a parameter of another name with a default receive no context: only the name
    `context` opts in, so no check that works today changes what it is handed."""
    seen = []

    def variadic(*args, **kwargs):
        seen.append(("variadic", args, kwargs))
        return Health(True, "ok")

    def timeout_default(timeout=30.0):
        seen.append(("timeout", timeout))
        return Health(True, "ok")

    for check in (variadic, timeout_default):
        gate, _ = gate_for(tmp_path / check.__name__, check)
        assert gate.before_run("run-a", attempt=1).verdict == GO
        assert gate.after_run("run-a", attempt=1, attempt_id=3)[0].ok
    assert seen == [("variadic", (), {})] * 2 + [("timeout", 30.0)] * 2


# --- the context a check receives --------------------------------------------------------------------------------


def test_o82_a_pre_run_and_a_post_run_check_that_meet_each_get_their_own_attempts_identity(tmp_path):
    """O82: attempt N+1's pre-run check and attempt N's post-run check of one run, inside the one shared check at the
    same moment (a barrier holds both there), are each given their own run, attempt, id and phase, and each caller
    gets the answer built from its own context."""
    check = Meeting(parties=2)
    gate, _ = gate_for(tmp_path, check, timeout_s=30)
    results: dict[str, object] = {}

    def pre_run():
        results["pre"] = gate.before_run("run-a", attempt=2)

    def post_run():
        results["post"] = gate.after_run("run-a", attempt=1, attempt_id=41)

    threads = [threading.Thread(target=pre_run), threading.Thread(target=post_run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert results["pre"].verdict == GO
    assert results["pre"].detail == "pre_run run-a 2 None"
    health, outage = results["post"]
    assert health == Health(True, "post_run run-a 1 41")
    assert outage is None
    assert len(check.seen) == 2
    assert set(check.seen) == {CheckContext("run-a", 2, PRE_RUN), CheckContext("run-a", 1, POST_RUN, 41)}


def test_o82_many_checks_at_once_never_see_one_anothers_identity(tmp_path):
    """O82: the context is passed down the call, never kept on the gate: with sixteen workers' checks inside the one
    check together, every caller is answered from its own run, attempt and phase."""
    pairs = 8
    check = Meeting(parties=2 * pairs)
    gate, _ = gate_for(tmp_path, check, timeout_s=60)
    answers: dict[tuple[str, int], str] = {}

    def pre(number):
        answers["pre", number] = gate.before_run(f"run-{number}", attempt=number + 1).detail

    def post(number):
        answers["post", number] = gate.after_run(f"run-{number}", attempt=number, attempt_id=100 + number)[0].detail

    threads = [threading.Thread(target=pre, args=(n,)) for n in range(1, pairs + 1)]
    threads += [threading.Thread(target=post, args=(n,)) for n in range(1, pairs + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    for n in range(1, pairs + 1):
        assert answers["pre", n] == f"pre_run run-{n} {n + 1} None"
        assert answers["post", n] == f"post_run run-{n} {n} {100 + n}"


def test_o82_a_check_that_names_no_attempt_is_given_none(tmp_path):
    """O82: the dispatcher's own check (the gate re-checking while closed) and a caller that gives no identity judge no
    attempt: a check that takes the context receives None, and a pre-run check that does name one receives it."""
    seen = []
    calls = iter(range(1, 100))

    def owner_style(context):
        seen.append(context)
        return Health(next(calls) > 2, "down")  # the first two answers fail, then it passes

    gate, _ = gate_for(tmp_path, owner_style)
    assert gate.before_run("run-a", attempt=1).verdict == FOUND  # closes the gate: charged to this run
    assert gate.poll() is False  # the dispatcher's own check
    assert gate.poll() is True
    assert seen == [CheckContext("run-a", 1, PRE_RUN), None, None]
    seen.clear()
    assert gate.before_run("run-a").verdict == GO  # no identity given, gate open again
    assert gate.after_run()[0].ok
    assert seen == [None, None]


@pytest.mark.parametrize(
    "given",
    [
        {"run_key": "run-a"},
        {"attempt": 1},
        {"attempt_id": 4},
        {"run_key": "run-a", "attempt": 1},
        {"attempt": 1, "attempt_id": 4},
    ],
)
def test_o82_a_post_run_identity_is_given_whole_or_not_at_all(tmp_path, given):
    """O82: a run key, an attempt and an id given in part could name another attempt's check as this one's: refused
    before any check runs."""
    seen = []
    gate, _ = gate_for(tmp_path, lambda context: seen.append(context) or Health(True, "ok"))
    with pytest.raises(ValueError, match="together"):
        gate.after_run(**given)
    assert seen == []


def test_o82_a_check_that_takes_the_context_and_raises_or_answers_nonsense_is_a_failed_check(tmp_path):
    """O82: the context changes nothing of how an answer is read: a raise or a non-answer is unhealthy, never a pass."""

    def raising(context):
        raise OSError("cannot reach")

    gate, _ = gate_for(tmp_path / "raises", raising)
    verdict = gate.before_run("run-a", attempt=1)
    assert verdict.verdict == FOUND
    assert "raised OSError" in verdict.detail
    gate, _ = gate_for(tmp_path / "nonsense", lambda context: "healthy")
    health, _ = gate.after_run("run-a", attempt=1, attempt_id=2)
    assert health.ok is False
