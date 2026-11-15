# Provenance: new
"""How the Markdown report (`expcore.report`) and the terminal summary show a smallest effect of interest (S16).

A section "Smallest effects of interest" follows the contrast tables, with a row for each contrast that declares one:
the smallest effect as declared, the direction of the claim ("either" when none is declared), the estimate with its
sign written and its interval, the decision under the study's rule, where the interval lies against the smallest
effect (it includes it, or excludes it beyond or short of it), the minimum detectable effect and whether the design
detects the smallest effect. A ratio contrast's smallest ratio is shown with its natural log, and its estimate,
interval and minimum detectable effect, natural-log ratios, each with its ratio beside it. A two-sided rule's
`meets_either_direction` reads "meets (either direction)", so no cell says a bare "meets" that hides which way the
difference went, and an exploratory contrast's decision is labelled exploratory as its conclusion is.

A report in which no contrast declares a smallest effect has no such section and no such text in its summary, so it
keeps its bytes.
"""

import math
from collections.abc import Callable, Sequence

from expcore.report_notes import SMALLEST_NOTES, UNKNOWN, percent
from expcore.stats.analysis import ContrastResult

HEADING = "## Smallest effects of interest"
NOT_COMPUTED = "-"  # no minimum detectable effect was asked for (no power settings), or the contrast is untestable
WORDS = {"meets_either_direction": "meets (either direction)"}  # a decision whose shown words differ from its name
RELATIONS = {
    "includes": "includes it",
    "beyond": "excludes it: beyond it",
    "short_of": "excludes it: short of it",
}

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]


def _num(value: float | None) -> str:
    """A number to four significant figures, as the report's other tables show it; n/a when missing or non-finite."""
    return UNKNOWN if value is None or not math.isfinite(value) else f"{value:.4g}"


def _signed(value: float) -> str:
    """A number with its sign always written, so the direction of an estimate is never left to be inferred."""
    return UNKNOWN if not math.isfinite(value) else f"{value:+.4g}"


def _ratio(value: float) -> str:
    """exp of a natural-log ratio, for display only; past the float range it is infinite and reads n/a."""
    try:
        return _num(math.exp(value))
    except OverflowError:
        return UNKNOWN


def _declared(result: ContrastResult) -> str:
    """The smallest effect as declared; a ratio with its natural log, the scale it is compared on, in brackets."""
    rule = result.smallest_effect
    if rule.form == "ratio":
        return f"ratio {_num(rule.declared)} (ln {_num(math.log(rule.declared))})"
    return _num(rule.declared)


def _decision(result: ContrastResult) -> str:
    word = WORDS.get(result.smallest_effect.decision, result.smallest_effect.decision)
    return f"{word} (exploratory)" if result.exploratory else word


def _estimate(result: ContrastResult) -> str:
    shown = _signed(result.estimate)
    return f"{shown} (ratio {_ratio(result.estimate)})" if result.ratio is not None else shown


def _interval(result: ContrastResult) -> str:
    shown = f"[{_num(result.ci_low)}, {_num(result.ci_high)}]"
    if result.ratio is None:
        return shown
    return f"{shown} (ratio [{_ratio(result.ci_low)}, {_ratio(result.ci_high)}])"


def _mde(result: ContrastResult) -> str:
    mde = result.minimum_detectable_effect
    if mde is None:
        return NOT_COMPUTED
    if result.ratio is None or not math.isfinite(mde):
        return _num(mde)
    return f"{_num(mde)} (ratio {_num(result.smallest_effect.minimum_detectable_ratio)})"


def _detects(result: ContrastResult) -> str:
    verdict = result.smallest_effect.detectable
    if verdict is None:
        return NOT_COMPUTED if result.minimum_detectable_effect is None else UNKNOWN
    return "yes" if verdict else "no"


def section(results: Sequence[ContrastResult], level: float | None, table: Table) -> list[str]:
    """The report's section of smallest effects of interest, ending in a blank line; empty when no contrast declares
    one, so the report is what it was without it."""
    declared = [r for r in results if r.smallest_effect is not None]
    if not declared:
        return []
    interval = "Interval" if level is None else f"{percent(level)} interval"
    header = ["Contrast", "Smallest effect", "Direction", "Estimate", interval, "Decision", "Interval against it"]
    header += ["Min. detectable effect", "Detects it"]
    rows = [
        [
            r.name,
            _declared(r),
            r.smallest_effect.direction,
            _estimate(r),
            _interval(r),
            _decision(r),
            RELATIONS.get(r.smallest_effect.interval, UNKNOWN),
            _mde(r),
            _detects(r),
        ]
        for r in declared
    ]
    return [HEADING, "", *table(header, rows), "", *SMALLEST_NOTES, ""]


def summary_suffix(result: ContrastResult) -> str:
    """What a contrast's terminal line adds: its smallest effect, the decision and the signed estimate; nothing for
    a contrast that declares none."""
    if result.smallest_effect is None:
        return ""
    rule = result.smallest_effect
    return (
        f"; smallest effect {_declared(result)} ({rule.direction}): {_decision(result)}, estimate {_estimate(result)}"
    )
