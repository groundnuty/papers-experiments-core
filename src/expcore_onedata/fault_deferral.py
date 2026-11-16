# Provenance: new
"""The longest the platform's health check may stay deferred, from `[faults]` alone (O35).

With a declared heal the fault gate defers every check while it heals, and after an outage run a session drain comes
first. The core holds the campaign's `max_defer_s` to that figure and refuses a shorter one at its start, but it must
learn the figure twice, from two objects that do not depend on each other. The health check declares it
(`FaultGate.max_deferral_s`, `PlatformHealth.max_deferral_s`), and so does the platform's run admission
(`admission.run_admission`, `declared_deferral_s`), which reads it from the same settings with no health object
between. A study's own check around the platform's that leaves `max_deferral_s` out, or answers less, then disagrees
with the admission and the scheduler refuses the campaign (`SchedulerConfig.check_declared_deferral`), where it would
otherwise hold `max_defer_s` to nothing. Both declarations are built here, so they agree by construction.

A study's plugin can wrap the admission too, so the platform declares the bound a third time, through the module the
study lists in `[study] platforms` (`sandbox.kinds.declared_deferral_from_config`, read by the core, with no object of
the study's plugin between): `declared_from_file` loads the study's platform.toml as the platform layer does and answers
the same figure from its `[faults]`. Loading it holds the file to every check the platform makes of its own
configuration, the least `[tokens] margin_s` of its backend included.
"""

import os

from expcore.campaign.healthgate import DeferralBound
from expcore.config.loading import load_toml
from expcore_onedata.config import PLATFORM_MODULE, load_platform  # the module a study lists for this platform
from expcore_onedata.fault_settings import FaultSettings, heal_deferral_s, load_faults


def declared_from_file(path: str | os.PathLike[str]) -> DeferralBound | None:
    """The longest the platform's health check may stay deferred under the `[faults]` of the platform.toml at `path`
    (`heal_deferral_bound`); None when the file has no `[faults]`, or the heal is off. The file is loaded as a run loads
    it, with no secret read, so a refusal names the file and the key as the platform layer words it (a `[tokens]
    margin_s` below its backend's least, say)."""
    config = load_platform(path, require_secrets=False)
    if "faults" not in load_toml(config.file):
        return None
    return heal_deferral_bound(load_faults(config.file, config))


def heal_deferral_bound(settings: FaultSettings) -> DeferralBound | None:
    """The longest the platform's checks may stay deferred under `settings`, for the core to hold `max_defer_s` to;
    None with the heal off (`FaultGate.max_deferral_s` has the figure's terms, and what it assumes and leaves out).

    The platform's own seconds are the session drain and the heal's bound (`heal_deferral_s`); the waits between and
    around them are the core's settings, which it adds (`DeferralBound.rechecks`, `checks`): two rechecks and two checks
    with a drain, and one of each with no drain, where the first deferral is the check that starts the heal."""
    if settings.heal == "off":
        return None
    drained = settings.session_drain_s > 0
    first = (
        f"session_drain_s ({settings.session_drain_s:g} s) after an outage run, then one wait between two checks "
        "(the core's recheck_s and poll_interval_s) and the check that starts the heal (its health_timeout_s), "
        "then "
        if drained
        else "the check that starts the heal, then "
    )
    detail = (
        f'[faults] heal = "{settings.heal}": {first}heal_timeout_s ({settings.heal_timeout_s:g} s) and '
        f"heal_settle_s ({settings.heal_settle_s:g} s), then another wait between two checks and the check that "
        "makes the link check afresh after the heal (its health_timeout_s), each check assumed to end within its "
        "health_timeout_s and the heal within its own bound, and no deferral begun earlier or for another reason, "
        "nor a wait for another holder of the federation, nor the recovery of a failed heal's container restart, "
        "counted; lower heal_timeout_s or session_drain_s, or set "
        'heal = "off", to shorten it'
    )
    return DeferralBound(
        heal_deferral_s(settings),
        detail,
        rechecks=2 if drained else 1,
        checks=2 if drained else 1,
        platform=PLATFORM_MODULE,
    )
