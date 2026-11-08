# Provenance: new
"""The Markdown report of a preregistered analysis: what was found, under which preregistration, from which code.

`render_report` is a pure function of the analysis result and the campaign export, so two renderings of the same inputs
are the same text: no clock, no path and no random number enters it.

- Contrasts in the Holm family are in one table, contrasts outside it in a table of their own under the heading
  "Exploratory contrasts": nothing controls their multiplicity.
- A conclusion is never shown without its significance. Each row carries the raw p-value and the Holm-adjusted one
  (or the word "exploratory") beside the conclusion, and the conclusion words already encode the decision:
  `difference`, `trivial_difference`, `equivalent`, `not_detected`. Failing to detect a difference is not a finding of
  none, and the report says so under the table.
- Per condition it shows the runs analysed and the median dispatch position of their attempts, so a reader can see
  whether one condition's runs came late, and per condition and infrastructure kind the attempts set aside and the
  attempts scored as treatment.
- The header names the preregistration hash (with `+override` when the copy was marked as a test copy), the code stamp
  and the core version.
"""

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.stats.analysis import CONCLUSIONS, AnalysisResult, ContrastResult

EXPLORATORY = "exploratory"
UNKNOWN = "n/a"


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


def _arms(result: ContrastResult) -> str:
    first, second = result.arms
    if result.factor is not None:
        return f"factor {result.factor}: {first} vs {second}"
    return f"{first} vs {second}"


def _mde(result: ContrastResult) -> str:
    if result.minimum_detectable_effect is None:
        return "-"
    return _num(result.minimum_detectable_effect)


def _contrast_table(
    results: Sequence[ContrastResult], adjusted: Mapping[str, float], level: float | None, *, with_mde: bool
) -> list[str]:
    interval = "Interval" if level is None else f"{level:.0%} interval"
    header = ["Contrast", "Arms (A vs B, A - B)", "Outcome", "Estimate", interval, "p", "Holm p", "Conclusion"]
    header += ["Clusters", "Tasks dropped"] + (["Min. detectable effect"] if with_mde else [])
    rows = []
    for r in results:
        exploratory = r.name not in adjusted
        conclusion = f"{r.conclusion} ({EXPLORATORY})" if exploratory else r.conclusion
        row = [
            r.name,
            _arms(r),
            r.outcome,
            _num(r.estimate),
            f"[{_num(r.ci_low)}, {_num(r.ci_high)}]",
            _p(r.p_value),
            EXPLORATORY if exploratory else _p(adjusted[r.name]),
            conclusion,
            f"{r.n_clusters} (dropped {r.n_clusters_dropped})",
            ", ".join(r.tasks_dropped) or "none",
        ]
        rows.append(row + ([_mde(r)] if with_mde else []))
    return _table(header, rows)


def _contrasts(result: AnalysisResult, level: float | None) -> list[str]:
    for r in result.contrasts:
        if r.conclusion not in CONCLUSIONS:
            raise ValueError(f"contrast {r.name!r} has conclusion {r.conclusion!r}, not one of {CONCLUSIONS}")
    adjusted = {d.name: d.p_holm for d in result.holm}
    family = [r for r in result.contrasts if r.name in adjusted]
    exploratory = [r for r in result.contrasts if r.name not in adjusted]
    with_mde = any(r.minimum_detectable_effect is not None for r in result.contrasts)
    lines = [f"## Preregistered contrasts (Holm family of {len(family)}, alpha = {result.alpha:g})", ""]
    lines += _contrast_table(family, adjusted, level, with_mde=with_mde) if family else ["No contrast in the family."]
    lines += [
        "",
        "Conclusions: `difference` (significant), `trivial_difference` (significant, but inside the equivalence",
        "margin), `equivalent` (inside the margin, not significant), `not_detected` (anything else; failing to",
        "detect a difference is not evidence that there is none).",
    ]
    if with_mde:
        lines += [
            "",
            "Min. detectable effect: the smallest effect this contrast's number of clusters could detect with the",
            "preregistered power; n/a when it cannot be found (no spread between clusters, or too few clusters).",
        ]
    lines += [
        "",
        "## Exploratory contrasts (outside the Holm family)",
        "",
    ]
    if exploratory:
        lines += _contrast_table(exploratory, adjusted, level, with_mde=with_mde)
        lines += ["", "Nothing adjusts these for multiplicity; each is judged by its raw p-value at alpha."]
    else:
        lines += ["None declared."]
    return [*lines, ""]


def _conditions(export: Mapping[str, Any]) -> list[str]:
    analysed: dict[str, list[int]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for entry in export["analysed_attempts"]:
        counts[entry["condition"]] += 1
        if entry.get("dispatch_seq") is not None:
            analysed[entry["condition"]].append(entry["dispatch_seq"])
    names = sorted({*counts, *(e["condition"] for e in export["exhausted_runs"] + export["unfinished_runs"])})
    rows = [
        [
            name,
            counts[name],
            _num(statistics.median(analysed[name])) if analysed[name] else UNKNOWN,
        ]
        for name in names
    ]
    lines = [
        "## Conditions",
        "",
        "Median dispatch position is the median place of the analysed attempts in the campaign's dispatch sequence;",
        "a condition whose runs came late has a higher one.",
        "",
        *_table(["Condition", "Runs analysed", "Median dispatch position"], rows),
        "",
    ]
    kinds = export["infrastructure_by_condition"]
    lines += ["### Infrastructure failures by condition and kind", ""]
    if kinds:
        rows = [
            [k["condition"], k["kind"], "yes" if k["declared_treatment"] else "no", k["set_aside"], k["treatment"]]
            for k in kinds
        ]
        header = ["Condition", "Kind", "Declared treatment", "Set aside (run again)", "Scored as treatment"]
        lines += _table(header, rows)
    else:
        lines += ["No infrastructure failure."]
    return [*lines, ""]


def summary_lines(result: AnalysisResult) -> list[str]:
    """One line per contrast for a terminal: the estimate, the raw p-value, the Holm-adjusted one (or the word
    exploratory) and the conclusion word, which is labelled exploratory too outside the family. A conclusion is never
    printed without the basis it rests on."""
    adjusted = {d.name: d.p_holm for d in result.holm}
    lines = []
    for r in result.contrasts:
        member = r.name in adjusted
        holm = _p(adjusted[r.name]) if member else EXPLORATORY
        word = r.conclusion if member else f"{r.conclusion} ({EXPLORATORY})"
        lines.append(f"{r.name}: estimate {_num(r.estimate)}, p {_p(r.p_value)}, Holm p {holm}, {word}")
    return lines


def render_report(
    result: AnalysisResult,
    export: Mapping[str, Any],
    *,
    study: str,
    code: Mapping[str, Any],
    core_version: str | None,
    interval_level: float | None = None,
) -> str:
    """Render the report as Markdown.

    `code` is the code identity (`provenance.codestamp.version`), `interval_level` the level of the contrasts'
    intervals when the preregistration states one. The text ends with a newline.
    """
    state = "dirty: " + str(code.get("why")) if code.get("dirty") else "clean"
    planned = export["planned_runs"]
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
        f"- Runs: {planned} planned, {len(export['analysed_attempts'])} analysed, "
        f"{len(export['exhausted_runs'])} exhausted (every attempt failed on infrastructure), "
        f"{len(export['unfinished_runs'])} unfinished",
        "",
    ]
    return "\n".join(lines + _contrasts(result, interval_level) + _conditions(export))
