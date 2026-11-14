# Provenance: new
"""A preregistered smallest effect of interest, and what a contrast's result says against it (S16, #81).

A contrast declares at most one, in `[contrasts.<name>]`, in the form its estimator reads:
- a contrast of one outcome, `smallest_effect = d`: a difference d > 0 on the outcome's scale, the scale of its
  estimate;
- a ratio contrast, `smallest_ratio = r`: a ratio r > 0 other than 1, as written (2 is a factor of two). Its estimate,
  interval and minimum detectable effect are natural-log ratios, so r is compared as ln r, and as a size: r and 1 / r
  are one threshold, |ln r|. A ratio below 1 names a decrease, so it is refused beside `direction = "higher"`;
and, beside either, an optional `direction`: "higher" when the claim is that arm A's outcome is above arm B's (a
positive estimate A - B, or A's ratio above B's), "lower" when it is below. Without one the rule holds in either
direction ("either"). `Rule.threshold` is the one place a declared ratio becomes the log scale: every comparison
below takes a threshold already on the estimate's scale, and `exp` serves only to show a log ratio as a ratio.

The decision follows the study's rule, that a difference counts when it is significant (Holm's decision for a
member of the family, the raw p-value at alpha for an exploratory contrast) and its point estimate is at least the
threshold; with a direction, only an estimate of the claimed sign counts. It is one of `DECISIONS`:
- "meets": a direction is declared, the test is significant, and the estimate is on that side at or beyond the
  threshold;
- "meets_either_direction": no direction is declared, the test is significant and |estimate| is at or beyond the
  threshold; the estimate's sign says which way, and every output shows it;
- "opposite_direction": a direction is declared, the test is significant, and the estimate is on the other side of
  zero, whatever its size: a claim that A raises an outcome is never confirmed by a significant drop;
- "falls_short": significant, with the estimate short of the threshold on its side (zero included);
- "not_significant": not significant, whatever the estimate;
- "untestable": the contrast used no pair (S11), so there is nothing to judge.

Beside it, the contrast's reported interval is set against the threshold (`INTERVAL_RELATIONS`): "beyond" when every
value of it is at or beyond the threshold on the direction's side (either side for "either"), "short_of" when every
value is short of it, so the interval excludes the smallest effect either way, and "includes" otherwise; None when the
interval is undefined. And the design is said to detect the smallest effect when the contrast's minimum detectable
effect, a size on the same scale, is at most the threshold (`detectable`); unknown (None) without power settings or
where the minimum detectable effect cannot be found, never False on that account.

Every comparison is on the kernel's snapping grid (`contrasts.snap`), so float noise cannot split a tie (0.6 - 0.5
meets a threshold of 0.1) or give zero a sign. An equivalence margin is another declaration: it bounds the differences
the study calls negligible and decides `equivalent` and `trivial_difference`; the smallest effect is what a difference
must reach to count. Neither is derived from the other, and a contrast may declare both.
"""

from __future__ import annotations

import dataclasses
import math
import numbers

from expcore.stats.contrasts import snap

DIRECTIONS = ("higher", "lower")  # what a preregistration may declare
EITHER = "either"  # the direction of a rule that declares none: two-sided
RULE_DIRECTIONS = (*DIRECTIONS, EITHER)
FORMS = ("difference", "ratio")
KEYS = ("smallest_effect", "smallest_ratio", "direction")  # a [contrasts.<name>] table's keys of a rule
DECISIONS = (
    "meets",
    "meets_either_direction",
    "opposite_direction",
    "falls_short",
    "not_significant",
    "untestable",
)
INTERVAL_RELATIONS = ("beyond", "includes", "short_of")


