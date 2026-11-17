# Provenance: new
"""The `## Egress and mount isolation together` section of the report of a batch checked by two or more guards (#179,
O60, O73): the runs any guard leaves out, and the analysis again without them, beside each guard's own section and
rerun.

A batch gets the section only when its records carry two or more of the `egress`, `mount_guard` and
`process_visibility` instruments (`expcore.guards`); with one guard alone, that guard's rerun is the combined one, and
the report is the text it was. The section is named for the guards that checked the batch (`Egress and mount isolation
together`, as it always was for those two; `Egress, mount isolation and process visibility together` for all three) and
shows the runs left out per condition, by which guard left each out; one row per left-out run with what each guard
found; and the rerun in the tables the primary analysis uses (rendered by the caller), or why there is none.
"""

from collections.abc import Callable, Sequence
from typing import Any

from expcore.guards import EGRESS, MOUNT, NOT_APPLICABLE, VISIBILITY, GuardedRun, GuardsAnalysis
from expcore.report_notes import GUARDS_NOTES, GUARDS_SENSITIVITY_NOTES, any_guard_notes, any_guard_sensitivity_notes

TITLES = {EGRESS: "egress", MOUNT: "mount isolation", VISIBILITY: "process visibility"}  # the section's title
ALONE = {EGRESS: "egress", MOUNT: "the mount guard", VISIBILITY: "the process visibility check"}  # "by X alone"
COLUMNS = {EGRESS: "Egress", MOUNT: "Mount guard", VISIBILITY: "Process visibility"}  # a guard's column of a row
BY_NAMES = {EGRESS: "egress", MOUNT: "mount guard", VISIBILITY: "process visibility", "both": "both"}


def _list(names: Sequence[str], last: str = "and") -> str:
    """Names in a sentence: `a and b`, `a, b and c`."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


def title(guards: GuardsAnalysis) -> str:
    """The section's heading: the guards that checked the batch, named, `together`."""
    named = _list([TITLES[g] for g in guards.guards])
    return f"## {named[0].upper()}{named[1:]} together (detected, not prevented)"


def either(guards: GuardsAnalysis) -> str:
    """`either` for a batch two guards checked, `any` for three: `the runs either guard leaves out`."""
    return "either" if len(guards.guards) == 2 else "any"


def left_out_label(guards: GuardsAnalysis) -> str:
    """What the rerun's own tables say they leave out."""
    return f"the runs {either(guards)} guard leaves out"


def every(guards: GuardsAnalysis) -> str:
    """`both` or `all`: the guards a batch was checked by."""
    return "both" if len(guards.guards) == 2 else "all"


def by_alone(guards: GuardsAnalysis, counts: dict[str, int]) -> str:
    """The counts of the runs left out, by each guard alone and by several, in a sentence."""
    named = [f"{counts[f'{g}_only']} by {ALONE[g]} alone" for g in guards.guards]
    return ", ".join(
        [*named, f"{counts['both']} by both" if len(guards.guards) == 2 else f"{counts['several']} by several"]
    )


def not_applicable_kept(guards: GuardsAnalysis) -> bool:
    """Whether a guard does not apply to some run of the batch: such a run is kept, and never said to be clean to it."""
    return any(NOT_APPLICABLE in run.states.values() for run in guards.runs)


def _by_condition(guards: GuardsAnalysis, table: Callable[..., list[str]]) -> list[str]:
    keys = guards.count_keys()
    rows = [[condition, *(counts[key] for key in keys)] for condition, counts in guards.by_condition().items()]
    several = "By both" if len(guards.guards) == 2 else "By several"
    header = ["Condition", "Analysed", "Left out", *(f"By {ALONE[g]} alone" for g in guards.guards), several]
    return [f"### Left out by {either(guards)} guard, by condition", "", *table(header, rows), ""]


def _by(run: GuardedRun) -> str:
    return " + ".join(BY_NAMES[part] for part in str(run.by).split("+"))


def _row(guards: GuardsAnalysis, run: GuardedRun) -> list[Any]:
    found = run.states
    return [run.run_key, run.attempt, run.condition, *(found[g] for g in guards.guards), _by(run)]


def _left_out(guards: GuardsAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = [f"### Runs {either(guards)} guard leaves out", ""]
    if not guards.left_out and not_applicable_kept(guards):
        none = f"None: no analysed run is left out by {either(guards)} guard; a run a guard does not apply to is kept, "
        return [*lines, none + "never counted as clean to it.", ""]
    if not guards.left_out:
        return [*lines, f"None: every analysed run is clean to {every(guards)} guards.", ""]
    header = ["Run key", "Attempt", "Condition", *(COLUMNS[g] for g in guards.guards), "Left out by"]
    return [*lines, *table(header, [_row(guards, run) for run in guards.left_out]), ""]


def guards_lines(
    guards: GuardsAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The section, or nothing for a batch not checked by two or more guards. `table` renders a table; `sensitivity`
    renders an analysis result's contrasts in the primary analysis's own tables."""
    if guards is None:
        return []
    n = guards.counts()
    names = _list([TITLES[g] for g in guards.guards])
    notes = GUARDS_NOTES if guards.legacy else any_guard_notes(names)
    rerun = GUARDS_SENSITIVITY_NOTES if guards.legacy else any_guard_sensitivity_notes(either(guards))
    lines = [title(guards), "", *notes, ""]
    lines.append(
        f"{n['left_out']} of {n['analysed']} analysed run(s) left out by {either(guards)} guard: {by_alone(guards, n)}."
    )
    lines += ["", *_by_condition(guards, table), *_left_out(guards, table)]
    lines += [f"### Sensitivity analysis: {left_out_label(guards)}, left out", "", *rerun, ""]
    if guards.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({guards.refused}).", ""]
    if guards.sensitivity is None and not_applicable_kept(guards):
        none = f"No run is left out by {either(guards)} guard, so the analysis without the others is the one above."
        return [*lines, none, ""]
    if guards.sensitivity is None:
        return [
            *lines,
            f"Every run is clean to {every(guards)} guards, so the analysis without the others is the one above.",
            "",
        ]
    return [*lines, f"Left out: {n['left_out']} run(s).", "", *sensitivity(guards.sensitivity), ""]


def summary_lines(guards: GuardsAnalysis, rerun: Callable[[Any, str], list[str]]) -> list[str]:
    """The terminal's lines of the guards together (O60, O73): the runs any leaves out, overall and per condition, then
    each confirmatory contrast without them. `rerun` renders a sensitivity result's lines under a prefix."""
    n, label = guards.counts(), f"{every(guards)} guards"
    lines = [f"{label}: {n['left_out']} of {n['analysed']} analysed run(s) left out ({by_alone(guards, n)})"]
    for condition, c in guards.by_condition().items():
        lines.append(f"{label}, {condition}: {c['left_out']} of {c['analysed']} left out ({by_alone(guards, c)})")
    prefix = f"{every(guards)}-guards sensitivity"
    if guards.refused is not None:
        return [*lines, f"{prefix}: not computed ({guards.refused})"]
    if guards.sensitivity is None and not_applicable_kept(guards):
        return [*lines, f"{prefix}: no run left out, the same as the analysis above"]
    if guards.sensitivity is None:
        return [*lines, f"{prefix}: every run clean to {every(guards)}, the same as the analysis above"]
    return lines + rerun(guards.sensitivity, prefix)
