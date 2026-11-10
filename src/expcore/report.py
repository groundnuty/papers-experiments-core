# Provenance: new
"""The Markdown report of a preregistered analysis: what was found, under which preregistration, from which code.

`render_report` is a pure function of the analysis result and the campaign export, so two renderings of the same inputs
are the same text: no clock, no path and no random number enters it.

- Contrasts in the Holm family are in one table, contrasts outside it in a table of their own under the heading
  "Exploratory contrasts": nothing controls their multiplicity. Which is which comes from the statistics kernel's own
  flag (`ContrastResult.exploratory`), never from a guess here, and a result whose flag disagrees with its place in the
  Holm decisions is refused, so no table or summary line can show an exploratory contrast as confirmatory.
- A conclusion is never shown without its significance. Each row carries the raw p-value and the Holm-adjusted one
  (or the word "exploratory") beside the conclusion, and the preregistered equivalence margin with the interval at
  1 - 2 alpha that `equivalent` and `trivial_difference` rest on, and the conclusion words already encode the decision:
  `difference`, `trivial_difference`, `equivalent`, `not_detected`, `untestable`. Failing to detect a difference is
  not a finding of none, and the report says so under the table. An `untestable` contrast used no pair: its row gives
  the causes of its missing pairs with their counts in place of a number of pairs, and a result whose word disagrees
  with its pairs is refused (S11).
- Each contrast's row gives the pairs it used and how its treatment attempts entered it: declared "scored", declared
  "excluded" with the pairs that left out, or the pairs lost to a treatment attempt scored None. An excluded outage
  must not read as the absence of an outage effect (S10). A table of its own gives every contrast's missing pairs by
  cause, zeros included (C23).
- Per condition it shows the planned runs and how each ended (analysed, exhausted, unfinished, left out), the attempts
  set aside and a median dispatch position, so a reader can see whether one condition's runs came late; and per
  condition and infrastructure kind the infrastructure failures, with a row of zeros for a condition that had none, so
  0 is never mistaken for "not reported" (C23).
- The report says under "Provenance" whether the analysis is partial, and lists the attempts left out (run key,
  attempt and reason) in a section of their own. What each number counts is stated in the report itself, beside the
  table that shows it.
- The fixed prose beside the tables is in `expcore.report_notes`.
- The header names the preregistration hash (with `+override` when the copy was marked as a test copy), the code stamp,
  the core version and the version of each scorer, so a report says which scorers made the numbers in it (R13).
"""

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.accounting import condition_accounting
from expcore.report_notes import (
    CONDITION_NOTES,
    COUNT_DEFINITIONS,
    MDE_NOTES,
    MISSING_PAIRS,
    NO_KIND,
    contrast_notes,
    equivalence_header,
    percent,
)
from expcore.stats.analysis import CONCLUSIONS, UNTESTABLE, AnalysisResult, ContrastResult
from expcore.stats.pairing import MISSING_CAUSES

EXPLORATORY = "exploratory"
UNKNOWN = "n/a"
NO_MARGIN = "-"  # the margin and the equivalence interval of a contrast that preregistered no margin
CAUSE_HEADINGS = {
    "treatment_outcome_none": "Treatment outcome None",
    "treatment_excluded": "Treatment excluded",
    "exhausted": "Exhausted run",
    "unpaired_or_left_out": "Unpaired or left out",
    "outcome_none": "Outcome None (other)",
}