class RuleError(ValueError):
    """A smallest effect declared in the wrong form or with a wrong value; `key` names the key to fix."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key} {message}")
        self.key = key
        self.message = message


@dataclasses.dataclass(frozen=True)
class Rule:
    """A contrast's preregistered smallest effect: its form, its value as declared and the claimed direction."""

    form: str  # "difference" (smallest_effect) or "ratio" (smallest_ratio)
    declared: float  # as preregistered: a difference, or a ratio
    direction: str  # "higher", "lower", or "either" when none is declared

    def __post_init__(self) -> None:
        """Require a known form and direction, a positive finite difference, or a positive finite ratio other than 1,
        and no decrease (a ratio below 1) declared as "higher"."""
        if self.form not in FORMS:
            raise ValueError(f"a smallest effect's form must be one of {FORMS}, got {self.form!r}")
        key = "smallest_ratio" if self.form == "ratio" else "smallest_effect"
        value = self.declared
        if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
            raise RuleError(key, f"must be a finite number, got {value!r}")
        object.__setattr__(self, "declared", float(value))
        if self.direction not in RULE_DIRECTIONS:
            raise RuleError(
                "direction",
                f"must be 'higher' or 'lower', got {self.direction!r}; leave it out for a rule that holds in either "
                "direction",
            )
        if self.form == "difference" and value <= 0:
            raise RuleError(
                key,
                f'must be positive, got {value}: it is a size, and direction = "lower" declares a claim that A is '
                "below B",
            )
        if self.form == "ratio":
            self._check_ratio(value)

    def _check_ratio(self, value: float) -> None:
        if value <= 0:
            raise RuleError(
                "smallest_ratio",
                f"must be a positive ratio, got {value}: write 2 for a factor of two, not its logarithm",
            )
        if value == 1:
            raise RuleError(
                "smallest_ratio",
                "is 1, a factor of one, which is no effect at all: write the smallest factor that matters, 2 for a "
                "factor of two",
            )
        if value < 1 and self.direction == "higher":
            raise RuleError(
                "smallest_ratio",
                f'is {value:g}, below 1, a decrease, beside direction = "higher": write the increase as a ratio '
                f'above 1 ({1 / value:g}), or declare direction = "lower"',
            )

    @property
    def threshold(self) -> float:
        """The smallest effect on the scale of the contrast's estimate: the difference itself, or |ln ratio|, the size
        of a natural-log ratio, for a ratio contrast. The only place a declared ratio is taken to the log scale."""
        return abs(math.log(self.declared)) if self.form == "ratio" else self.declared


def rule(
    *,
    smallest_effect: object = None,
    smallest_ratio: object = None,
    direction: object = None,
    ratio: bool,
) -> Rule | None:
    """The rule a contrast declares, or None when it declares none; `ratio` says the contrast is a ratio contrast.

    Raises RuleError naming the key for the key of the other form, a direction with no threshold, and any value
    `Rule` refuses.
    """
    if ratio and smallest_effect is not None:
        raise RuleError(
            "smallest_effect",
            "belongs to a contrast of one outcome: a ratio contrast's numbers are natural-log ratios, so it declares "
            "smallest_ratio, a ratio such as 2 for a factor of two",
        )
    if not ratio and smallest_ratio is not None:
        raise RuleError(
            "smallest_ratio",
            "belongs to a ratio contrast, and this contrast's estimator reads one outcome: declare smallest_effect, a "
            "difference on the outcome's scale",
        )
    declared = smallest_ratio if ratio else smallest_effect
    if declared is None:
        if direction is not None:
            raise RuleError(
                "direction",
                "is given without smallest_effect or smallest_ratio: it says which sign of the estimate a smallest "
                "effect of interest is about, so declare the threshold beside it or leave it out",
            )
        return None
    if direction is not None and direction not in DIRECTIONS:  # "either" is the rule's word for none, never declared
        raise RuleError(
            "direction",
            f"must be 'higher' or 'lower', got {direction!r}; leave it out for a rule that holds in either direction",
        )
    form = "ratio" if ratio else "difference"
    return Rule(form, declared, EITHER if direction is None else direction)  # Rule checks each value's type


def _snapped(value: float) -> float:
    return float(snap(value))


