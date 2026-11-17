# Provenance: new
"""The `## Mount isolation` section of the report of a batch whose mount was guarded (#179, O60): the runs that are
not clean, and the sensitivity analysis without them. Built as the egress section is (`report_egress`), so the two
read alike.

A batch is guarded when one of its analysed records carries the `mount_guard` instrument
(`expcore.mountguard.sensitivity`); the report of any other batch has no such section and is the text it always was.
The section shows:

- per condition, the analysed runs, each verdict's runs (clean, flagged, unknown), and the runs that are not clean
  by each flag; with a run that had no mount (a REST run: `not_applicable`, kept in the rerun and never counted as
  clean), a column for them and a sentence saying so, and nothing of it for a batch whose every run had a mount;
- one row per run that is not clean: its run key and attempt, its condition, its verdict and flags, its log's state
  and why it is unknown, its isolation and why it is unproven, and the accesses and tracers its log shows;
- the sensitivity analysis: the preregistered contrasts again without the runs that are not clean, in the tables the
  primary analysis uses (rendered by the caller, so the two cannot differ in form), or why there is none.
"""

from collections import Counter
from collections.abc import Callable
from typing import Any

from expcore.mountguard.flags import FLAGS, FROM_CAMPAIGN, NOT_APPLICABLE, VERDICTS, RunMountGuard
from expcore.mountguard.sensitivity import MountGuardAnalysis
from expcore.report_notes import MOUNT_GUARD_NOTES, MOUNT_NOT_APPLICABLE_NOTES, MOUNT_SENSITIVITY_NOTES

LEFT_OUT = "the runs that are not clean"  # what the rerun's own tables say they leave out
CELL_ITEMS = 5  # reasons shown in one cell before "and N more"
FLAG_NAMES = ("Token access", "Process access", "Log unknown", "Isolation unproven")


def _more(items: tuple[str, ...]) -> str:
    shown = ", ".join(items[:CELL_ITEMS]) or "-"
    return shown if len(items) <= CELL_ITEMS else f"{shown} and {len(items) - CELL_ITEMS} more"


def _by_condition(guard: MountGuardAnalysis, table: Callable[..., list[str]]) -> list[str]:
    analysed = Counter(run.condition for run in guard.runs)
    with_none = guard.counts()[NOT_APPLICABLE] > 0  # the column only for a batch that has such a run
    verdicts = (*VERDICTS, NOT_APPLICABLE) if with_none else VERDICTS
    by_verdict = {v: Counter(run.condition for run in guard.runs if run.verdict == v) for v in verdicts}
    by_flag = {flag: Counter(run.condition for run in guard.left_out if flag in run.flags) for flag in FLAGS}
    rows = [
        [condition, analysed[condition], *(by_verdict[v][condition] for v in verdicts),
         *(by_flag[flag][condition] for flag in FLAGS)]
        for condition in sorted(analysed)
    ]  # fmt: skip
    header = [
        "Condition",
        "Analysed",
        "Clean",
        "Flagged",
        "Unknown",
        *(["Not applicable"] if with_none else []),
        *FLAG_NAMES,
    ]
    return ["### Mount isolation by condition", "", *table(header, rows), ""]


def _row(run: RunMountGuard) -> list[Any]:
    accesses = f"{run.accesses.get('token_file', 0)} / {run.accesses.get('token_dir', 0)}"
    return [
        run.run_key, run.attempt, run.condition, run.verdict, ", ".join(run.flags), run.log, _more(run.unknown),
        run.isolation, _more(run.unproven), accesses, run.traced,
    ]  # fmt: skip


def _not_clean(guard: MountGuardAnalysis, table: Callable[..., list[str]]) -> list[str]:
    lines = ["### Runs that are not clean", ""]
    none = guard.counts()[NOT_APPLICABLE]
    if not guard.left_out and none == len(guard.runs):
        return [*lines, "None: the guard applies to no analysed run, since none of them had a mount.", ""]
    if not guard.left_out:
        which = "every analysed run" if not none else "every analysed run with a mount"
        return [*lines, f"None: {which}'s log is intact and shows no access, and its cells' isolation was proven.", ""]
    header = ["Run key", "Attempt", "Condition", "Verdict", "Flags", "Log", "Unknown because", "Isolation"]
    header += ["Unproven because", "Accesses (token / directory)", "Tracers"]
    return [*lines, *table(header, [_row(run) for run in guard.left_out]), ""]


def mount_guard_lines(
    guard: MountGuardAnalysis | None, table: Callable[..., list[str]], sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The section, or nothing for a batch whose mount was not guarded. `table` renders a table; `sensitivity` renders
    an analysis result's contrasts in the primary analysis's own tables."""
    if guard is None:
        return []
    n = guard.counts()
    lines = ["## Mount isolation (detected, not prevented)", "", *MOUNT_GUARD_NOTES, ""]
    lines.append(
        f"{n['left_out']} of {n['analysed']} analysed run(s) not clean: {n['flagged']} flagged for an access "
        f"({n['token_access']} with an access to the token's file or directory, {n['process_access']} with a tracer on "
        f"oneclient) and {n['unknown']} unknown; {n['log_unknown']} with an unknown log, {n['isolation_unproven']} "
        "with an unproven isolation (a run may have several flags)."
    )
    if n[NOT_APPLICABLE]:
        by_campaign = sum(1 for run in guard.runs if run.data_path_from == FROM_CAMPAIGN)
        how = (
            f" ({by_campaign} by the campaign's recorded data path, their records holding no snapshot)"
            if by_campaign
            else ""
        )
        lines.append(
            f"{n[NOT_APPLICABLE]} run(s) had no mount{how}, so the guard does not apply to them: they are counted "
            "apart, never as clean, and stay in the rerun."
        )
    lines += ["", *_by_condition(guard, table), *_not_clean(guard, table)]
    lines += [f"### Sensitivity analysis: {LEFT_OUT} left out", "", *MOUNT_SENSITIVITY_NOTES, ""]
    if n[NOT_APPLICABLE]:
        lines += [*MOUNT_NOT_APPLICABLE_NOTES, ""]
    if guard.refused is not None:
        return [*lines, f"Not computed: the analysis refused the rerun ({guard.refused}).", ""]
    if guard.sensitivity is None and n[NOT_APPLICABLE]:
        none = (
            "No run is left out (the runs with no mount stay in), so the analysis without the others is the one above."
        )
        return [*lines, none, ""]
    if guard.sensitivity is None:
        return [*lines, "Every run is clean, so the analysis without the others is the analysis above.", ""]
    return [*lines, f"Left out: {n['left_out']} run(s).", "", *sensitivity(guard.sensitivity), ""]