def _cell(text: object) -> str:
    """Make a value safe inside a table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _num(value: float | None) -> str:
    """A number to four significant figures; a missing or non-finite one is n/a (never zero)."""
    if value is None or not math.isfinite(value):
        return UNKNOWN
    return f"{value:.4g}"


def _p(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return UNKNOWN
    return "<0.0001" if value < 0.0001 else f"{value:.4f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _exploratory(result: ContrastResult, adjusted: Mapping[str, float]) -> bool:
    """Return the kernel's exploratory flag of a contrast; refuse one that disagrees with the Holm decisions.

    A contrast is confirmatory only as a member of the Holm family: flagged not exploratory, in the family and given a
    Holm decision. Anything else in between is an inconsistent result, and showing it either way would mislabel it.
    """
    if result.exploratory == result.in_family or result.exploratory == (result.name in adjusted):
        raise ValueError(
            f"contrast {result.name!r}: exploratory={result.exploratory}, in_family={result.in_family}, "
            f"Holm decision {'present' if result.name in adjusted else 'absent'}; the flags must agree"
        )
    return result.exploratory


def _arms(result: ContrastResult) -> str:
    first, second = result.arms
    if result.factor is not None:
        return f"factor {result.factor}: {first} vs {second}"
    return f"{first} vs {second}"


def _margin(result: ContrastResult) -> str:
    """The preregistered equivalence margin, or `-` without one."""
    return NO_MARGIN if result.equivalence_margin is None else _num(result.equivalence_margin)


def _equivalence_interval(result: ContrastResult) -> str:
    """The interval at 1 - 2 alpha that is compared with the margin, or `-` without a margin."""
    if result.equivalence_interval is None:
        return NO_MARGIN
    low, high = result.equivalence_interval
    return f"[{_num(low)}, {_num(high)}]"


def _mde(result: ContrastResult) -> str:
    if result.minimum_detectable_effect is None:
        return "-"
    return _num(result.minimum_detectable_effect)


def _treatment(result: ContrastResult) -> str:
    """How the contrast's treatment attempts entered it, with the pairs that cost: never blank when it cost any."""
    excluded = result.pairs_missing["treatment_excluded"]
    lost = result.pairs_missing["treatment_outcome_none"]
    if result.treatment_outcomes == "excluded":
        return f"excluded ({excluded} pair(s) left out)"
    if result.treatment_outcomes == "scored":
        allowed = f", at most {result.max_unscored_treatment} preregistered" if result.max_unscored_treatment else ""
        return f"scored ({lost} pair(s) None{allowed})" if lost else "scored"
    return f"not declared ({lost} pair(s) None)" if lost else "-"


def _contrast_table(
    results: Sequence[ContrastResult],
    adjusted: Mapping[str, float],
    level: float | None,
    alpha: float,
    *,
    with_mde: bool,
) -> list[str]:
    interval = "Interval" if level is None else f"{percent(level)} interval"
    header = ["Contrast", "Arms (A vs B, A - B)", "Outcome", "Estimate", interval, "Margin", equivalence_header(alpha)]
    header += ["p", "Holm p", "Conclusion"]
    header += ["Clusters", "Pairs used", "Treatment attempts", "Tasks dropped"]
    header += ["Min. detectable effect"] if with_mde else []
    rows = []
    for r in results:
        exploratory = _exploratory(r, adjusted)
        conclusion = f"{r.conclusion} ({EXPLORATORY})" if exploratory else r.conclusion
        row = [
            r.name,
            _arms(r),
            r.outcome,
            _num(r.estimate),
            f"[{_num(r.ci_low)}, {_num(r.ci_high)}]",
            _margin(r),
            _equivalence_interval(r),
            _p(r.p_value),
            EXPLORATORY if exploratory else _p(adjusted[r.name]),
            conclusion,
            f"{r.n_clusters} (dropped {r.n_clusters_dropped})",
            _pairs_used(r),
            _treatment(r),
            ", ".join(r.tasks_dropped) or "none",
        ]
        rows.append(row + ([_mde(r)] if with_mde else []))
    return _table(header, rows)


def _pairs_used(result: ContrastResult) -> str:
    """The pairs used; for an untestable contrast, why there are none, by cause with the pairs tables' counts."""
    if result.conclusion != UNTESTABLE:
        return str(result.n_pairs)
    causes = [
        f"{CAUSE_HEADINGS[cause][0].lower()}{CAUSE_HEADINGS[cause][1:]} {result.pairs_missing[cause]}"
        for cause in MISSING_CAUSES
        if result.pairs_missing[cause]
    ]
    return f"0 (missing: {', '.join(causes)})" if causes else "0 (no pair planned)"


def _check_conclusion(result: ContrastResult) -> None:
    """Refuse a word outside the vocabulary, or one that disagrees with the pairs: untestable exactly when none."""
    if result.conclusion not in CONCLUSIONS:
        raise ValueError(f"contrast {result.name!r} has conclusion {result.conclusion!r}, not one of {CONCLUSIONS}")
    if (result.conclusion == UNTESTABLE) != (result.n_pairs == 0):
        raise ValueError(
            f"contrast {result.name!r}: conclusion {result.conclusion!r} with {result.n_pairs} pair(s) used; a "
            "contrast is untestable exactly when it used no pair"
        )


