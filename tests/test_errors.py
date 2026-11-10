"""The closed infrastructure-kind vocabulary, its registry, and the two errors that are not infrastructure."""

import os
import sys
import threading
import time
import warnings

import pytest

from expcore import errors
from expcore.errors import (
    CORE_KINDS,
    ContextOverflow,
    InfraError,
    RequestAbandoned,
    infra_kinds,
    is_treatable,
    register_infra_kind,
    untreatable_kinds,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test starts with only the core's kinds and leaves no platform or study kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})
    monkeypatch.setattr(errors, "_platform_untreatable", {})
    monkeypatch.setattr(errors, "_study_kinds", {})


CORE = {"endpoint", "key_rejected", "sandbox", "knowledge", "unattributed_kill", "ceiling", "tool_plane_integrity"}


@pytest.mark.parametrize("kind", sorted(CORE))
def test_every_core_kind_is_accepted_and_keeps_its_own_name(kind):
    error = InfraError(kind, "detail")
    assert error.kind == kind
    assert error.detail == "detail"
    assert str(error) == f"{kind}: detail"


def test_the_core_vocabulary_is_exactly_the_seven_kinds():
    assert set(CORE_KINDS) == CORE
    assert infra_kinds() == CORE_KINDS


def test_an_unknown_kind_is_refused_and_names_the_known_ones():
    with pytest.raises(ValueError, match="unknown infrastructure kind 'endpiont'") as caught:
        InfraError("endpiont", "typo")
    assert "key_rejected" in str(caught.value)


def test_a_platform_registers_its_own_kind_and_can_then_raise_it():
    with pytest.raises(ValueError, match="unknown infrastructure kind"):
        InfraError("provider_restart", "before registration")
    register_infra_kind("provider_restart", "a storage provider restarted under the run")
    assert InfraError("provider_restart", "after registration").kind == "provider_restart"
    assert infra_kinds() == (*CORE_KINDS, "provider_restart")


def test_registering_a_kind_twice_is_harmless():
    register_infra_kind("provider_restart", "first description")
    register_infra_kind("provider_restart", "second description")
    assert infra_kinds().count("provider_restart") == 1


@pytest.mark.parametrize("bad", ["", "Upper", "1leading", "has space", "dash-ed"])
def test_a_malformed_kind_name_cannot_be_registered(bad):
    with pytest.raises(ValueError, match="lower-case letters"):
        register_infra_kind(bad, "described")


def test_a_core_kind_cannot_be_registered_again():
    with pytest.raises(ValueError, match="core's own kinds"):
        register_infra_kind("sandbox", "described")


def test_a_kind_needs_a_description():
    with pytest.raises(ValueError, match="needs a description"):
        register_infra_kind("provider_restart", "  ")


def test_slowness_and_context_overflow_are_not_infrastructure():
    assert not issubclass(RequestAbandoned, InfraError)
    assert not issubclass(ContextOverflow, InfraError)


def test_context_overflow_keeps_the_status_and_pattern_but_no_provider_text():
    error = ContextOverflow(400, "prompt_too_long")
    assert (error.status, error.pattern) == (400, "prompt_too_long")
    assert str(error) == "context overflow (HTTP 400, prompt_too_long)"


def test_o05_only_the_tool_plane_integrity_kind_of_the_core_is_not_treatable():
    """O05: every core kind may be a condition's treatment except the one that reports a failed integrity check of the
    tool plane, which a run must never score as the manipulation."""
    assert {kind for kind in CORE_KINDS if not is_treatable(kind)} == {"tool_plane_integrity"}
    assert set(untreatable_kinds()) == {"tool_plane_integrity"}
    assert errors.TOOL_PLANE_INTEGRITY == "tool_plane_integrity"
    assert InfraError("tool_plane_integrity", "detail").kind == "tool_plane_integrity"


def test_o05_a_platform_can_register_a_kind_that_is_not_treatable():
    """O05: the property is the registry's, so a platform adds kinds on either side of it, and the answer for a kind
    outside the vocabulary is a refusal, not a guess."""
    with pytest.raises(ValueError, match="unknown infrastructure kind"):
        is_treatable("provider_down")
    register_infra_kind("provider_down", "a provider that is down", treatable=True)
    register_infra_kind("provider_tampered", "a provider whose state was changed", treatable=False)
    assert is_treatable("provider_down") is True
    assert is_treatable("provider_tampered") is False
    assert set(untreatable_kinds()) == {"tool_plane_integrity", "provider_tampered"}
    assert "state was changed" in untreatable_kinds()["provider_tampered"]


def test_o05_a_registered_kind_cannot_be_made_treatable_or_untreatable_by_registering_it_again():
    """O05: a second registration with the other answer is refused, so a platform cannot quietly weaken a kind; the
    same answer again is harmless."""
    register_infra_kind("provider_tampered", "first", treatable=False)
    register_infra_kind("provider_tampered", "again", treatable=False)
    with pytest.raises(ValueError, match="already registered as not treatable"):
        register_infra_kind("provider_tampered", "weaker", treatable=True)
    register_infra_kind("provider_down", "first")
    with pytest.raises(ValueError, match="already registered as treatable"):
        register_infra_kind("provider_down", "stronger", treatable=False)
    assert is_treatable("provider_tampered") is False
    assert is_treatable("provider_down") is True


def test_c02_a_study_s_own_kinds_hold_only_for_its_scope_however_the_scope_ends():
    """C02: a study's kinds join the vocabulary for the block that enters them, and the vocabulary is what it was
    afterwards, whether the block returned or raised; a platform cannot register a kind in scope."""
    kinds = {"relay_flap": errors.StudyKind("the relay restarted", treatable=False)}
    with errors.study_kinds(kinds):
        assert "relay_flap" in infra_kinds()
        assert is_treatable("relay_flap") is False
        assert untreatable_kinds()["relay_flap"] == "the relay restarted"
        with pytest.raises(ValueError, match=r"declared by the study in scope \(\[study.kinds\]\)"):
            register_infra_kind("relay_flap", "the relay restarted", treatable=False)
    assert infra_kinds() == CORE_KINDS
    with pytest.raises(RuntimeError, match="boom"), errors.study_kinds(kinds):
        raise RuntimeError("boom")
    assert infra_kinds() == CORE_KINDS
    register_infra_kind("relay_flap", "now a platform's, once no study holds the name")
    assert "relay_flap" in infra_kinds()


FLAP = errors.StudyKind("the relay restarted")
GONE = errors.StudyKind("the relay went away")
SHARED = errors.StudyKind("a fault two studies describe alike")


def in_a_plain_thread(action) -> list[BaseException]:
    """Run an action in a new thread, which inherits no context, and return what it raised."""
    raised: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as error:  # handed to the test, which asserts on it
            raised.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return raised


def test_c02_a_study_keeps_its_kinds_while_another_enters_and_leaves():
    """C02: a scope adds to the vocabulary and never replaces it: while another study is loaded the first study's kinds
    still validate, in a plain thread of its run as well, and after it left they are all that remain."""
    with errors.study_kinds({"relay_flap": FLAP}):
        with errors.study_kinds({"relay_gone": GONE}):
            assert {"relay_flap", "relay_gone"} <= set(infra_kinds())  # the overlap may see both: accepted
            assert (
                in_a_plain_thread(lambda: InfraError("relay_flap", "raised by a run while another study loads")) == []
            )
        assert "relay_flap" in infra_kinds()
        assert "relay_gone" not in infra_kinds()
    assert infra_kinds() == CORE_KINDS


@pytest.mark.parametrize("first_out", [0, 1], ids=["first-in-first-out", "last-in-first-out"])
def test_c02_overlapping_scopes_leave_nothing_behind_in_either_exit_order(first_out):
    """C02: a kind stays until the last scope that declares it has left, and no scope takes another's kind with it."""
    declared = [{"relay_flap": FLAP, "relay_shared": SHARED}, {"relay_gone": GONE, "relay_shared": SHARED}]
    scopes = [errors.study_kinds(kinds) for kinds in declared]
    for scope in scopes:
        scope.__enter__()
    assert {*declared[0], *declared[1]} <= set(infra_kinds())
    scopes[first_out].__exit__(None, None, None)
    staying, leaving = declared[1 - first_out], declared[first_out]
    assert set(staying) <= set(infra_kinds())
    assert not (set(leaving) - set(staying)) & set(infra_kinds())
    scopes[1 - first_out].__exit__(None, None, None)
    assert infra_kinds() == CORE_KINDS


@pytest.mark.parametrize(
    "other", [errors.StudyKind("another reading of the fault"), errors.StudyKind(FLAP.description, treatable=False)]
)
def test_c02_an_overlapping_scope_that_declares_a_name_differently_is_refused_naming_it(other):
    """C02: while two scopes overlap a name has one meaning, so the second is refused, and being refused it adds nothing
    and takes nothing from the scope already held."""
    with errors.study_kinds({"relay_flap": FLAP}):
        with (
            pytest.raises(ValueError, match="'relay_flap'"),
            errors.study_kinds({"relay_gone": GONE, "relay_flap": other}),
        ):
            pass
        assert "relay_gone" not in infra_kinds()
        assert is_treatable("relay_flap") is True
    assert infra_kinds() == CORE_KINDS


def test_c02_scopes_entered_and_left_concurrently_leave_the_registry_as_it_began(monkeypatch):
    """C02: threads that each hold their own kind and one they share, all at once, always see their own kinds and leave
    nothing behind."""
    workers, rounds = 8, 150
    barrier = threading.Barrier(workers)
    raised: list[BaseException] = []

    def work(number: int) -> None:
        own = f"relay_{number}"
        declared = {own: errors.StudyKind(f"relay number {number}"), "relay_shared": SHARED}
        try:
            barrier.wait()
            for _ in range(rounds):
                with errors.study_kinds(declared):
                    assert own in infra_kinds()
                    assert is_treatable("relay_shared") is True
                    InfraError(own, "raised while its study is in scope")
        except BaseException as error:  # handed to the test, which asserts on it
            raised.append(error)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # so the threads interleave inside the scopes
    try:
        threads = [threading.Thread(target=work, args=(number,)) for number in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert raised == []
    assert infra_kinds() == CORE_KINDS
    assert untreatable_kinds() == errors.CORE_UNTREATABLE


@pytest.mark.parametrize(
    ("kind", "description", "message"),
    [
        ("endpoint", "the endpoint failed", "core's own kinds"),
        ("provider_down", "a provider that is down", "a platform's kind; a study cannot declare it too"),
        ("Bad-Kind", "described", "must be lower-case"),
        ("relay_flap", "  ", "needs a description"),
    ],
)
def test_c02_a_study_cannot_declare_a_core_or_a_platform_kind_nor_a_malformed_one(kind, description, message):
    """C02: a study's kind is its own: the check is the same when the study is read and when its scope is entered, since
    a platform imported in between may have taken the name."""
    register_infra_kind("provider_down", "a provider that is down")
    with pytest.raises(ValueError, match=message):
        errors.check_study_kind(kind, description)
    with pytest.raises(ValueError, match=message), errors.study_kinds({kind: errors.StudyKind(description)}):
        pass


def test_o05_the_core_s_untreatable_kind_cannot_be_registered_again_as_treatable():
    with pytest.raises(ValueError, match="core's own kinds"):
        register_infra_kind("tool_plane_integrity", "described", treatable=True)
    assert is_treatable("endpoint") is True


@pytest.mark.skipif(not hasattr(os, "fork"), reason="the platform cannot fork")
def test_c02_a_fork_waits_for_a_change_to_the_kinds_under_way_and_the_child_starts_with_the_lock_free():
    """C02: a fork never copies a half-made registration or scope change into the child: it waits for the change under
    way to finish, and the child has a free lock of its own, so it cannot hang on one no thread will release."""
    held = threading.Event()

    def change_under_way() -> None:
        with errors._scope_lock:
            held.set()
            time.sleep(0.3)

    holder = threading.Thread(target=change_under_way)
    holder.start()
    held.wait(5)
    started = time.monotonic()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # forking a threaded process: the point of the test
        pid = os.fork()
    if pid == 0:  # the child: its lock must be free; os._exit keeps pytest out of the child
        os._exit(0 if errors._scope_lock.acquire(blocking=False) else 1)
    waited = time.monotonic() - started
    _, status = os.waitpid(pid, 0)
    holder.join(5)
    assert os.waitstatus_to_exitcode(status) == 0
    assert waited >= 0.25  # the fork waited for the change to finish


def test_c02_a_kind_becomes_known_only_once_whether_it_is_treatable_is_already_written(monkeypatch):
    """C02: readers take no lock, so a platform's kind must never be visible before its treatability: an untreatable
    kind seen half-registered would read as treatable and could be scored as a treatment."""
    seen: list[bool] = []

    class Watched(dict):
        def __setitem__(self, kind, description):
            seen.append(kind in errors._platform_untreatable)
            super().__setitem__(kind, description)

    monkeypatch.setattr(errors, "_platform_kinds", Watched(errors._platform_kinds))
    monkeypatch.setattr(errors, "_platform_untreatable", dict(errors._platform_untreatable))
    errors.register_infra_kind("ordering_probe_kind", "a kind registered by this test", treatable=False)
    assert seen == [True]
    assert errors.is_treatable("ordering_probe_kind") is False


def test_c02_a_reader_waits_for_a_change_to_the_vocabulary_under_way_and_reads_it_whole():
    """C02: the platforms' kinds, their treatability and the study kinds in scope are read together under the lock
    that changes them, so a reader never mixes two moments: it waits while a change is under way."""
    held, release = threading.Event(), threading.Event()
    answers: list[tuple[str, ...]] = []

    def change_under_way() -> None:
        with errors._scope_lock:
            held.set()
            release.wait(5)

    writer = threading.Thread(target=change_under_way)
    writer.start()
    held.wait(5)
    reader = threading.Thread(target=lambda: answers.append(errors.infra_kinds()))
    reader.start()
    reader.join(0.2)
    waiting = reader.is_alive()
    release.set()
    reader.join(5)
    writer.join(5)
    assert waiting  # the reader did not read while the change was under way
    assert answers
    assert set(errors.CORE_KINDS) <= set(answers[0])


def test_c02_the_vocabulary_and_its_untreatable_kinds_come_from_one_view(monkeypatch):
    """C02: a caller that needs both the kinds and their treatability gets them from one moment, so a study's
    untreatable kind cannot be known in one read and missing from the other."""
    views = []
    real = errors._view
    monkeypatch.setattr(errors, "_view", lambda: views.append(1) or real())
    known, untreatable = errors.vocabulary()
    assert len(views) == 1
    assert set(untreatable) <= set(known)
