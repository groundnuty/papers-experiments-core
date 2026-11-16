# Provenance: new
"""What each platform a study lists declares of its own longest deferral, read from the platform's own configuration.

A platform whose health check can stay deferred (a heal it runs between runs) declares the longest in its configuration,
and the core holds the campaign's `max_defer_s` to it (C11, O35). The health check and the run admission the study's
plugin passes can both declare it, but both are objects the plugin may wrap, and a wrapper that leaves the method out
hides the declaration: the campaign would then be held to nothing. So the core also asks the platform itself, through
the module `[study] platforms` names (the one `study.config.import_platforms` imports), with no object of the study's
plugin between the platform's settings and the comparison.

A platform module opts in with one function, and a platform without it declares nothing:

    def declared_deferral_from_config(path: Path) -> DeferralBound | None: ...

`path` is the platform configuration the study points at (`StudyConfig.platform_config`: `[study] platform_config`, by
default the `platform.toml` beside study.toml). The function loads the file as the platform loads it for a run, so every
check of the platform's own configuration (a `[tokens] margin_s` too small for its backend, a key it does not know)
holds on this path too, and answers a `DeferralBound` or None. A refusal is a `ConfigError` naming the file and the key,
which reaches the study as the platform worded it.

A study that lists such a platform but points at no configuration is refused, naming the key: the guard would otherwise
be skipped by leaving the pointer out.
"""

import importlib

from expcore.campaign.healthgate import PlatformDeferral, checked_bound
from expcore.config.loading import ConfigError
from expcore.study.config import (
    PLATFORM_CONFIG_KEY,
    PLATFORM_FILE,
    PLATFORMS_KEY,
    STUDY_FILE,
    StudyConfig,
    import_platforms,
)

PLATFORM_HOOK = "declared_deferral_from_config"  # a platform module's optional function, read by name


def platform_deferrals(study: StudyConfig) -> tuple[PlatformDeferral, ...]:
    """What each platform the study lists declares of its deferral, in the order `[study] platforms` lists them: only
    those whose module has `declared_deferral_from_config`, each with the answer (None when its configuration declares
    none). Raise ConfigError when a module cannot be imported, has a hook that is not a function, answers anything but
    None or a `DeferralBound`, fails, or when the study points at no platform configuration for a platform that
    declares from one."""
    found = []
    study_file = study.root / STUDY_FILE
    for name in study.platforms:
        import_platforms(
            study_file, [name]
        )  # the process has imported it already; this names the study's key on failure
        hook = getattr(importlib.import_module(name), PLATFORM_HOOK, None)
        if hook is None:
            continue
        if not callable(hook):
            raise ConfigError(
                f"{study_file}: [study] {PLATFORMS_KEY}: module {name!r} has a {PLATFORM_HOOK} that is not a function"
            )
        if study.platform_config is None:
            raise ConfigError(
                f"{study_file}: [study] {PLATFORMS_KEY}: platform {name!r} declares its longest deferral from its "
                f"configuration, and this study points at none: name the file in [study] {PLATFORM_CONFIG_KEY}, or "
                f"keep it as {PLATFORM_FILE} beside {STUDY_FILE}"
            )
        try:
            answer = hook(study.platform_config)
        except ConfigError:
            raise  # the platform's own words: they name the file and the key
        except Exception as error:  # its text may hold a credential, so the refusal names the type only
            raise ConfigError(
                f"{study_file}: [study] {PLATFORMS_KEY}: platform {name!r} failed reading its configuration in "
                f"{PLATFORM_HOOK} ({type(error).__name__})"
            ) from None
        found.append(PlatformDeferral(name, checked_bound(answer, PLATFORM_HOOK, f"platform {name!r}")))
    return tuple(found)