def _contrasts(result: AnalysisResult, level: float | None) -> list[str]:
    for r in result.contrasts:
        _check_conclusion(r)
    adjusted = {d.name: d.p_holm for d in result.holm}
    family = [r for r in result.contrasts if not _exploratory(r, adjusted)]
    exploratory = [r for r in result.contrasts if _exploratory(r, adjusted)]
    with_mde = any(r.minimum_detectable_effect is not None for r in result.contrasts)
    lines = [f"## Preregistered contrasts (Holm family of {len(family)}, alpha = {result.alpha:g})", ""]
    alpha = result.alpha
    table = _contrast_table(family, adjusted, level, alpha, with_mde=with_mde) if family else []
    lines += table or ["No contrast in the family."]
    lines += ["", *contrast_notes(alpha)]
    if with_mde:
        lines += ["", *MDE_NOTES]
    if family:
        lines += ["", *_pairs(family, "### Pairs of the family's contrasts")]
    lines += [
        "",
        "## Exploratory contrasts (outside the Holm family)",
        "",
    ]
    if exploratory:
        lines += _contrast_table(exploratory, adjusted, level, alpha, with_mde=with_mde)
        lines += ["", "Nothing adjusts these for multiplicity; each is judged by its raw p-value at alpha."]
        lines += ["", *_pairs(exploratory, "### Pairs of the exploratory contrasts")]
    else:
        lines += ["None declared."]
    return [*lines, "", *MISSING_PAIRS, ""]


def _pairs(results: Sequence[ContrastResult], heading: str) -> list[str]:
    """The pairs each contrast used and missed, by each cause, zeros included."""
    rows = [[r.name, r.n_pairs, *(r.pairs_missing[cause] for cause in MISSING_CAUSES)] for r in results]
    header = ["Contrast", "Pairs used", *(CAUSE_HEADINGS[cause] for cause in MISSING_CAUSES)]
    return [heading, "", *_table(header, rows)]


def _conditions(export: Mapping[str, Any]) -> list[str]:
    accounting = condition_accounting(export)
    positions: dict[str, list[int]] = defaultdict(list)
    for entry in export["analysed_attempts"]:
        if entry.get("dispatch_seq") is not None:
            positions[entry["condition"]].append(entry["dispatch_seq"])
    fields = ("planned", "analysed", "exhausted", "unfinished", "left_out", "set_aside")
    rows = [
        [
            cell["condition"],
            *(cell[field] for field in fields),
            _num(statistics.median(positions[cell["condition"]])) if positions[cell["condition"]] else UNKNOWN,
        ]
        for cell in accounting
    ]
    header = ["Condition", "Planned", "Analysed", "Exhausted", "Unfinished", "Left out", "Attempts set aside"]
    lines = [
        "## Conditions",
        "",
        *CONDITION_NOTES,
        "",
        *_table([*header, "Median dispatch position"], rows),
        "",
    ]
    return [*lines, *_infrastructure(export, [cell["condition"] for cell in accounting]), ""]


def _infrastructure(export: Mapping[str, Any], conditions: Sequence[str]) -> list[str]:
    """The infrastructure failures per condition and kind, with a row of zeros for a condition that has no row."""
    kinds = list(export["infrastructure_by_condition"])
    present = {k["condition"] for k in kinds}
    zeros = [
        {"condition": name, "kind": NO_KIND, "declared_treatment": False, "set_aside": 0, "treatment": 0}
        for name in conditions
        if name not in present
    ]
    rows = [
        [
            k["condition"],
            k["kind"],
            "yes" if k["declared_treatment"] else "no",
            k["set_aside"],
            k["treatment"],
            k.get("excluded", 0),
        ]
        for k in sorted(kinds + zeros, key=lambda k: k["condition"])  # stable: each condition's kinds keep their order
    ]
    lines = ["### Infrastructure failures by condition and kind", ""]
    if not rows:
        return [*lines, "No condition has a planned run."]
    header = ["Condition", "Kind", "Declared treatment", "Set aside (all runs)", "Scored as treatment", "Left out"]
    return [*lines, *_table(header, rows), "", *COUNT_DEFINITIONS]


