"""The hold registry says why runs are not starting, once, when it changes; and the settings refuse what cannot work."""

import pytest
from campaign_support import NOW, make_schema, planned_store, treatment_schema

from expcore.campaign.holds import ALL, Hold, HoldRegistry
from expcore.campaign.settings import SchedulerConfig


def registry(tmp_path):
    store, _, _ = planned_store(tmp_path)
    return HoldRegistry(store, lambda: NOW), store


def lines(store):
    return [(r["kind"], r["model"], r["detail"]) for r in store.q("SELECT kind, model, detail FROM events ORDER BY id")]


def test_a_hold_is_written_once_when_it_begins_changes_or_ends(tmp_path):
    """A subject's hold is in the log when it begins, changes and ends; not at every pass."""
    holds, store = registry(tmp_path)
    over = Hold("budget", "over the ceiling")
    holds.note("m1", over)
    holds.note("m1", over)
    holds.note("m1", Hold("budget", "over a higher ceiling"))
    holds.note("m1", None)
    holds.note("m1", None)
    assert [(kind, model) for kind, model, _ in lines(store)] == [("hold", "m1"), ("hold", "m1"), ("hold_lifted", "m1")]
    assert holds.current() == {}


def test_the_subject_all_and_a_unit_are_not_models_in_the_log(tmp_path):
    """A hold on every run, or on a unit, carries no model name."""
    holds, store = registry(tmp_path)
    holds.note(ALL, Hold("health_gate", "closed", temporary=True))
    holds.note("unit:u1", Hold("unit_paused", "paused", temporary=True))
    assert [model for _, model, _ in lines(store)] == [None, None]


def test_a_hold_that_will_not_end_by_itself_is_told_apart_from_one_that_will(tmp_path):
    """`blocked` is true only for a hold that needs someone to look."""
    holds, _ = registry(tmp_path)
    holds.note("unit:u1", Hold("unit_paused", "paused", temporary=True))
    assert not holds.blocked()
    holds.note("m1", Hold("budget", "over"))
    assert holds.blocked()
    holds.note("m1", None)
    assert not holds.blocked()


@pytest.mark.parametrize(
    "changes",
    [{"max_attempts": 0}, {"workers": 0}, {"unit_caps": {"u": 0}}, {"unit_pause_s": 0}, {"retry_delay_s": -1}],
)
def test_settings_that_cannot_work_are_refused(changes):
    """Limits that cannot work are refused when the settings are built."""
    with pytest.raises(ValueError, match=r"at least 1|must be positive|must not be negative"):
        SchedulerConfig(**changes)


def test_the_health_and_identity_kinds_must_be_known_kinds_and_not_a_conditions_treatment():
    """A kind outside the vocabulary, or one a condition scores as treatment, is refused against the schema."""
    with pytest.raises(ValueError, match="not an infrastructure kind"):
        SchedulerConfig(health_kind="no_such_kind").check_kinds(make_schema())
    SchedulerConfig(health_kind="sandbox", identity_kind="endpoint").check_kinds(make_schema())
    with pytest.raises(ValueError, match="treatment of condition 'api-none'"):
        SchedulerConfig(identity_kind="endpoint").check_kinds(treatment_schema(("endpoint",), "api-none"))


def test_c01_the_scheduler_refuses_its_health_and_identity_kinds_as_treatment_by_role():
    """C01: the kinds a campaign charges its own failures to cannot be a condition's treatment, whichever kinds they
    are; the refusal names the role. Other kinds a condition declares are left to the registry."""
    with pytest.raises(ValueError, match="health_kind 'sandbox' is the treatment of condition 'api-none'"):
        SchedulerConfig(health_kind="sandbox").check_kinds(treatment_schema(("sandbox",), "api-none"))
    with pytest.raises(ValueError, match="identity_kind 'knowledge' is the treatment of condition 'api-none'"):
        SchedulerConfig(identity_kind="knowledge").check_kinds(treatment_schema(("knowledge",), "api-none"))
    SchedulerConfig().check_kinds(treatment_schema(("knowledge",), "api-none"))
    SchedulerConfig(health_kind="tool_plane_integrity").check_kinds(make_schema())