def decide(significant: bool, estimate: float, threshold: float, direction: str, untestable: bool = False) -> str:
    """The decision under the rule: untestable first, then significance, then the sign against a declared direction,
    then the estimate against the threshold, on the snapping grid. `threshold` is on the estimate's scale."""
    if direction not in RULE_DIRECTIONS:
        raise ValueError(f"direction must be one of {RULE_DIRECTIONS}, got {direction!r}")
    if untestable:
        return "untestable"
    if not significant:
        return "not_significant"
    if not math.isfinite(estimate):
        return "falls_short"
    value, bound = _snapped(estimate), _snapped(threshold)
    if direction == "higher":
        return "opposite_direction" if value < 0 else ("meets" if value >= bound else "falls_short")
    if direction == "lower":
        return "opposite_direction" if value > 0 else ("meets" if value <= -bound else "falls_short")
    return "meets_either_direction" if abs(value) >= bound else "falls_short"


def interval_relation(low: float, high: float, threshold: float, direction: str) -> str | None:
    """Where an interval lies against the threshold on the direction's side: "beyond" (every value at or beyond it),
    "short_of" (every value short of it), "includes"; None for an undefined interval. On the snapping grid."""
    if direction not in RULE_DIRECTIONS:
        raise ValueError(f"direction must be one of {RULE_DIRECTIONS}, got {direction!r}")
    if not (math.isfinite(low) and math.isfinite(high)):
        return None
    lo, hi, bound = _snapped(low), _snapped(high), _snapped(threshold)
    if direction == "higher":
        beyond, short = lo >= bound, hi < bound
    elif direction == "lower":
        beyond, short = hi <= -bound, lo > -bound
    else:
        beyond, short = lo >= bound or hi <= -bound, -bound < lo and hi < bound
    return "beyond" if beyond else ("short_of" if short else "includes")


def detectable(minimum_detectable_effect: float | None, threshold: float) -> bool | None:
    """Whether the design detects the smallest effect: its minimum detectable effect, a size on the threshold's scale,
    is at most the threshold. None, never False, when there is no figure (no power settings) or it is NaN."""
    mde = minimum_detectable_effect
    if mde is None or not math.isfinite(mde):
        return None
    return _snapped(mde) <= _snapped(threshold)


@dataclasses.dataclass(frozen=True)
class SmallestEffect:
    """A contrast's smallest effect of interest and what its result says against it, as `analysis.json` carries it."""

    form: str  # "difference" or "ratio"
    declared: float  # as preregistered: the difference, or the ratio
    threshold: float  # on the estimate's scale: the difference, or |ln ratio|
    direction: str  # "higher", "lower", or "either" when none is declared
    decision: str  # one of DECISIONS
    interval: str | None  # one of INTERVAL_RELATIONS, None when the interval is undefined
    detectable: bool | None  # the minimum detectable effect is at most the threshold; None when it is unknown
    minimum_detectable_ratio: float | None  # a ratio contrast's: exp of its minimum detectable effect; else None


def judge(
    declared: Rule,
    *,
    estimate: float,
    interval: tuple[float, float],
    significant: bool,
    untestable: bool,
    minimum_detectable_effect: float | None,
) -> SmallestEffect:
    """A contrast's result against its rule: every number given is on the scale of its estimate."""
    threshold = declared.threshold
    mde = None if untestable else minimum_detectable_effect
    ratio = None
    if declared.form == "ratio" and mde is not None and math.isfinite(mde):
        try:
            ratio = math.exp(mde)
        except OverflowError:  # a ratio past the float range: infinite, which analysis.json writes as null
            ratio = math.inf
    return SmallestEffect(
        form=declared.form,
        declared=declared.declared,
        threshold=threshold,
        direction=declared.direction,
        decision=decide(significant, estimate, threshold, declared.direction, untestable),
        interval=None if untestable else interval_relation(*interval, threshold, declared.direction),
        detectable=detectable(mde, threshold),
        minimum_detectable_ratio=ratio,
    )
