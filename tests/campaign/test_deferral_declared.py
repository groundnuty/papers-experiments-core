"""C11: what the platform declares about its own deferral is held against the health check the scheduler is given.

A health check may declare the longest it can stay deferred (`max_deferral_s`), and the scheduler refuses a
`max_defer_s` shorter than that. A study that wraps the platform's check in a closure of its own hides the method, so
the scheduler would read no bound and hold the campaign to nothing. The platform therefore declares its deferral a
second time, without the health object: its run admission may have a method `declared_deferral_s()` answering the same
`DeferralBound`, read from the platform's settings when the scheduler is built. A declaration the health check does not
carry (no method, or a smaller bound) refuses the campaign at its start.
"""

import pytest
from campaign_support import make_schema
from scheduler_support import build, seed_store, settings_for

from expcore.campaign.healthgate import DeferralBound, Health
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
DECLARED = DeferralBound(4700.0, "the platform's heal: lower heal_timeout_s or switch it off", rechecks=2, checks=2)
CORE_WAITS = {"recheck_s": 30.0, "poll_interval_s": 1.0, "health_timeout_s": 60.0}
NEEDED = 4700.0 + 2 * (30.0 + 1.0) + 2 * 60.0  # the platform's own seconds and the core's waits it declares: 4,882 s
ENOUGH = settings_for(max_defer_s=NEEDED, **CORE_WAITS)


