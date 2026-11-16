"""C11: a platform's declared deferral, read from its own configuration through the platform registry, is held against
the health check the scheduler is given, whatever the study's plugin wrapped.

The platform's run admission declares its deferral a second time (`declared_deferral_s`, #298), but a study that wraps
its run admission hides that method as a wrapped health check hides `max_deferral_s`. So the core also asks each
platform the study lists in `[study] platforms` for it, given the platform configuration the study points at, and
passes the answers to the scheduler (`Scheduler(platform_deferrals=...)`): no object of the study's plugin sits between
the platform's settings and the comparison. A health check that exposes no `max_deferral_s`, answers None or declares
less is refused when the scheduler is built, before the lock or the store, naming both figures.
"""

import pytest
from campaign_support import make_schema
from scheduler_support import build, seed_store, settings_for

from expcore.campaign.healthgate import DeferralBound, Health, PlatformDeferral
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
PLATFORM = "toy_platform.kinds"
DECLARED = DeferralBound(4700.0, "the platform's heal: lower heal_timeout_s or switch it off", rechecks=2, checks=2)
CORE_WAITS = {"recheck_s": 30.0, "poll_interval_s": 1.0, "health_timeout_s": 60.0}
NEEDED = 4700.0 + 2 * (30.0 + 1.0) + 2 * 60.0  # the platform's own seconds and the core's waits it declares: 4,882 s
ENOUGH = settings_for(max_defer_s=NEEDED, **CORE_WAITS)
REGISTRY = [PlatformDeferral(PLATFORM, DECLARED)]


class Declaring:
    """A health check that passes and declares the longest it may stay deferred itself (`max_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self):
        return Health(True, "fine")

    def max_deferral_s(self):
        return self.bound


def wrapped(check):
    """A study's own check around the platform's: a closure with no `max_deferral_s`, whatever the wrapped one has."""

    def health():
        return check()

    return health


def wrapped_admission(admit):
    """A study's own admission around the platform's: a callable of its own, so `declared_deferral_s` is hidden."""

    def admission(treatment_kinds, treatment_targets, cancel, exclusive):
        return admit(treatment_kinds, treatment_targets, cancel, exclusive)

    return admission


class Platform:
    """A platform's run admission, which declares its deferral from its settings (`declared_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self, treatment_kinds, treatment_targets, cancel, exclusive):
        raise AssertionError("the scheduler does not enter the admission when it is built")

    def declared_deferral_s(self):
        return self.bound


def test_c11_a_study_wrapping_both_its_health_check_and_its_admission_is_refused(tmp_path):
    """C11: the study's plugin wraps the health check (no `max_deferral_s`) and the platform's admission (no
    `declared_deferral_s`), so nothing the plugin built carries the platform's deferral. The platform's own
    configuration, read through the registry, still declares it, and the campaign is refused when the scheduler is
    built, before the lock or the store: naming the missing method, the platform, its figure and words and the fix."""
    root = tmp_path / "campaign"
    seed_store(root, make_schema())
    seeded = (root / "state.sqlite").read_bytes()
    health = wrapped(Declaring(DECLARED))
    admission = wrapped_admission(Platform(DECLARED))
    assert not hasattr(health, "max_deferral_s")
    assert not hasattr(admission, "declared_deferral_s")
    with pytest.raises(ConfigError) as refused:
        build(root, health=health, admission=admission, platform_deferrals=REGISTRY, settings=ENOUGH, plan=SMALL)
    text = str(refused.value)
    parts = (
        "the health check passed to the scheduler doesn't expose max_deferral_s",
        f"the platform {PLATFORM!r} (its configuration, read through [study] platforms) declares a deferral of "
        f"{NEEDED:g} s",
        "4700 s of the platform's own",
        DECLARED.detail,
        "2 x health_timeout_s 60 s",
        "pass the method on",
    )
    for part in parts:
        assert part in text, text
    assert [path.name for path in root.iterdir()] == ["state.sqlite"]  # no lock file
    assert (root / "state.sqlite").read_bytes() == seeded  # the store was never opened


def test_c11_a_study_that_passes_the_methods_on_starts_and_is_still_held_to_max_defer_s(tmp_path):
    """C11: a health check that carries the platform's declared bound agrees with the registry, so the campaign starts
    when `max_defer_s` covers it and is refused, as without any declaration, when it does not."""
    health = Declaring(DECLARED)
    with build(tmp_path / "enough", health=health, platform_deferrals=REGISTRY, settings=ENOUGH, plan=SMALL):
        pass
    short = settings_for(max_defer_s=NEEDED - 1, **CORE_WAITS)
    with pytest.raises(ConfigError, match=f"raise max_defer_s to at least {NEEDED:g}"):
        build(tmp_path / "short", health=health, platform_deferrals=REGISTRY, settings=short, plan=SMALL)


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
def test_c11_a_health_check_declaring_less_than_the_registry_is_refused_naming_both(tmp_path, bound, words):
    """C11: a check built from other settings than the platform's configuration (the study's plugin loaded another
    platform.toml, or rebuilt the check) declares less, or nothing, and would let the campaign start on a `max_defer_s`
    the heal outlasts: refused, naming what the check declares and what the platform's configuration does."""
    with pytest.raises(ConfigError) as refused:
        build(tmp_path, health=Declaring(bound), platform_deferrals=REGISTRY, settings=ENOUGH, plan=SMALL)
    text = str(refused.value)
    for part in (words, f"declares a deferral of {NEEDED:g} s", DECLARED.detail, "pass the method on", PLATFORM):
        assert part in text, text


