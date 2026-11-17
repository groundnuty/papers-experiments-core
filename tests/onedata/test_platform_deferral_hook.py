"""O35: the Onedata platform declares its longest deferral from its own platform.toml, through the module a study lists
in `[study] platforms` (`expcore_onedata.sandbox.kinds`), with no health check or admission of the study's between.

The module exposes `declared_deferral_from_config(path)`: it loads the file as the platform layer does
(`load_platform`, `load_faults`, so the `[tokens] margin_s` a backend needs is held to as well) and answers
`fault_deferral.heal_deferral_bound`, the figure `FaultGate.max_deferral_s`, `PlatformHealth.max_deferral_s` and
`RunAdmit.declared_deferral_s` answer from the same settings. Each backend's own settings give its own figure: the HPC
backend's relay, exec and stop bounds are in its derived heal bound, and Compose's are not.
"""

import shutil
from pathlib import Path

import pytest
from fault_support import FakeServices, write_faults
from hpc_support import write_hpc_platform
from scheduler_support import build, settings_for
from study_support import EXAMPLE

from expcore.campaign.healthgate import DeferralBound, Health, PlatformDeferral
from expcore.config.loading import ConfigError
from expcore.study.config import load_study
from expcore.study.platform_deferral import PLATFORM_HOOK, platform_deferrals
from expcore_onedata import fault_deferral
from expcore_onedata.admission import run_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_deferral import heal_deferral_bound
from expcore_onedata.fault_settings import DEFAULT_READY_TIMEOUT_S, load_faults
from expcore_onedata.health import PlatformHealth
from expcore_onedata.sandbox import kinds
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer

MODULE = "expcore_onedata.sandbox.kinds"
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
# Every wait Compose and the HPC backend share at the HPC backend's defaults, so the two tables differ in the backend's
# own bounds only (the relay's wait, the exec's step and the stop's own bound).
SHARED = {
    "heal": "all_links",
    "settle_timeout_s": 450,
    "command_timeout_s": 300,
    "ready_timeout_s": DEFAULT_READY_TIMEOUT_S,
    "poll_s": 10,
    "clear_attempts": 3,
    "session_drain_s": 320,
    "admission_timeout_s": 3600,
}
HPC_DEFAULTS = dict.fromkeys(
    (
        "stop_bound_s",
        "settle_timeout_s",
        "relay_wait_s",
        "step_launch_wait_s",
        "command_timeout_s",
        "poll_s",
        "clear_attempts",
    )
)


def compose_platform(root: Path, margin_s: int = 100_000, **changes) -> Path:
    """The journal is stated in full, as it must be in a study directory (a `study.toml` beside the file)."""
    root.mkdir(parents=True, exist_ok=True)
    return write_faults(root, margin_s=margin_s, **{**SHARED, "journal": str(root / "fault-journal"), **changes})