def summary_lines(result: AnalysisResult) -> list[str]:
    """One line per contrast for a terminal: the estimate, the raw p-value, the Holm-adjusted one (or the word
    exploratory) and the conclusion word, which is labelled exploratory too outside the family. A conclusion is never
    printed without the basis it rests on, and `untestable` is never printed for a contrast that used a pair, or
    any other word for one that used none."""
    adjusted = {d.name: d.p_holm for d in result.holm}
    lines = []
    for r in result.contrasts:
        _check_conclusion(r)
        member = not _exploratory(r, adjusted)
        holm = _p(adjusted[r.name]) if member else EXPLORATORY
        word = r.conclusion if member else f"{r.conclusion} ({EXPLORATORY})"
        lines.append(f"{r.name}: estimate {_num(r.estimate)}, p {_p(r.p_value)}, Holm p {holm}, {word}")
    return lines


def _partial_line(export: Mapping[str, Any]) -> str:
    """Say whether the analysis is partial, and if it is, why."""
    if not export.get("partial", False):
        return "- Partial analysis: no"
    return (
        f"- **Partial analysis**: yes. {export.get('running_attempts', 0)} attempt(s) were still running when the "
        f"replay check was made, and {len(export.get('excluded_attempts', []))} run(s) are left out of the analysis "
        "(listed at the end)"
    )


def _left_out(excluded: Sequence[Mapping[str, Any]]) -> list[str]:
    """The attempts a partial analysis leaves out, with the reason, one row each."""
    lines = ["## Attempts left out of the analysis", ""]
    if not excluded:
        return [*lines, "None.", ""]
    rows = [[e["run_key"], e["attempt"], e["reason"]] for e in excluded]
    return [*lines, *_table(["Run key", "Attempt", "Reason"], rows), ""]


def _scorers(scorers: Mapping[str, str] | None) -> str:
    """Each scorer's name and version in name order, or n/a when the report was not given them."""
    return ", ".join(f"{name} `{version}`" for name, version in sorted((scorers or {}).items())) or UNKNOWN


def render_report(
    result: AnalysisResult,
    export: Mapping[str, Any],
    *,
    study: str,
    code: Mapping[str, Any],
    core_version: str | None,
    scorers: Mapping[str, str] | None = None,
    interval_level: float | None = None,
) -> str:
    """Render the report as Markdown.

    `code` is the code identity (`provenance.codestamp.version`), `scorers` the version of each scorer by name (the
    report says n/a without them), `interval_level` the level of the contrasts' intervals when the preregistration
    states one. A partial analysis is described by the export (`partial`,
    `running_attempts`, `excluded_attempts`). What each number counts is stated in the report beside it. The
    text ends with a newline.
    """
    state = "dirty: " + str(code.get("why")) if code.get("dirty") else "clean"
    planned = export["planned_runs"]
    left_out = export.get("excluded_attempts", [])
    lines = [
        f"# Report: {study}",
        "",
        "## Provenance",
        "",
        f"- Preregistration SHA-256: `{result.manifest['preregistration_sha256']}`",
    ]
    if "factor_schema_sha256" in result.manifest:
        lines.append(f"- Factor schema SHA-256: `{result.manifest['factor_schema_sha256']}`")
    lines += [
        f"- Code: commit `{code.get('commit')}`, tree `{code.get('tree_sha256')}` ({state})",
        f"- Core version: {core_version or UNKNOWN}",
        f"- Scorers: {_scorers(scorers)}",
        f"- Runs: {planned} planned, {len(export['analysed_attempts'])} analysed, "
        f"{len(export['exhausted_runs'])} exhausted (charged infrastructure attempts reached the limit; no scored "
        "attempt), "
        f"{len(export['unfinished_runs'])} unfinished, {len(left_out)} left out of the analysis",
        _partial_line(export),
        "",
    ]
    return "\n".join(lines + _contrasts(result, interval_level) + _conditions(export) + _left_out(left_out))
