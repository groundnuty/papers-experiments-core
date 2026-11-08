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
        ({"ceiling_s": 100.0, "ceiling_kind": "sandbox"}, "not below time_cap_s"),
        ({"ceiling_s": float("inf")}, "finite"),
        ({"ceiling_s": float("nan")}, "finite"),
        ({"ceiling_s": 400.0, "ceiling_kind": "nowhere"}, "ceiling_kind"),
        ({"ceiling_factor": 0.5}, "ceiling_factor"),
        ({"ceiling_factor": float("inf")}, "ceiling_factor"),
        ({"ceiling_s": 400.0, "ceiling_factor": 2.0}, "disagree"),
        ({"min_call_s": -1.0}, "must not be negative"),
    ],
)
def test_a_configuration_that_cannot_work_is_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        AgentConfig(LIMITS, **kwargs)


def test_l04_a_configuration_has_a_ceiling_of_three_times_the_time_cap_unless_it_says_otherwise():
    """L04: the whole-time ceiling is always set, and the record holds the value that was in force."""
    default = AgentConfig(LIMITS)
    assert (default.ceiling_s, default.ceiling_factor, default.ceiling_kind) == (900.0, 3.0, "ceiling")
    assert default.as_dict()["ceiling_s"] == 900.0
    assert default.timer().ceiling_s == 900.0
    assert AgentConfig(LIMITS, ceiling_factor=2.0).ceiling_s == 600.0
    explicit = AgentConfig(LIMITS, ceiling_s=450.0, ceiling_kind="sandbox")
    assert (explicit.ceiling_s, explicit.ceiling_factor, explicit.ceiling_kind) == (450.0, None, "sandbox")
    assert (
        AgentConfig(LIMITS, ceiling_s=LIMITS.time_cap_s).ceiling_s == LIMITS.time_cap_s
    )  # equal to the cap is allowed


def test_l04_a_settled_configuration_can_be_copied_and_the_ceiling_follows_a_changed_cap_only_by_being_given():
    """L04: a copy keeps the ceiling it holds; one that changes the time cap must say what the ceiling becomes."""
    base = AgentConfig(LIMITS)
    assert dataclasses.replace(base, malformed_policy="end").ceiling_s == 900.0
    longer = dataclasses.replace(LIMITS, time_cap_s=400.0)
    with pytest.raises(ValueError, match="disagree"):
        dataclasses.replace(base, limits=longer)
    assert dataclasses.replace(base, limits=longer, ceiling_s=None).ceiling_s == 1200.0


def test_l04_the_ceiling_is_part_of_the_configuration_hash():
    base = AgentConfig(LIMITS).config_sha256()
    assert AgentConfig(LIMITS, ceiling_factor=4.0).config_sha256() != base
    assert AgentConfig(LIMITS, ceiling_s=900.0).config_sha256() != base  # no factor recorded: another record
    assert AgentConfig(LIMITS, ceiling_kind="sandbox").config_sha256() != base


@pytest.mark.parametrize("change", [{"time_cap_s": 0.0}, {"time_cap_s": float("inf")}, {"output_cap_chars": 0}])
def test_caps_that_cannot_work_are_refused(change):
    with pytest.raises(ValueError, match="agent configuration"):
        AgentConfig(dataclasses.replace(LIMITS, **change))