def hpc_platform(root: Path, **changes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    keep = {
        k: v for k, v in SHARED.items() if k in ("heal", "ready_timeout_s", "session_drain_s", "admission_timeout_s")
    }
    return write_hpc_platform(root, **{**HPC_DEFAULTS, **keep, "journal": str(root / "fault-journal"), **changes})


def bound_of(platform: Path) -> DeferralBound | None:
    return heal_deferral_bound(load_faults(platform, load_platform(platform, require_secrets=False)))


def test_o35_the_module_a_study_lists_exposes_the_hook_under_the_cores_name():
    """O35: the core reads the hook by a name it defines, from the module the study lists for its kinds."""
    assert PLATFORM_HOOK == "declared_deferral_from_config"
    assert callable(getattr(kinds, PLATFORM_HOOK))
    assert PLATFORM_HOOK in kinds.__all__


def test_o35_the_bound_names_the_module_a_study_lists_for_the_platform():
    """O35: the module the platform's bounds name is the one the study lists in `[study] platforms` and whose function
    the core calls: the module's own name."""
    assert fault_deferral.PLATFORM_MODULE == kinds.__name__ == MODULE


def test_o35_the_hook_answers_what_the_health_check_and_the_admission_declare_of_the_same_file(tmp_path, fake):
    """O35: one file, three declarations (the hook's, the standard health check's and the run admission's) that agree
    by construction, since all three come from `heal_deferral_bound` over the file's `[faults]`."""
    platform = compose_platform(tmp_path)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    health = PlatformHealth(
        config,
        client,
        TokenIssuer(client, config, clock=lambda: NOW),
        clock=lambda: NOW,
        fault_driver=FakeServices(("store-one", "store-two")),
    )
    hook = getattr(kinds, PLATFORM_HOOK)(platform)
    assert hook == health.max_deferral_s() == run_admission(config).declared_deferral_s() == bound_of(platform)
    assert hook.platform == MODULE  # each names the module a study must list for it to be read through the registry
    settings = load_faults(platform, config)
    assert hook.seconds == settings.session_drain_s + settings.heal_timeout_s + settings.heal_settle_s > 0
    assert hook.rechecks == hook.checks == 2  # a session drain comes first: two rechecks and two checks


def test_o35_each_backend_gets_its_own_figure_from_its_own_file(tmp_path):
    """O35: a Compose platform.toml and an HPC one, with the same waits, declare different figures, each the one its own
    settings derive: the HPC backend's heal round counts the relay's wait, the exec's step and the stop's own bound, so
    its derived `heal_timeout_s`, and the deferral built on it, is longer."""
    compose, hpc = compose_platform(tmp_path / "compose"), hpc_platform(tmp_path / "hpc")
    declared = {name: getattr(kinds, PLATFORM_HOOK)(path) for name, path in (("compose", compose), ("hpc", hpc))}
    assert declared["compose"] == bound_of(compose)
    assert declared["hpc"] == bound_of(hpc)
    assert declared["compose"].seconds < declared["hpc"].seconds
    for name, path in (("compose", compose), ("hpc", hpc)):
        settings = load_faults(path, load_platform(path, require_secrets=False))
        assert settings.backend == name
        assert declared[name].seconds == settings.session_drain_s + settings.heal_timeout_s + settings.heal_settle_s


def test_o58_the_step_launch_wait_is_in_the_deferral_a_study_holds_max_defer_s_to(tmp_path):
    """O58: the launch wait widens the HPC bounds the heal counts (each command, a stop, a link restart), so the
    deferral the platform declares, and `max_defer_s` is held to, counts it: with the heal declared, a table whose steps
    start at once and one that may wait the default 240 s declare deferrals that differ by exactly their
    `heal_timeout_s`, and each is the platform's own sum of the session drain, the heal and the settle after it. The
    launch wait widens timeouts only: the drain and the settle are the same."""
    quick = hpc_platform(tmp_path / "quick", step_launch_wait_s=0)
    waits = hpc_platform(tmp_path / "waits")
    settings = {
        name: load_faults(path, load_platform(path, require_secrets=False))
        for name, path in (("quick", quick), ("waits", waits))
    }
    declared = {name: getattr(kinds, PLATFORM_HOOK)(path) for name, path in (("quick", quick), ("waits", waits))}
    assert (settings["quick"].step_launch_wait_s, settings["waits"].step_launch_wait_s) == (0.0, 240.0)
    assert declared["waits"] == bound_of(waits)
    for name in ("quick", "waits"):
        s = settings[name]
        assert declared[name].seconds == s.session_drain_s + s.heal_timeout_s + s.heal_settle_s
    assert declared["waits"].seconds > declared["quick"].seconds
    assert declared["waits"].seconds - declared["quick"].seconds == (
        settings["waits"].heal_timeout_s - settings["quick"].heal_timeout_s
    )
    assert settings["waits"].session_drain_s == settings["quick"].session_drain_s
    assert settings["waits"].heal_settle_s == settings["quick"].heal_settle_s


def test_o35_a_backends_own_token_margin_is_held_to_through_the_hook(tmp_path):
    """O35: the hook loads the file as the platform layer does, so the least `[tokens] margin_s` of its backend (5,370 s
    on HPC, 2,134 s on Compose) is held to on this path as well: an HPC file with Compose's margin is refused naming the
    margin it needs, and each file with its own backend's margin is read."""
    short = hpc_platform(tmp_path / "short", margin_s=2134)
    with pytest.raises(ConfigError, match=r"margin_s = 2134 is too small.*set: \[tokens\] margin_s = 5370"):
        getattr(kinds, PLATFORM_HOOK)(short)
    enough = hpc_platform(tmp_path / "enough", margin_s=5370)
    assert getattr(kinds, PLATFORM_HOOK)(enough) == bound_of(enough)
    small = compose_platform(tmp_path / "compose", margin_s=2133)
    with pytest.raises(ConfigError, match=r"margin_s = 2133 is too small"):
        getattr(kinds, PLATFORM_HOOK)(small)


def test_o35_with_the_heal_off_or_without_faults_the_platform_declares_none(tmp_path):
    """O35: `heal = "off"` heals nothing, and a platform.toml with no `[faults]` has no fault gate: either declares no
    deferral (None), as the health check and the admission do."""
    off = compose_platform(tmp_path / "off", heal="off")
    (tmp_path / "plain").mkdir()
    plain = write_platform(tmp_path / "plain")
    assert getattr(kinds, PLATFORM_HOOK)(off) is None
    assert getattr(kinds, PLATFORM_HOOK)(plain) is None


def test_o35_a_platform_file_the_layer_cannot_load_is_refused_by_its_own_message(tmp_path):
    """O35: a key `[faults]` does not know is refused by the platform layer's loader, naming the file and the key, as
    when the study's plugin loads the same file."""
    bad = compose_platform(tmp_path, no_such_key=1)
    with pytest.raises(ConfigError, match="no_such_key"):
        getattr(kinds, PLATFORM_HOOK)(bad)


def wrapped(check):
    """A study's own health check around the platform's: a closure with no `max_deferral_s`."""

    def health():
        return check()

    return health


def wrapped_admission(admit):
    """A study's own admission around the platform's: no `declared_deferral_s`."""

    def admission(treatment_kinds, treatment_targets, cancel, exclusive):
        return admit(treatment_kinds, treatment_targets, cancel, exclusive)

    return admission


class Carrying:
    """A wrapper that passes `max_deferral_s` on."""

    def __init__(self, bound):
        self.bound = bound

    def __call__(self):
        return Health(True, "fine")

    def max_deferral_s(self):
        return self.bound


def listing_the_platform(tmp_path: Path, writer=compose_platform, *, listed: bool = True) -> Path:
    """A copy of the example that lists `expcore_onedata.sandbox.kinds` (unless `listed` is false) and holds a
    `platform.toml` that `writer` writes: the study directory the core reads the platform's declaration from."""
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    if listed:
        toml = study / "study.toml"
        line = 'plugin = "plugin.py"\n'
        toml.write_text(
            toml.read_text(encoding="utf-8").replace(line, f'{line}platforms = ["{MODULE}"]\n', 1), encoding="utf-8"
        )
    writer(study)
    return study


def test_o35_a_study_wrapping_both_its_check_and_its_admission_is_refused_and_an_unwrapped_one_starts(tmp_path):
    """O35: a study that lists the platform and holds its platform.toml, and whose plugin wraps the health check and
    the run admission so neither carries the heal's deferral, is refused when the scheduler is built, naming 4,972 s'
    kind of figure, the platform and the missing method; a check that passes the declared bound on starts under a
    `max_defer_s` that covers it, and is refused under one that does not."""
    study = load_study(listing_the_platform(tmp_path))
    declared = platform_deferrals(study)
    assert [d.platform for d in declared] == [MODULE]
    assert isinstance(declared[0], PlatformDeferral)
    bound = declared[0].bound
    assert bound is not None
    assert bound.seconds > 0
    core = settings_for(poll_interval_s=1.0)
    needed = (
        bound.seconds + bound.rechecks * (core.recheck_s + core.poll_interval_s) + bound.checks * core.health_timeout_s
    )
    inner = Carrying(bound)
    with pytest.raises(ConfigError) as refused:
        build(
            tmp_path / "wrapped",
            health=wrapped(inner),
            admission=wrapped_admission(lambda *a: None),
            platform_deferrals=declared,
            settings=settings_for(max_defer_s=needed, poll_interval_s=1.0),
            plan=SMALL,
        )
    text = str(refused.value)
    for part in ("doesn't expose max_deferral_s", f"the platform {MODULE!r}", f"{needed:g} s", 'heal = "all_links"'):
        assert part in text, text
    with build(
        tmp_path / "carried",
        health=inner,
        admission=wrapped_admission(lambda *a: None),
        platform_deferrals=declared,
        settings=settings_for(max_defer_s=needed, poll_interval_s=1.0),
        plan=SMALL,
    ):
        pass
    with pytest.raises(ConfigError, match=f"raise max_defer_s to at least {needed:g}"):
        build(
            tmp_path / "short",
            health=inner,
            platform_deferrals=declared,
            settings=settings_for(max_defer_s=needed - 1, poll_interval_s=1.0),
            plan=SMALL,
        )


def test_o35_a_study_pointing_at_the_other_backends_file_gets_that_backends_figure(tmp_path):
    """O35: the figure the core asks for is that of the file the study points at: a study on the HPC file is held to the
    HPC figure, which a health check built from the Compose file (the wrong platform.toml) does not carry, so the
    campaign is refused naming both, where the Compose figure alone would have started it."""
    study = listing_the_platform(tmp_path / "study", hpc_platform)
    declared = platform_deferrals(load_study(study))
    compose = bound_of(compose_platform(tmp_path / "other"))
    assert declared[0].bound == bound_of(study / "platform.toml")
    assert declared[0].bound.seconds > compose.seconds
    with pytest.raises(ConfigError) as refused:
        build(
            tmp_path / "campaign",
            health=Carrying(compose),
            platform_deferrals=declared,
            settings=settings_for(max_defer_s=100_000.0),
            plan=SMALL,
        )
    assert "declares a deferral of" in str(refused.value)
    assert "shorter than the platform's" in str(refused.value)


def test_o35_a_study_that_uses_the_platform_without_listing_its_module_is_refused_naming_it(tmp_path):
    """O35: the platform's health check and run admission declare the deferral and name the module they are read
    through (`expcore_onedata.sandbox.kinds`); a study whose list leaves that module out (the registry was never asked)
    is refused when the scheduler is built, naming the module, whichever object declares it; listing the module starts
    the campaign."""
    unlisted = load_study(listing_the_platform(tmp_path / "unlisted", listed=False))
    listed = load_study(listing_the_platform(tmp_path / "listed"))
    assert (unlisted.platforms, listed.platforms) == ((), (MODULE,))
    platform = unlisted.root / "platform.toml"
    config = load_platform(platform, require_secrets=False)
    bound = bound_of(platform)
    assert bound is not None
    assert bound.platform == MODULE
    core = settings_for(poll_interval_s=1.0)
    needed = (
        bound.seconds + bound.rechecks * (core.recheck_s + core.poll_interval_s) + bound.checks * core.health_timeout_s
    )
    settings = settings_for(max_defer_s=needed, poll_interval_s=1.0)
    by_health, by_admission = (
        {"health": Carrying(bound)},
        {"health": Carrying(None), "admission": run_admission(config)},
    )
    for name, options in (("health", by_health), ("admission", by_admission)):
        with pytest.raises(ConfigError, match=MODULE) as refused:
            build(tmp_path / name, listed_platforms=unlisted.platforms, settings=settings, plan=SMALL, **options)
        assert "[study] platforms" in str(refused.value)
    with build(
        tmp_path / "listed-campaign",
        platform_deferrals=platform_deferrals(listed),
        listed_platforms=listed.platforms,
        settings=settings,
        plan=SMALL,
        **by_health,
    ):
        pass
