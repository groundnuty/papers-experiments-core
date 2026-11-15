"""O35: a platform's run admission may declare that it needs a health check, and a campaign without one is refused.

An admission that reads what a try's pre-run check noted (`expcore.campaign.prerun`) can vouch for a run only when the
campaign has a health check: with none, the try has no note and the run would be admitted on nothing. So the platform's
admission declares the need (`expcore.campaign.health_required`), and the scheduler refuses, when it is built and
before any lock or store is touched, a campaign that has the admission and no health check. The core knows no platform:
these tests use a toy admission and a toy check.
"""

import contextlib
import threading

import pytest
from scheduler_support import FakeExecutor, build, settings_for

from expcore.campaign.health_required import ATTRIBUTE, refuse_without_health_check, required_reason
from expcore.campaign.health_required import requiring_health_check as requiring
from expcore.campaign.healthgate import Health
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
WHY = "the toy platform's admission reads its pre-run check's note, and runs without one would be admitted on nothing"


@contextlib.contextmanager
def hold(treatment_kinds, treatment_targets, cancel, exclusive):
    """A run admission that holds nothing and says so."""
    yield "shared"


def passing() -> Health:
    """A health check that always passes."""
    return Health(True, "the toy platform is up")


def test_o35_a_campaign_whose_admission_needs_a_health_check_and_has_none_is_refused_when_the_scheduler_is_built(
    tmp_path,
):
    """O35: the refusal names why (the platform's own words) and the fix, comes from the constructor, and leaves the
    campaign's directory as it was: no lock is taken and no store opened."""
    with pytest.raises(ConfigError) as refused:
        build(tmp_path, plan=SMALL, admission=requiring(hold, WHY))
    message = str(refused.value)
    assert WHY in message
    assert "requires a health check" in message
    assert "plugin's `health`" in message
    assert not (tmp_path / "campaign.lock").exists()


def test_o35_a_campaign_whose_admission_needs_a_health_check_starts_with_one(tmp_path):
    """O35: with any health check the campaign starts and runs every run: the need is that one is configured."""
    executor = FakeExecutor()
    admission = requiring(hold, WHY)
    with build(
        tmp_path, executor=executor, plan=SMALL, settings=settings_for(), health=passing, admission=admission
    ) as scheduler:
        summary = scheduler.run()
    assert summary.statuses
    assert set(summary.statuses) == {"done"}


def test_o35_an_admission_that_declares_no_need_starts_without_a_health_check_as_before(tmp_path):
    """O35: a platform with no such declaration keeps today's behaviour: no health check, and its runs are admitted."""
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL, settings=settings_for(), admission=hold) as scheduler:
        summary = scheduler.run()
    assert set(summary.statuses) == {"done"}


def test_o35_a_campaign_with_no_admission_and_no_health_check_starts_as_before(tmp_path):
    """O35: no admission, so nothing to need a health check."""
    with build(tmp_path, executor=FakeExecutor(), plan=SMALL, settings=settings_for()) as scheduler:
        summary = scheduler.run()
    assert set(summary.statuses) == {"done"}


def test_o35_the_declaration_is_the_admissions_attribute_and_the_admission_still_admits():
    """O35: `requiring_health_check` returns an admission that is called as the one it wraps, and declares the need in
    the attribute the core reads; an admission with no such attribute, and none at all, declare none."""
    admission = requiring(hold, WHY)
    with admission((), {}, None, False) as held:
        assert held == "shared"
    assert getattr(admission, ATTRIBUTE) == WHY
    assert required_reason(admission) == WHY
    assert required_reason(hold) is None
    assert required_reason(None) is None


@pytest.mark.parametrize("reason", ["", "   ", None, 1])
def test_o35_a_platform_cannot_declare_the_need_without_saying_why(reason):
    """O35: the reason is what the refusal shows a study's author, so an empty or non-text one is the platform's own
    bug and is refused where it is made."""
    with pytest.raises(ValueError, match="reason"):
        requiring(hold, reason)


def test_o35_declaring_a_need_hides_no_other_declaration_of_the_wrapped_admission():
    """O35: the core reads other hooks off the admission it is given (the deferral the platform declares, say), so the
    admission that declares a need passes every other attribute of the one it wraps through, and an attribute the
    wrapped one lacks is still absent."""

    class Declares:
        def __call__(self, *args):
            return hold(*args)

        def declared_deferral_s(self):
            return 42

    wrapped = requiring(Declares(), WHY)
    assert wrapped.declared_deferral_s() == 42
    assert getattr(wrapped, "no_such_hook", None) is None
    assert required_reason(wrapped) == WHY


def test_o35_the_declaring_admission_is_called_with_keywords_as_the_admission_it_wraps_is():
    """O35: the wrapper forwards what it is called with, keywords included, so a caller that named its arguments
    before the declaration still works."""
    with requiring(hold, WHY)(
        treatment_kinds=(), treatment_targets={}, cancel=threading.Event(), exclusive=False
    ) as held:
        assert held == "shared"


@pytest.mark.parametrize("declared", ["", "  ", 1, True, False, None, ["a reason"]])
def test_o35_an_admission_whose_own_declaration_is_not_a_reason_is_refused_at_the_boundary(declared):
    """O35: an admission that sets the attribute itself, to anything but a non-empty text (an explicit None and False
    included), is refused with a message naming the attribute, whether or not the campaign has a health check: a
    malformed declaration is never read as 'none'; an admission that leaves it out declares none."""

    class Declares:
        requires_health_check = declared

        def __call__(self, *args):
            return hold(*args)

    for health in (None, passing):
        with pytest.raises(ConfigError, match=ATTRIBUTE):
            refuse_without_health_check(Declares(), health)


def test_o35_an_admission_may_set_the_attribute_itself_as_well_as_through_the_helper(tmp_path):
    """O35: the contract is the attribute, so an admission of the study's own that sets it is held to it too."""

    class Declares:
        requires_health_check = WHY

        def __call__(self, *args):
            return hold(*args)

    with pytest.raises(ConfigError, match="requires a health check"):
        build(tmp_path, plan=SMALL, admission=Declares())
