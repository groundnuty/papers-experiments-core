"""O35: a campaign whose `max_defer_s` the fault gate's heal can outlast is refused at its start.

While the gate heals stuck pairs every check answers the uncharged deferral, for up to the heal's bound
(`heal_timeout_s` plus `heal_settle_s`), and after an outage run a session drain (`session_drain_s`) can come first,
with no check between them that passes. The core's `max_defer_s` (3,600 s by default) turns a longer deferral into a
blocking hold, and a campaign that exits when blocked (every `run`) would stop on a heal it should have waited out. So
the platform's check declares that bound (`max_deferral_s`), and the core refuses a shorter `max_defer_s` at the start.

The bound also holds the waits between those, which are the core's own settings: after the drain one `recheck_s` (and
`poll_interval_s`) passes and then the check that starts the heal (`health_timeout_s`), and after the heal another
`recheck_s` and the check that makes the link check afresh (`health_timeout_s`), which a call meeting it waits for,
uncharged (the last deferred answer before the heal ends is followed by a check one recheck later). The platform
declares how many of each (`DeferralBound.rechecks`, `checks`) and the core adds its own figures.

The platform declares the bound twice: its health check (`max_deferral_s`) and its run admission
(`declared_deferral_s`, from the same `[faults]`), so a study's check around the platform's that hides the method, or
declares less, is refused at the start instead of holding the campaign to nothing.
"""

from functools import partial

import pytest
from fault_support import FakeServices, faults_table
from heal_support import heal_margin_s
from scheduler_support import build, settings_for

from expcore.config.loading import ConfigError
from expcore_onedata.admission import fault_settings_of, run_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer

FIVE = ("alpha", "beta", "gamma", "delta", "epsilon")
FIVE_STORES = {name: f"store-{name}" for name in FIVE}
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
POLL = {"poll_interval_s": 1.0}  # the core's defaults (recheck_s 30 s, health_timeout_s 60 s) with a poll of 1 s
CORE = settings_for(**POLL)
# What the core adds to the platform's own seconds: with a session drain a recheck and the check that starts the heal,
# and a recheck and the check after it; without one only the pair after the heal (the first deferral is then the
# heal's own start).
BETWEEN = CORE.recheck_s + CORE.poll_interval_s  # 31 s
AFTER_A_DRAIN = 2 * BETWEEN + 2 * CORE.health_timeout_s  # 182 s
WITHOUT_A_DRAIN = BETWEEN + CORE.health_timeout_s  # 91 s


