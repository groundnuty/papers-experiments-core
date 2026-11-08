"""The closed infrastructure-kind vocabulary, its registry, and the two errors that are not infrastructure."""

import pytest

from expcore import errors
from expcore.errors import (
    CORE_KINDS,
    ContextOverflow,
    InfraError,
    RequestAbandoned,
    infra_kinds,
    register_infra_kind,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test starts with only the core's kinds and leaves no platform kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})


@pytest.mark.parametrize("kind", ["endpoint", "key_rejected", "sandbox", "knowledge", "unattributed_kill", "ceiling"])
def test_every_core_kind_is_accepted_and_keeps_its_own_name(kind):
    error = InfraError(kind, "detail")
    assert error.kind == kind
    assert error.detail == "detail"
    assert str(error) == f"{kind}: detail"


def test_the_core_vocabulary_is_exactly_the_six_kinds():
    assert set(CORE_KINDS) == {"endpoint", "key_rejected", "sandbox", "knowledge", "unattributed_kill", "ceiling"}
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
