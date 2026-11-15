# Provenance: new
"""The scheduler's settings: caps, attempt limits, pauses, thresholds, and the kinds it blames failures on."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from expcore.campaign.factors import FactorSchema
from expcore.campaign.gate import Budget
from expcore.campaign.harness_stop import HarnessStopConfig
from expcore.campaign.healthgate import DeferralBound
from expcore.config.loading import ConfigError
from expcore.errors import infra_kinds, untreatable_kinds


@dataclass(frozen=True)
class SchedulerConfig:
    """What `campaign.toml` holds for the dispatcher, as values.

    - `max_attempts`: charged attempts a run may use (C03).
    - `workers`: runs in flight at once on this host, quarantined leases counted among them.
    - `unit_caps`: runs in flight at once per unit (a key the platform names); a unit not listed has no cap of its own.
    - `retry_delay_s`: how long a run waits after an attempt that must be run again.
    - `unit_fail_threshold`, `unit_pause_s`: failures in a row that pause a unit, and for how long (C15).
    - `health_kind`: the infrastructure kind a failed health check is charged as (C11); `identity_kind`: the kind of a
      run whose endpoint changed its served identity (C16). Neither may be a kind any condition declares as its
      treatment, because a run that never started, or that the wrong endpoint served, must not be scored as its fault.
    - `recheck_s`, `health_timeout_s`: how often the closed gate is checked (and how long a run whose check was
      deferred waits before it is tried again), and how long a check may take. `max_defer_s`: how long the platform's
      check may stay deferred (a run holds the platform on purpose) before the gate fails and blocks dispatch (C11); at
      least the longest deferral the platform's check declares, or the campaign is refused (`check_deferral_bound`).
    - `lease_timeout_s`: how long a run waits for a lease before it is left unstarted; None: for ever.
    - `harness_stop`, `budget`: the harness-error stops (C10) and the budget gate (C12); no budget runs without one.
    - `exit_when_blocked`: `run` returns, instead of waiting, when nothing is in flight and every pending run is
      held by a hold that will not end by itself (a stop, a budget, a model not admitted, a deferral past its bound).
    - `allow_growth`: the plan may add whole tasks, models, seeds or conditions to a store that already holds a run.
    - `refuse_unpriced`: with a budget, refuse to open the campaign when a planned model has no price and is not free
      (C12). Without it such a model is held at the budget gate instead.
    """

    max_attempts: int = 3
    workers: int = 1
    unit_caps: Mapping[str, int] = field(default_factory=dict)
    retry_delay_s: float = 60.0
    poll_interval_s: float = 1.0
    unit_fail_threshold: int = 3
    unit_pause_s: float = 300.0
    health_kind: str = "sandbox"
    identity_kind: str = "endpoint"
    recheck_s: float = 30.0
    health_timeout_s: float = 60.0
    max_defer_s: float = 3600.0
    lease_timeout_s: float | None = 600.0
    harness_stop: HarnessStopConfig = field(default_factory=HarnessStopConfig)
    budget: Budget | None = None
    exit_when_blocked: bool = False
    allow_growth: bool = False
    refuse_unpriced: bool = True

    def __post_init__(self) -> None:
        """Refuse limits that cannot work."""
        problems = []
        if self.max_attempts < 1 or self.workers < 1 or self.unit_fail_threshold < 1:
            problems.append("max_attempts, workers and unit_fail_threshold must be at least 1")
        if any(cap < 1 for cap in self.unit_caps.values()):
            problems.append("a unit cap must be at least 1")
        if min(self.unit_pause_s, self.recheck_s, self.health_timeout_s, self.poll_interval_s, self.max_defer_s) <= 0:
            problems.append(
                "unit_pause_s, recheck_s, health_timeout_s, poll_interval_s and max_defer_s must be positive"
            )
        if self.retry_delay_s < 0 or (self.lease_timeout_s is not None and self.lease_timeout_s <= 0):
            problems.append("retry_delay_s must not be negative and lease_timeout_s must be positive")
        if problems:
            raise ValueError("; ".join(problems))

    @property
    def kind_roles(self) -> dict[str, str]:
        """The kinds this campaign charges its own failures to, by the key of `[scheduler]` that names each."""
        return {"health_kind": self.health_kind, "identity_kind": self.identity_kind}

    def deferral_needed(self, bound: DeferralBound) -> tuple[float, str]:
        """The longest a platform's check may stay deferred, this campaign's own waits included (the `max_defer_s` it
        needs), and in words the core's waits the platform's declared (`bound.rechecks` waits between two checks of
        `recheck_s` and up to `poll_interval_s` each, `bound.checks` checks of up to `health_timeout_s` each); empty
        when it declared none."""
        between = bound.rechecks * (self.recheck_s + self.poll_interval_s)
        checking = bound.checks * self.health_timeout_s
        parts = []
        if bound.rechecks:
            parts.append(
                f"{bound.rechecks} x (recheck_s {self.recheck_s:g} s + poll_interval_s {self.poll_interval_s:g} s)"
            )
        if bound.checks:
            parts.append(f"{bound.checks} x health_timeout_s {self.health_timeout_s:g} s")
        return bound.seconds + between + checking, " and ".join(parts)

    def _deferral_words(self, bound: DeferralBound) -> tuple[float, str]:
        """The `max_defer_s` a bound needs, and in words its parts: the platform's own seconds and detail, and the
        core's waits it declared with their values (`deferral_needed`)."""
        needed, core = self.deferral_needed(bound)
        if not core:
            return needed, bound.detail
        return needed, f"{bound.seconds:g} s of the platform's own: {bound.detail}; plus the core's waits {core}"

    def check_deferral_bound(self, bound: DeferralBound | None) -> None:
        """Refuse a `max_defer_s` shorter than the longest the platform's check may stay deferred (`bound`, from
        `healthgate.deferral_bound`; None: the platform declares none), the core's own waits it declared included
        (`deferral_needed`). Past `max_defer_s` a deferral closes the gate blocking, so a campaign that exits when
        blocked would stop on a wait the platform ends by itself. The gate fails a deferral only once its age is past
        `max_defer_s`, so a bound equal to it starts."""
        if bound is None:
            return
        needed, words = self._deferral_words(bound)
        if needed <= self.max_defer_s:
            return
        raise ConfigError(
            f"campaign.toml [scheduler] max_defer_s is {self.max_defer_s:g} s, but the platform's health check may "
            f"stay deferred for up to {needed:g} s ({words}); a deferral past max_defer_s closes the "
            "health gate as blocking, so a campaign that exits when blocked would stop on a wait it should have waited "
            f"out: raise max_defer_s to at least {needed:g}, or shorten the platform's wait"
            + (", or recheck_s or health_timeout_s" if bound.rechecks or bound.checks else "")
        )

    def check_declared_deferral(
        self, declared: DeferralBound | None, reported: DeferralBound | None, *, exposed: bool
    ) -> None:
        """Refuse a health check that does not carry what the platform declares: `declared` is the platform's own
        declaration of its longest deferral (the run admission's `declared_deferral_s`, read from its settings, with no
        health object between), `reported` what the health check passed to the scheduler declares (`max_deferral_s`),
        and `exposed` whether it has that method at all. A study's own check around the platform's that leaves the
        method out would hold `max_defer_s` to nothing, and one that declares less would hold it to less: both are
        refused, naming the platform's figure, what the check declares and the fix, compared as the `max_defer_s` each
        needs under this campaign's own waits (`deferral_needed`). A check that declares more, or the same, agrees.
        Nothing is asked of a platform that declares none."""
        if declared is None:
            return
        platform, platform_words = self._deferral_words(declared)
        if platform <= 0:
            return
        theirs = 0.0 if reported is None else self.deferral_needed(reported)[0]
        if exposed and theirs >= platform:
            return
        if not exposed:
            reason = "the health check passed to the scheduler doesn't expose max_deferral_s"
        elif reported is None:
            reason = "the health check passed to the scheduler answers max_deferral_s() with None"
        else:
            reason = (
                f"the health check passed to the scheduler declares a deferral of {theirs:g} s through max_deferral_s "
                f"({self._deferral_words(reported)[1]}), shorter than the platform's"
            )
        raise ConfigError(
            f"{reason}, but the platform declares a deferral of {platform:g} s ({platform_words}); pass the method "
            "on: a study's own check around the platform's must have a max_deferral_s() that returns the wrapped "
            f"check's, or campaign.toml [scheduler] max_defer_s ({self.max_defer_s:g} s) is not held to what the "
            "platform declares, and a deferral past it closes the health gate as blocking"
        )

    def check_kinds(self, schema: FactorSchema) -> None:
        """Refuse a health or identity kind that is unknown or that a condition declares as its treatment.

        The kinds a campaign charges its own failures to are untreatable for that campaign, as the registry's
        untreatable kinds are for every one (`expcore.errors.untreatable_kinds`): a failed health check or a changed
        endpoint would otherwise be scored as the condition's fault. A study's load refuses an unknown kind first
        (`study.config.load_study`), naming campaign.toml and the key; this is the check for a scheduler built in code.
        """
        roles = self.kind_roles
        for name, kind in roles.items():
            if kind not in infra_kinds():
                raise ValueError(
                    f"{name} {kind!r} is not an infrastructure kind; known kinds: {', '.join(infra_kinds())}"
                )
        untreatable = {
            **untreatable_kinds(),
            **{kind: f"it is this campaign's {name}" for name, kind in roles.items()},
        }
        refused = schema.refused_treatment(untreatable)
        if refused is not None:
            condition, kind = refused
            name = next((n for n, k in roles.items() if k == kind), None)
            if name is None:
                raise ValueError(
                    f"{kind!r} is the treatment of condition {condition!r} but can never be one: {untreatable[kind]}"
                )
            raise ValueError(
                f"{name} {kind!r} is the treatment of condition {condition!r}: a failed health check or a "
                "changed endpoint would be scored as that condition's fault; name another kind"
            )