@pytest.mark.parametrize(
    "bound",
    [
        DECLARED,
        DeferralBound(4800.0, "another process's longer heal", rechecks=2, checks=2),
        DECLARED._replace(checks=3),
    ],
    ids=["equal", "longer", "more-checks"],
)
def test_c11_a_health_check_declaring_the_registrys_bound_or_more_is_accepted(tmp_path, bound):
    """C11: a check that declares the platform's bound, or a longer one (a configuration longer than the one the study
    points at), agrees with it; the campaign is then held to the check's own figure."""
    needed = bound.seconds + bound.rechecks * 31.0 + bound.checks * 60.0
    with build(
        tmp_path / "fits",
        health=Declaring(bound),
        platform_deferrals=REGISTRY,
        settings=settings_for(max_defer_s=needed, **CORE_WAITS),
        plan=SMALL,
    ):
        pass
    with pytest.raises(ConfigError, match="raise max_defer_s"):
        build(
            tmp_path / "short",
            health=Declaring(bound),
            platform_deferrals=REGISTRY,
            settings=settings_for(max_defer_s=needed - 1, **CORE_WAITS),
            plan=SMALL,
        )


def test_c11_the_registry_is_read_whatever_the_admission_declares(tmp_path):
    """C11: an admission that declares nothing (the study's wrapper, or a platform that does not), or declares the
    platform's bound itself, changes nothing about the registry's answer: a health check that does not carry it is
    refused, one that does starts."""
    admissions = (None, wrapped_admission(Platform(DECLARED)), Platform(None), Platform(DECLARED))
    for number, admission in enumerate(admissions):
        with pytest.raises(ConfigError, match="doesn't expose max_deferral_s"):
            build(
                tmp_path / f"hidden-{number}",
                health=wrapped(Declaring(DECLARED)),
                admission=admission,
                platform_deferrals=REGISTRY,
                settings=ENOUGH,
                plan=SMALL,
            )
        with build(
            tmp_path / f"carried-{number}",
            health=Declaring(DECLARED),
            admission=admission,
            platform_deferrals=REGISTRY,
            settings=ENOUGH,
            plan=SMALL,
        ):
            pass


def test_c11_the_admissions_own_declaration_is_still_checked_beside_the_registrys(tmp_path):
    """C11: #298's check is kept: an admission that declares more than the registry (the study's plugin built it from a
    longer configuration) refuses a health check that carries only the registry's figure, naming both."""
    longer = DeferralBound(5000.0, "a longer heal in another platform.toml", rechecks=2, checks=2)
    with pytest.raises(ConfigError) as refused:
        build(
            tmp_path,
            health=Declaring(DECLARED),
            admission=Platform(longer),
            platform_deferrals=REGISTRY,
            settings=settings_for(max_defer_s=10_000.0, **CORE_WAITS),
            plan=SMALL,
        )
    assert "declares a deferral of 4882 s" in str(refused.value)
    assert "the platform declares a deferral of 5182 s" in str(refused.value)


@pytest.mark.parametrize(
    "registry",
    [
        [],
        [PlatformDeferral(PLATFORM, None)],
        [PlatformDeferral(PLATFORM, None), PlatformDeferral("another_platform", None)],
        [PlatformDeferral(PLATFORM, DeferralBound(0.0, "nothing to wait for"))],
    ],
    ids=["none-asked", "declares-none", "several-declare-none", "zero-seconds"],
)
@pytest.mark.parametrize("health", [wrapped(Declaring(None)), Declaring(None)], ids=["wrapped", "plain"])
def test_c11_a_platform_that_declares_nothing_is_unaffected(tmp_path, health, registry):
    """C11: a platform without the registry hook is not asked, and one that answers None (a heal off, no `[faults]`)
    declares nothing: a health check without the method, wrapped or not, is held to nothing, as before, and any
    positive `max_defer_s` starts."""
    with build(
        tmp_path, health=health, platform_deferrals=registry, settings=settings_for(max_defer_s=1.0), plan=SMALL
    ):
        pass


def test_c11_a_declaration_with_no_health_check_is_unaffected(tmp_path):
    """C11: no health check is passed, so nothing defers and nothing needs the bound."""
    with build(tmp_path, platform_deferrals=REGISTRY, settings=settings_for(max_defer_s=1.0), plan=SMALL):
        pass


def test_c11_each_platform_a_study_lists_is_held_to_its_own_declaration(tmp_path):
    """C11: two platforms declare each its own figure, and the health check must carry the larger: the refusal names
    the platform whose figure it does not cover."""
    small = PlatformDeferral("first_platform", DeferralBound(100.0, "a short wait"))
    large = PlatformDeferral("second_platform", DECLARED)
    health = Declaring(DeferralBound(100.0, "a short wait"))
    with pytest.raises(ConfigError, match="'second_platform'"):
        build(tmp_path, health=health, platform_deferrals=[small, large], settings=ENOUGH, plan=SMALL)
    with build(
        tmp_path / "ok", health=Declaring(DECLARED), platform_deferrals=[small, large], settings=ENOUGH, plan=SMALL
    ):
        pass
