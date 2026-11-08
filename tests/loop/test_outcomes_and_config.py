"""The closed outcome vocabulary, validated at construction; the run configuration and its hash."""

import dataclasses

import pytest

from expcore.config.models import Limits
from expcore.errors import CORE_KINDS
from expcore.loop.config import AgentConfig
from expcore.loop.outcomes import CAP_OUTCOMES, OUTCOMES, HarnessDefect, Outcome

LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=8000, max_output_tokens=4096
)


def test_the_vocabulary_is_the_nine_endings():
    assert OUTCOMES == (
        "finished",
        "turn_cap",
        "time_cap",
        "token_cap",
        "output_cap",
        "malformed_end",
        "harness_defect",
        "error",
        "infrastructure",
    )
    assert set(CAP_OUTCOMES) == {"turn_cap", "time_cap", "token_cap", "output_cap"}


@pytest.mark.parametrize("name", ["finnished", "capped", "failed", "", "Finished"])
def test_an_outcome_outside_the_vocabulary_is_refused_at_construction(name):
    with pytest.raises(ValueError, match="unknown outcome"):
        Outcome(name)


@pytest.mark.parametrize("name", [n for n in OUTCOMES if n not in ("infrastructure", "error", "harness_defect")])
def test_a_plain_outcome_builds_and_is_scored_unless_it_is_set_aside(name):
    outcome = Outcome(name, "some detail")
    assert outcome.set_aside is False
    assert outcome.is_cap is (name in CAP_OUTCOMES)


@pytest.mark.parametrize("kind", CORE_KINDS)
def test_infrastructure_carries_a_kind_from_the_closed_vocabulary(kind):
    outcome = Outcome("infrastructure", infra_kind=kind)
    assert outcome.infra_kind == kind
    assert outcome.set_aside is True


def test_infrastructure_needs_a_known_kind_and_no_other_outcome_takes_one():
    with pytest.raises(ValueError, match="infra_kind"):
        Outcome("infrastructure")
    with pytest.raises(ValueError, match="unknown infrastructure kind 'sandbx'"):
        Outcome("infrastructure", infra_kind="sandbx")
    with pytest.raises(ValueError, match="infra_kind"):
        Outcome("time_cap", infra_kind="endpoint")


def test_an_error_carries_its_type_and_a_defect_carries_its_hash():
    assert Outcome("error", error_type="ContextOverflow").error_type == "ContextOverflow"
    with pytest.raises(ValueError, match="error_type"):
        Outcome("error")
    defect = HarnessDefect("KeyError", "0" * 64)
    assert Outcome("harness_defect", defect=defect).set_aside is True
    with pytest.raises(ValueError, match="defect"):
        Outcome("harness_defect")
    with pytest.raises(ValueError, match="error_type"):
        Outcome("finished", error_type="X")


def test_a_long_detail_is_shortened():
    assert len(Outcome("finished", "x" * 5000).detail) == 500


def test_the_configuration_hash_covers_every_value():
    base = AgentConfig(LIMITS)
    assert base.config_sha256() == AgentConfig(LIMITS).config_sha256()
    for change in (
        {"output_cap_chars": 4000},
        {"max_tool_rounds": 9},
        {"time_cap_s": 301.0},
        {"token_budget": 1},
        {"max_output_tokens": 1},
    ):
        other = AgentConfig(dataclasses.replace(LIMITS, **change))
        assert other.config_sha256() != base.config_sha256(), change
    assert AgentConfig(LIMITS, malformed_policy="end").config_sha256() != base.config_sha256()
    assert AgentConfig(LIMITS, min_call_s=2.0).config_sha256() != base.config_sha256()


def test_the_configuration_as_the_record_keeps_it_holds_the_values_not_only_a_hash():
    values = AgentConfig(LIMITS, malformed_policy="end").as_dict()
    assert values["limits"]["output_cap_chars"] == 8000
    assert values["malformed_policy"] == "end"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"malformed_policy": "run_empty"}, "malformed_policy"),
        ({"ceiling_s": 400.0}, "go together"),
        ({"ceiling_kind": "sandbox"}, "go together"),
        ({"ceiling_s": 100.0, "ceiling_kind": "sandbox"}, "not below time_cap_s"),
        ({"ceiling_s": 400.0, "ceiling_kind": "nowhere"}, "ceiling_kind"),
        ({"min_call_s": -1.0}, "must not be negative"),
    ],
)
def test_a_configuration_that_cannot_work_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        AgentConfig(LIMITS, **kwargs)


@pytest.mark.parametrize("change", [{"time_cap_s": 0.0}, {"time_cap_s": float("inf")}, {"output_cap_chars": 0}])
def test_caps_that_cannot_work_are_refused(change):
    with pytest.raises(ValueError, match="agent configuration"):
        AgentConfig(dataclasses.replace(LIMITS, **change))
