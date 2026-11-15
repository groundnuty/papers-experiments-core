# Provenance: new
"""A platform's run admission may declare that it needs a health check, and a campaign without one is refused (O35).

An admission can read what a try's pre-run check noted (`expcore.campaign.prerun`) and refuse a run the check cannot
vouch for. A campaign with no health check has no try note, so such an admission would admit every run on nothing: a
run is let in onto a federation no check has looked at, and the refusal it was written to make never happens. A
platform whose admission depends on the check therefore declares it:

    admission = requiring_health_check(admit, "why the platform needs it, and what to pass")

The result is called as `admit` is and carries the platform's reason in the attribute `requires_health_check`. An
admission of a platform's own may set that attribute itself: the attribute is the contract, the helper is how the
platform's code sets it. The declaration lives on the admission, not on the health check, because it is exactly the
campaign with no check that has no check to ask; and not in the study's plugin, because an author who forgot the check
would equally forget a flag.

`Scheduler.__init__` refuses (`refuse_without_health_check`) a campaign whose admission declares the need and that has
no health check, with a `ConfigError` that says why in the platform's words and that the campaign needs the platform's
health check. It runs before the dispatcher's lock is taken and before the store is opened, so a refused campaign
touches nothing. Without the declaration nothing changes: a campaign with no health check runs as it did.

An admission that wraps another one in a function of its own must keep the attribute (`functools.wraps` does), or the
campaign is not refused: the declaration travels with the admission that is passed to the campaign.
"""

from dataclasses import dataclass
from typing import Any

from expcore.campaign.attempt import RunAdmission
from expcore.campaign.healthgate import HealthCheck
from expcore.config.loading import ConfigError

ATTRIBUTE = "requires_health_check"


@dataclass(frozen=True)
class _Requiring:
    """A run admission and the reason its platform needs a health check."""

    admit: RunAdmission
    requires_health_check: str

    def __call__(self, *args: Any) -> Any:
        return self.admit(*args)


def requiring_health_check(admit: RunAdmission, reason: str) -> RunAdmission:
    """`admit`, declaring that its platform needs a health check for `reason` (what a study's author is shown).

    A reason that is not a non-empty text is the platform's own bug and raises `ValueError` here, where it is made.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("the reason a platform's admission needs a health check must be a non-empty text")
    return _Requiring(admit, reason.strip())


def required_reason(admission: RunAdmission | None) -> str | None:
    """Why the admission's platform needs a health check, or None when it declares no need.

    A declaration that is not a non-empty text is refused with a `ConfigError` naming the attribute: it is never read
    as no need, or an admission could lose its protection by setting it wrongly.
    """
    declared = getattr(admission, ATTRIBUTE, None)
    if declared is None:
        return None
    if not isinstance(declared, str) or not declared.strip():
        raise ConfigError(
            f"the run admission's {ATTRIBUTE} must be the reason its platform needs a health check, a non-empty text, "
            f"not {declared!r}; leave it out for a platform that needs none"
        )
    return declared.strip()


def refuse_without_health_check(admission: RunAdmission | None, health: HealthCheck | None) -> None:
    """Refuse a campaign whose admission declares the need for a health check and that has none (`ConfigError`).

    Call it before anything is opened. A campaign with a health check, or one whose admission declares no need, passes.
    """
    reason = required_reason(admission)
    if reason is None or health is not None:
        return
    raise ConfigError(
        f"the platform's run admission requires a health check, and this campaign has none: {reason}. Give the "
        "campaign the platform's health check (the study plugin's `health`, or `Scheduler(health=...)`); without "
        "one, runs would be admitted with no check behind them"
    )