class Platform:
    """A run admission that declares the platform's deferral from its settings (`declared_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self, treatment_kinds, treatment_targets, cancel, exclusive):
        raise AssertionError("the scheduler does not enter the admission when it is built")

    def declared_deferral_s(self):
        return self.bound


class Declaring:
    """A health check that passes and declares the longest it may stay deferred itself (`max_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self):
        return Health(True, "fine")

    def max_deferral_s(self):
        return self.bound


def wrapped(check):
    """A study's own health check around the platform's: a closure that answers what the check answers and has no
    `max_deferral_s`, however many the wrapped one has."""

    def health():
        return check()

    return health


class Passing:
    """A study's own wrapper that passes `max_deferral_s` on."""

    def __init__(self, check):
        self.check = check

    def __call__(self):
        return self.check()

    def max_deferral_s(self):
        return self.check.max_deferral_s()


def test_c11_a_wrapped_health_check_that_hides_the_platforms_deferral_is_refused_before_the_store_opens(tmp_path):
    """C11: the platform declares a deferral (its admission's `declared_deferral_s`) and the health check the scheduler
    is given exposes no `max_deferral_s`, so nothing would hold `max_defer_s` to the heal: the campaign is refused when
    the scheduler is built, before the lock is taken or the store opened, naming the missing method, the platform's
    figure and words and the fix."""
    root = tmp_path / "campaign"
    seed_store(root, make_schema())
    seeded = (root / "state.sqlite").read_bytes()
    health = wrapped(Declaring(DECLARED))
    assert not hasattr(health, "max_deferral_s")
    with pytest.raises(ConfigError) as refused:
        build(root, health=health, admission=Platform(DECLARED), settings=ENOUGH, plan=SMALL)
    text = str(refused.value)
    parts = (
        "the health check passed to the scheduler doesn't expose max_deferral_s",
        f"the platform declares a deferral of {NEEDED:g} s",
        "4700 s of the platform's own",
        DECLARED.detail,
        "2 x health_timeout_s 60 s",
        "pass the method on",
    )
    for part in parts:
        assert part in text, text
    assert [path.name for path in root.iterdir()] == ["state.sqlite"]  # no lock file
    assert (root / "state.sqlite").read_bytes() == seeded  # the store was never opened


def test_c11_a_health_check_that_passes_the_method_on_starts_and_is_still_held_to_max_defer_s(tmp_path):
    """C11: a wrapper that passes `max_deferral_s` on carries what the platform declares, so the campaign starts when
    its `max_defer_s` covers the bound, and is refused, as without any declaration, when it does not."""
    health = Passing(Declaring(DECLARED))
    with build(tmp_path / "enough", health=health, admission=Platform(DECLARED), settings=ENOUGH, plan=SMALL):
        pass
    short = settings_for(max_defer_s=NEEDED - 1, **CORE_WAITS)
    with pytest.raises(ConfigError, match=f"raise max_defer_s to at least {NEEDED:g}"):
        build(tmp_path / "short", health=health, admission=Platform(DECLARED), settings=short, plan=SMALL)


@pytest.mark.parametrize(
    "options",
    [
        {"admission": Platform(None)},
        {"admission": lambda kinds, targets, cancel, exclusive: None},
        {},
    ],
    ids=["declares-none", "admission-without-the-method", "no-admission"],
)
@pytest.mark.parametrize("health", [wrapped(Declaring(None)), Declaring(None)], ids=["wrapped", "plain"])
def test_c11_a_platform_that_declares_no_deferral_is_unaffected(tmp_path, health, options):
    """C11: nothing is declared, so a health check without the method, wrapped or not, is held to nothing, as before,
    and any positive `max_defer_s` starts."""
    with build(tmp_path, health=health, settings=settings_for(max_defer_s=1.0), plan=SMALL, **options):
        pass


def test_c11_a_declared_deferral_without_a_health_check_is_unaffected(tmp_path):
    """C11: no health check is passed, so nothing defers and nothing needs the bound."""
    with build(tmp_path, admission=Platform(DECLARED), settings=settings_for(max_defer_s=1.0), plan=SMALL):
        pass


@pytest.mark.parametrize(
    ("bound", "words"),
    [
        (DeferralBound(4000.0, "a heal that forgot the drain", rechecks=2, checks=2), "declares a deferral of 4182 s"),
        (DeferralBound(4700.0, "a heal that forgot a check", rechecks=2, checks=0), "declares a deferral of 4762 s"),
        (DeferralBound(4700.0, "a heal that forgot a recheck", rechecks=0, checks=2), "declares a deferral of 4820 s"),
        (None, "answers max_deferral_s() with None"),
    ],
    ids=["fewer-seconds", "fewer-checks", "fewer-rechecks", "none"],
)
def test_c11_a_health_check_whose_bound_is_smaller_than_the_platforms_is_refused_naming_both(tmp_path, bound, words):
    """C11: a health check that does declare a longest deferral, but a smaller one than the platform declares (a
    wrapper that rebuilt it, a check made from other settings), would let the campaign start on a `max_defer_s` the
    heal outlasts: the campaign is refused, naming what the check declares and what the platform does."""
    health = Declaring(bound)
    settings = settings_for(max_defer_s=NEEDED, **CORE_WAITS)
    with pytest.raises(ConfigError) as refused:
        build(tmp_path, health=health, admission=Platform(DECLARED), settings=settings, plan=SMALL)
    text = str(refused.value)
    for part in (words, f"the platform declares a deferral of {NEEDED:g} s", DECLARED.detail, "pass the method on"):
        assert part in text, text
    assert "max_deferral_s" in text, text


@pytest.mark.parametrize(
    "bound",
    [
        DECLARED,
        DeferralBound(4800.0, "another process's longer heal", rechecks=2, checks=2),
        DECLARED._replace(checks=3),
    ],
    ids=["equal", "longer", "more-checks"],
)
def test_c11_a_health_check_whose_bound_covers_the_platforms_is_accepted(tmp_path, bound):
    """C11: a check that declares the platform's bound, or a longer one, agrees with it: the campaign is then held to
    the check's own (the longer) figure as before."""
    needed = bound.seconds + bound.rechecks * 31.0 + bound.checks * 60.0
    health = Declaring(bound)
    with build(
        tmp_path / "fits",
        health=health,
        admission=Platform(DECLARED),
        settings=settings_for(max_defer_s=needed, **CORE_WAITS),
        plan=SMALL,
    ):
        pass
    with pytest.raises(ConfigError, match="raise max_defer_s"):
        build(
            tmp_path / "short",
            health=health,
            admission=Platform(DECLARED),
            settings=settings_for(max_defer_s=needed - 1, **CORE_WAITS),
            plan=SMALL,
        )


@pytest.mark.parametrize(
    "answer",
    [5000.0, "an hour", DeferralBound(-1.0, "a heal"), DeferralBound(float("nan"), "a heal"), DeferralBound(5.0, None)],
    ids=["number", "text", "negative", "nan", "no-detail"],
)
def test_c11_a_declaration_that_is_not_a_deferral_bound_refuses_the_start_naming_the_method(tmp_path, answer):
    """C11: what the platform's admission declares crosses into the core as what a check's bound does, so anything but
    None or a `DeferralBound` of a finite, non-negative number of seconds, a detail and non-negative whole counts is
    refused, naming `declared_deferral_s`."""
    with pytest.raises(ConfigError, match="declared_deferral_s"):
        build(tmp_path, health=Declaring(None), admission=Platform(answer), plan=SMALL)