def five_providers(data, **faults):
    """Five providers, each with a service, at the designed bounds of a restore's waits; the heal's bound is declared
    (the least five providers' `"all_links"` with a container fallback needs at these bounds, 4,290 s), so no default
    moves the figures, and the token margin is the one a declared heal needs at load."""
    for name, index in (("gamma", 3), ("delta", 4), ("epsilon", 5)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    designed = {"settle_timeout_s": 180, "ready_timeout_s": 180, "heal_timeout_s": 4290, "session_drain_s": 320}
    data["faults"] = faults_table(**{"services": dict(FIVE_STORES), **designed, **faults})
    data["tokens"] = {**data.get("tokens", {}), "margin_s": heal_margin_s(data["faults"])}


def four_providers(data, **faults):
    """Four providers at the defaults of the heal: the 4,200 s and 320 s a declared heal gets with no other word, and
    the token margin it needs."""
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    stores = {name: f"store-{name}" for name in FIVE[:4]}
    data["faults"] = faults_table(**{"services": stores, "heal": "all_links", "session_drain_s": 320, **faults})
    data["tokens"] = {**data.get("tokens", {}), "margin_s": heal_margin_s(data["faults"])}


def platform_check(tmp_path, fake, edit):
    """The platform's standard health check over the platform.toml `edit` writes, and that file's `[faults]`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    platform = write_platform(tmp_path, edit)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    services = FakeServices(tuple(FIVE_STORES.values()))
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, fault_driver=services)
    return health, fault_settings_of(config)


@pytest.mark.parametrize(
    ("drain_s", "own_s", "needed_s"),
    [(320, 4790.0, 4790.0 + AFTER_A_DRAIN), (0, 4470.0, 4470.0 + WITHOUT_A_DRAIN)],
    ids=["with-a-drain", "no-drain"],
)
def test_o35_five_providers_healing_all_links_refuse_the_default_max_defer_s_at_the_start(
    tmp_path, fake, drain_s, own_s, needed_s
):
    """O35: five providers healing `"all_links"` defer the gate for up to `session_drain_s`, `heal_timeout_s` and
    `heal_settle_s` (4,790 s at the designed bounds, 4,470 s with no drain) and the core's own waits around them (182 s
    with a drain, 91 s without), 4,972 s and 4,561 s in all with the defaults, past the default `max_defer_s` of 3,600
    s: the campaign is refused when it starts, naming the figures and the fix."""
    edit = partial(five_providers, heal="all_links", session_drain_s=drain_s)
    health, faults = platform_check(tmp_path / "platform", fake, edit)
    assert faults.session_drain_s + faults.heal_timeout_s + faults.heal_settle_s == own_s
    with pytest.raises(ConfigError) as refused:
        build(tmp_path / "campaign", health=health, settings=CORE, plan=SMALL)
    text = str(refused.value)
    for part in ("[scheduler] max_defer_s", "3600 s", f"up to {needed_s:g} s", 'heal = "all_links"', 'heal = "off"'):
        assert part in text, text


def test_o35_four_providers_at_the_defaults_need_4882_s_and_five_need_4972_s(tmp_path, fake):
    """O35: the figures with the defaults, a drain of 320 s, a heal of 4,200 s (4,290 s for five providers) and its
    settle of 180 s: the platform's own 4,700 s and 4,790 s, and the core's 182 s around them, 4,882 s and 4,972 s."""
    four, _ = platform_check(tmp_path / "four", fake, four_providers)
    five, _ = platform_check(tmp_path / "five", fake, partial(five_providers, heal="all_links"))
    assert [health.max_deferral_s().seconds for health in (four, five)] == [4700.0, 4790.0]
    for health, needed in ((four, 4882.0), (five, 4972.0)):
        with pytest.raises(ConfigError, match=f"up to {needed:g} s"):
            build(tmp_path / f"campaign-{needed:g}", health=health, settings=CORE, plan=SMALL)
        with build(
            tmp_path / f"fits-{needed:g}", health=health, settings=settings_for(max_defer_s=needed, **POLL), plan=SMALL
        ):
            pass


def test_o35_the_bound_names_every_term_and_the_refusal_gives_each_its_value(tmp_path, fake):
    """O35: what the platform declares names each wait it holds, and the refusal gives every one its figure: the drain,
    the heal's two bounds, and the core's `recheck_s`, `poll_interval_s` and `health_timeout_s`; a platform that
    declares a drain counts two rechecks and two checks, and one with none counts those after the heal."""
    health, _ = platform_check(tmp_path / "platform", fake, partial(five_providers, heal="all_links"))
    bound = health.max_deferral_s()
    assert (bound.rechecks, bound.checks) == (2, 2)
    for part in (
        "session_drain_s (320 s)",
        "heal_timeout_s (4290 s)",
        "heal_settle_s (180 s)",
        "recheck_s",
        "poll_interval_s",
    ):
        assert part in bound.detail, bound.detail
    for part in ("the check that starts the heal (its health_timeout_s)", "after the heal (its health_timeout_s)"):
        assert part in bound.detail, bound.detail  # a check of the core's `health_timeout_s` each, twice
    assert "assumed to end within its health_timeout_s" in bound.detail, bound.detail
    with pytest.raises(ConfigError) as refused:
        build(tmp_path / "campaign", health=health, settings=CORE, plan=SMALL)
    text = str(refused.value)
    for part in ("4790 s of the platform's own", "recheck_s 30 s", "poll_interval_s 1 s", "2 x health_timeout_s 60 s"):
        assert part in text, text
    quick, _ = platform_check(tmp_path / "quick", fake, partial(five_providers, heal="all_links", session_drain_s=0))
    undrained = quick.max_deferral_s()
    assert (undrained.rechecks, undrained.checks) == (1, 1)
    assert "the check that starts the heal (its health_timeout_s)" not in undrained.detail, undrained.detail
    assert "after the heal (its health_timeout_s)" in undrained.detail, undrained.detail  # only the check after it


@pytest.mark.parametrize("drain_s", [320, 0], ids=["with-a-drain", "no-drain"])
def test_o35_a_max_defer_s_raised_to_the_bound_and_the_cores_waits_starts_and_one_second_less_is_refused(
    tmp_path, fake, drain_s
):
    """O35: raising `max_defer_s` to the declared bound and the core's waits around it starts the campaign; a second
    less is refused."""
    health, faults = platform_check(
        tmp_path / "platform", fake, partial(five_providers, heal="all_links", session_drain_s=drain_s)
    )
    own = faults.session_drain_s + faults.heal_timeout_s + faults.heal_settle_s
    assert health.max_deferral_s().seconds == own
    needed = own + (AFTER_A_DRAIN if drain_s else WITHOUT_A_DRAIN)
    with pytest.raises(ConfigError, match="max_defer_s"):
        build(tmp_path / "short", health=health, settings=settings_for(max_defer_s=needed - 1, **POLL), plan=SMALL)
    with build(tmp_path / "campaign", health=health, settings=settings_for(max_defer_s=needed, **POLL), plan=SMALL):
        pass


def test_o35_with_the_heal_off_the_platform_declares_no_longest_deferral(tmp_path, fake):
    """O35: with `heal = "off"` the gate heals nothing, so its check declares no longest deferral and any
    `max_defer_s` starts."""
    health, _ = platform_check(tmp_path / "platform", fake, partial(five_providers, heal="off"))
    assert health.max_deferral_s() is None
    with build(tmp_path / "campaign", health=health, settings=settings_for(max_defer_s=1.0), plan=SMALL):
        pass


def test_o35_without_faults_the_platform_declares_no_longest_deferral(tmp_path, fake):
    """O35: a platform.toml with no `[faults]` has no fault gate, and its check declares no longest deferral."""
    health, faults = platform_check(tmp_path / "platform", fake, lambda data: None)
    assert faults is None
    assert health.max_deferral_s() is None


def wrapped(check):
    """A study's own health check around the platform's: a closure that calls it and has no `max_deferral_s`."""

    def health():
        return check()

    return health


class Passing:
    """A study's own health check around the platform's that passes `max_deferral_s` on."""

    def __init__(self, check):
        self.check = check

    def __call__(self):
        return self.check()

    def max_deferral_s(self):
        return self.check.max_deferral_s()


def declared_by_the_platform(tmp_path, fake, edit):
    """The platform's health check and its run admission over one platform.toml, and that file's configuration."""
    health, _ = platform_check(tmp_path, fake, edit)
    config = load_platform(tmp_path / "platform.toml")
    return health, run_admission(config), config


def test_o35_the_platforms_run_admission_declares_the_deferral_its_health_check_does(tmp_path, fake):
    """O35: the run admission reads the longest deferral from the same `[faults]` settings as the health check, with no
    health object between: the same `DeferralBound` for a declared heal, with a drain and without one, and none with
    the heal off."""
    for name, edit in (
        ("drained", partial(five_providers, heal="all_links")),
        ("undrained", partial(five_providers, heal="all_links", session_drain_s=0)),
        ("off", partial(five_providers, heal="off")),
    ):
        health, admission, _ = declared_by_the_platform(tmp_path / name, fake, edit)
        assert admission.declared_deferral_s() == health.max_deferral_s(), name
    assert admission.declared_deferral_s() is None
    _, admit, _ = declared_by_the_platform(tmp_path / "again", fake, partial(five_providers, heal="all_links"))
    assert (admit.declared_deferral_s().seconds, admit.declared_deferral_s().rechecks) == (4790.0, 2)
    assert callable(admit)  # still what the core enters around every attempt


def test_o35_a_study_check_that_wraps_the_platforms_and_hides_the_method_is_refused_at_the_start(tmp_path, fake):
    """O35: a study's plugin wraps the platform's health check in a closure of its own, so the object the scheduler
    receives has no `max_deferral_s`. The platform's admission still declares the heal's deferral, so the campaign is
    refused when it is built, naming the missing method and the figure, before the lock is taken or the store opened,
    even with a `max_defer_s` that would cover the bound."""
    health, admission, _ = declared_by_the_platform(
        tmp_path / "platform", fake, partial(five_providers, heal="all_links")
    )
    assert health.max_deferral_s() is not None
    assert not hasattr(wrapped(health), "max_deferral_s")
    covers = settings_for(max_defer_s=4972.0, **POLL)
    with pytest.raises(ConfigError) as refused:
        build(tmp_path / "campaign", health=wrapped(health), admission=admission, settings=covers, plan=SMALL)
    text = str(refused.value)
    for part in (
        "the health check passed to the scheduler doesn't expose max_deferral_s",
        "the platform declares a deferral of 4972 s",
        'heal = "all_links"',
        "pass the method on",
    ):
        assert part in text, text
    assert [path.name for path in (tmp_path / "campaign").iterdir()] == ["state.sqlite"]  # no campaign.lock


def test_o35_a_study_check_that_passes_the_method_on_starts_under_a_max_defer_s_that_covers_the_bound(tmp_path, fake):
    """O35: the same wrapper with `max_deferral_s` passed on carries the declaration, so the campaign starts when its
    `max_defer_s` covers the bound (4,972 s for five providers) and is refused by that bound otherwise."""
    health, admission, _ = declared_by_the_platform(
        tmp_path / "platform", fake, partial(five_providers, heal="all_links")
    )
    with build(
        tmp_path / "fits",
        health=Passing(health),
        admission=admission,
        settings=settings_for(max_defer_s=4972.0, **POLL),
        plan=SMALL,
    ):
        pass
    with pytest.raises(ConfigError, match="raise max_defer_s to at least 4972"):
        build(tmp_path / "short", health=Passing(health), admission=admission, settings=CORE, plan=SMALL)


def test_o35_a_platform_without_a_declared_heal_is_unaffected_by_a_wrapped_check(tmp_path, fake):
    """O35: with `heal = "off"`, or without `[faults]`, the platform declares no deferral, so a study's check that hides
    `max_deferral_s` is held to nothing, as before: any positive `max_defer_s` starts."""
    health, admission, _ = declared_by_the_platform(tmp_path / "off", fake, partial(five_providers, heal="off"))
    with build(
        tmp_path / "campaign-off",
        health=wrapped(health),
        admission=admission,
        settings=settings_for(max_defer_s=1.0),
        plan=SMALL,
    ):
        pass
    plain, none, _ = declared_by_the_platform(tmp_path / "plain", fake, lambda data: None)
    assert none is None  # no `[faults]`, no admission
    with build(tmp_path / "campaign-plain", health=wrapped(plain), settings=settings_for(max_defer_s=1.0), plan=SMALL):
        pass


def test_o35_a_study_check_built_from_other_settings_declares_less_and_is_refused_naming_both(tmp_path, fake):
    """O35: a health check made from a platform.toml with no session drain, where the admission was made from one with
    the drain, declares a smaller bound than the platform does: the campaign is refused, naming both figures."""
    _, admission, _ = declared_by_the_platform(tmp_path / "long", fake, partial(five_providers, heal="all_links"))
    undrained = partial(five_providers, heal="all_links", session_drain_s=0)
    quick, _, _ = declared_by_the_platform(tmp_path / "short", fake, undrained)
    assert quick.max_deferral_s().seconds < admission.declared_deferral_s().seconds
    with pytest.raises(ConfigError) as refused:
        build(
            tmp_path / "campaign",
            health=Passing(quick),
            admission=admission,
            settings=settings_for(max_defer_s=9999.0, **POLL),
            plan=SMALL,
        )
    text = str(refused.value)
    for part in ("declares a deferral of 4561 s through max_deferral_s", "the platform declares a deferral of 4972 s"):
        assert part in text, text
