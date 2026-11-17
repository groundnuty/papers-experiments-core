# Provenance: new
"""The sensitivity reruns a preregistration declares (S19), and the check that an analysis runs exactly those.

An analysis of a batch checked by a guard runs the preregistered analysis again without the runs the guard leaves out:
`egress` (the egress check), `mount_guard` (the mount guard), `process_visibility` (the hidden-process check),
`read_log` (the read-log scan, S20) and `any_guard` (without every run any guard that checked the batch leaves out,
when two or more did). Which of them an
analysis runs depends on which instruments its records carry, so a study that lists a guard on no record, or on the
wrong runs, would otherwise get a different set of reruns than the one it meant to preregister. The preregistration
declares the set:

    [sensitivity]
    reruns = ["egress", "mount_guard", "any_guard"]   # in any order; each of the five names at most once

and `analyze` refuses when the reruns it would run are not exactly these, in either direction, naming the difference: a
declared rerun that no record supports (no record carries the guard, or fewer than two guards checked the batch for
`any_guard`), and a rerun the records would produce that the preregistration does not declare. **A rerun counts as run
when its guard checked the batch, whether or not it left a run out:** a batch whose every run is clean has no second
analysis, and is still the rerun the study declared. `reruns = []` declares that none is expected. **A preregistration
with no `[sensitivity]` table reads as `reruns = []`:** a batch no guard checked runs no rerun and passes with no
table, its analysis.json unchanged, and a batch a guard checked is refused, naming the reruns it would run and the
declaration that is missing, so no rerun goes unpreregistered. (`Preregistration.sensitivity_reruns` is None for an
absent table and `()` for `reruns = []`, so that analysis.json says only what was written.) The declaration is in the
file, so in the preregistration's hash.
"""

from collections.abc import Sequence

from expcore.config.loading import Section, close_match

EGRESS, MOUNT_GUARD, PROCESS_VISIBILITY, ANY_GUARD = "egress", "mount_guard", "process_visibility", "any_guard"
READ_LOG = "read_log"
RERUNS = (EGRESS, MOUNT_GUARD, PROCESS_VISIBILITY, READ_LOG, ANY_GUARD)
SINGLE = (
    EGRESS,
    MOUNT_GUARD,
    PROCESS_VISIBILITY,
    READ_LOG,
)  # the reruns of one guard; `any_guard` is the one of several
KEYS = ("reruns",)


def names_problem(names: object) -> str | None:
    """Why `names` is not a declaration of reruns (a list of the four names, none twice); None when it is one."""
    if not isinstance(names, list | tuple) or not all(isinstance(name, str) for name in names):
        return f"must be an array of names from {', '.join(RERUNS)}"
    for name in names:
        if name not in RERUNS:
            return (
                f"names {name!r}, which is not a rerun{close_match(name, RERUNS)}; the reruns are {', '.join(RERUNS)}"
            )
    twice = sorted({name for name in names if names.count(name) > 1})
    return f"names {', '.join(twice)} twice" if twice else None


def declared(root: Section) -> tuple[str, ...] | None:
    """The reruns a preregistration's `[sensitivity]` table declares, in the order given; None without the table."""
    if "sensitivity" not in root.data:
        return None
    table = Section(root.section("sensitivity").data, root.file, "[sensitivity]")  # messages name [sensitivity]
    table.reject_unknown(KEYS)
    if "reruns" not in table.data:
        raise table.fail("reruns", "is required: the reruns the study expects, possibly none")
    names = table.data["reruns"]
    problem = names_problem(names)
    if problem is not None:
        raise table.fail("reruns", problem)
    return tuple(names)


def checked(names: Sequence[str] | None) -> tuple[str, ...] | None:
    """The declaration of a preregistration built in code: None, or the names as a tuple; ValueError naming the
    problem otherwise."""
    if names is None:
        return None
    problem = names_problem(names)
    if problem is not None:
        raise ValueError(f"sensitivity reruns {problem}")
    return tuple(names)


def ran_reruns(
    egress: object, mount_guard: object, visibility: object, guards: object, read_log: object = None
) -> tuple[str, ...]:
    """The reruns an analysis would run, in the order of `RERUNS`: each guard's analysis that exists (its guard checked
    the batch, whether or not a run was left out), and the one of every guard together when it exists."""
    found = (egress, mount_guard, visibility, read_log, guards)
    return tuple(name for name, analysis in zip(RERUNS, found, strict=True) if analysis is not None)


def missing_table(source: str, ran: Sequence[str]) -> str:
    """The sentence of a refusal for a preregistration with no `[sensitivity]` table: it declares no reruns, and the
    table to add for the reruns the analysis would run."""
    names = ", ".join(f'"{name}"' for name in ran)
    return (
        f"{source} has no [sensitivity] table, and an absent table declares none; add [sensitivity] reruns = [{names}]"
    )


def _cannot_run(name: str, ran: Sequence[str]) -> str:
    """Why a declared rerun is not one this analysis can run."""
    if name != ANY_GUARD:
        return f"no analysed record carries the {name} instrument"
    guards = [guard for guard in SINGLE if guard in ran]
    checked_by = "none did" if not guards else f"only {' and '.join(guards)} did"
    return f"it needs two or more guards to have checked the batch; {checked_by}"


def difference(declared_names: Sequence[str], ran: Sequence[str]) -> str | None:
    """None when the reruns an analysis would run (`ran`) are exactly the declared ones, whatever their order; else a
    message naming each declared rerun it cannot run, with why, and each rerun it would run that is not declared."""
    missing = [name for name in declared_names if name not in ran]
    extra = [name for name in ran if name not in declared_names]
    if not missing and not extra:
        return None
    lines = []
    if missing:
        lines.append("declared but not run: " + "; ".join(f"{name} ({_cannot_run(name, ran)})" for name in missing))
    if extra:
        lines.append("run but not declared: " + ", ".join(extra))
    lines.append(f"declared: {', '.join(declared_names) or 'none'}; would run: {', '.join(ran) or 'none'}")
    return "\n  ".join(lines)
