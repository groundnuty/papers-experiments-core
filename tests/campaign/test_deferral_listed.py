"""C11: a deferral a run's admission or health check declares must come from a platform module the study lists.

The core reads a platform's declared deferral from its configuration through the modules `[study] platforms` lists
(`Scheduler(platform_deferrals=...)`, `test_deferral_registry`). A study that uses a platform without listing its module
is never asked, so a study that wraps its objects would hide the figure. A platform's bound therefore names the module
it comes from (`DeferralBound.platform`), and when the campaign starts the scheduler refuses a bound, declared by the
admission (`declared_deferral_s`) or by the health check (`max_deferral_s`), that names a module `[study] platforms`
does not list: listing it is required whenever a platform with a deferral is used. A bound that names no module, a
study whose platform declares none, and a scheduler built in code with no list are unaffected.
"""

import pytest
from campaign_support import make_schema
from scheduler_support import build, seed_store, settings_for

from expcore.campaign.healthgate import DeferralBound, Health, PlatformDeferral
from expcore.config.loading import ConfigError

SMALL = {"tasks": ("task-a",), "seeds": (1,)}
PLATFORM = "toy_platform.kinds"
BOUND = DeferralBound(4700.0, "the platform's heal: lower heal_timeout_s", rechecks=2, checks=2, platform=PLATFORM)
CORE_WAITS = {"recheck_s": 30.0, "poll_interval_s": 1.0, "health_timeout_s": 60.0}
ENOUGH = settings_for(max_defer_s=4700.0 + 2 * 31.0 + 2 * 60.0, **CORE_WAITS)


class Declaring:
    """A health check that passes and declares the longest it may stay deferred (`max_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self):
        return Health(True, "fine")

    def max_deferral_s(self):
        return self.bound


class Platform:
    """A run admission that declares the platform's deferral (`declared_deferral_s`)."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self, treatment_kinds, treatment_targets, cancel, exclusive):
        raise AssertionError("the scheduler does not enter the admission when it is built")

    def declared_deferral_s(self):
        return self.bound


def refused_unlisted(tmp_path, **options):
    """The text of the refusal of a campaign built with `options` and a study that lists no platform."""
    with pytest.raises(ConfigError) as refused:
        build(tmp_path, listed_platforms=(), settings=ENOUGH, plan=SMALL, **options)
    return str(refused.value)


@pytest.mark.parametrize(
    ("options", "owner"),
    [
        ({"health": Declaring(BOUND)}, "health check"),
        ({"health": Declaring(None), "admission": Platform(BOUND)}, "run admission"),
        ({"admission": Platform(BOUND)}, "run admission"),
        ({"health": Declaring(BOUND), "admission": Platform(BOUND)}, "health check"),
    ],
    ids=["health-check", "admission", "admission-and-no-health-check", "both"],
)
def test_c11_a_declaration_from_a_module_the_study_does_not_list_is_refused_naming_the_module(tmp_path, options, owner):
    """C11: the health check, or the run admission, declares a deferral that comes from `toy_platform.kinds`, which the
    study does not list: the campaign is refused when the scheduler is built, naming the module, the object that
    declared it and the fix, with or without a health check."""
    text = refused_unlisted(tmp_path, **options)
    for part in (f"{PLATFORM!r}", owner, "[study] platforms", "list"):
        assert part in text, text


def test_c11_the_refusal_comes_before_the_lock_and_the_store(tmp_path):
    """C11: nothing of the campaign is opened: the store is as it was seeded and there is no lock file."""
    root = tmp_path / "campaign"
    seed_store(root, make_schema())
    seeded = (root / "state.sqlite").read_bytes()
    with pytest.raises(ConfigError, match=PLATFORM):
        build(root, health=Declaring(BOUND), listed_platforms=("another_platform",), settings=ENOUGH, plan=SMALL)
    assert [path.name for path in root.iterdir()] == ["state.sqlite"]
    assert (root / "state.sqlite").read_bytes() == seeded


def test_c11_a_declaration_from_a_listed_module_is_read_as_before(tmp_path):
    """C11: the module is listed, so its declaration is held against the registry's and the health check's as ever: a
    check that carries it starts under a `max_defer_s` that covers it."""
    registry = [PlatformDeferral(PLATFORM, BOUND)]
    with build(
        tmp_path,
        health=Declaring(BOUND),
        admission=Platform(BOUND),
        platform_deferrals=registry,
        listed_platforms=(PLATFORM, "another_platform"),
        settings=ENOUGH,
        plan=SMALL,
    ):
        pass


@pytest.mark.parametrize(
    "bound",
    [None, DeferralBound(4700.0, "a heal"), DeferralBound(0.0, "nothing to wait for", platform=PLATFORM)],
    ids=["declares-none", "names-no-module", "zero-seconds"],
)
@pytest.mark.parametrize("listed", [(), ("another_platform",), None], ids=["empty", "another", "not-asked"])
def test_c11_a_platform_that_declares_no_deferral_from_a_module_is_unaffected(tmp_path, bound, listed):
    """C11: a platform that declares none, a bound that names no module (a platform that does not say, a study's own
    check) and a bound of no seconds ask nothing of the study's list: any list, or none, starts the campaign."""
    options = {"health": Declaring(bound), "admission": Platform(bound)}
    with build(tmp_path, listed_platforms=listed, settings=ENOUGH, plan=SMALL, **options):
        pass


def test_c11_a_scheduler_built_in_code_with_no_list_asks_nothing_of_it(tmp_path):
    """C11: `listed_platforms` is the study's list, which the pipeline passes; a scheduler built without it reads no
    list and refuses nothing for want of one, as before."""
    with build(tmp_path, health=Declaring(BOUND), admission=Platform(BOUND), settings=ENOUGH, plan=SMALL):
        pass


@pytest.mark.parametrize("platform", [3, None, ["toy_platform.kinds"]], ids=["number", "none", "list"])
def test_c11_a_platform_name_that_is_not_text_is_refused_naming_the_hook(tmp_path, platform):
    """C11: the module a bound names crosses into the core as the rest of the bound does: anything but text is refused,
    naming the method that answered."""
    bad = BOUND._replace(platform=platform)
    with pytest.raises(ConfigError, match="max_deferral_s"):
        build(tmp_path, health=Declaring(bad), listed_platforms=(PLATFORM,), settings=ENOUGH, plan=SMALL)
