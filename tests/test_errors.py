"""The closed infrastructure-kind vocabulary, its registry, and the two errors that are not infrastructure."""

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
    """Every test starts with only the core's kinds and leaves no platform kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})
    monkeypatch.setattr(errors, "_platform_untreatable", {})


CORE = {
    "endpoint",
    "key_rejected",
    "sandbox",
    "knowledge",
    "unattributed_kill",
    "ceiling",
    "tool_plane_integrity",
    "post_check_unverified",
}


@pytest.mark.parametrize("kind", sorted(CORE))
def test_every_core_kind_is_accepted_and_keeps_its_own_name(kind):
    error = InfraError(kind, "detail")
    assert error.kind == kind
    assert error.detail == "detail"
    assert str(error) == f"{kind}: detail"


def test_the_core_vocabulary_is_exactly_the_eight_kinds():
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


def test_o05_only_the_tool_plane_integrity_and_unverified_post_check_kinds_of_the_core_are_not_treatable():
    """O05: every core kind may be a condition's treatment except the one that reports a failed integrity check of the
    tool plane and the one of a post-run check that verified nothing (C11), which a run must never score."""
    assert {kind for kind in CORE_KINDS if not is_treatable(kind)} == {"tool_plane_integrity", "post_check_unverified"}
    assert set(untreatable_kinds()) == {"tool_plane_integrity", "post_check_unverified"}
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
    assert set(untreatable_kinds()) == {"tool_plane_integrity", "post_check_unverified", "provider_tampered"}
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


def test_o05_the_core_s_untreatable_kind_cannot_be_registered_again_as_treatable():
    with pytest.raises(ValueError, match="core's own kinds"):
        register_infra_kind("tool_plane_integrity", "described", treatable=True)
    assert is_treatable("endpoint") is True
