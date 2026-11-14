# Provenance: new
"""The parts of the report that several named Holm families add (S16, #76): each family's steps, and the check that
the families and the Holm decisions are one computation.

A preregistration with one flat `holm_family` has no named family, and its report is the text it was before families
were named: nothing here is called for it. With named families, `expcore.report` shows each family's contrasts in a
table of their own, followed by the family's steps from `steps_lines`, and refuses a result `check_families` refuses,
since showing it either way would show a member adjusted in another family than its own.
"""

from collections.abc import Callable, Sequence

from expcore.stats.analysis import AnalysisResult, HolmDecision, HolmFamily, HolmStep

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]
Format = Callable[[float | None], str]


def check_families(result: AnalysisResult) -> dict[str, str]:
    """Each confirmatory contrast's family, by contrast name; refuse a result whose families and Holm decisions
    disagree: a decision in no family or in two, a family member with no decision, or a step whose numbers are not
    its member's decision. A result with no named family (one flat family) has nothing to check."""
    if not result.holm_families:
        return {}
    decisions = {d.name: d for d in result.holm}
    family_of: dict[str, str] = {}
    for family in result.holm_families:
        if sorted(family.members) != sorted(s.name for s in family.steps):
            raise ValueError(f"Holm family {family.name!r}: its members and its steps name different contrasts")
        for step in family.steps:
            if step.name in family_of:
                raise ValueError(
                    f"contrast {step.name!r} is in two Holm families, {family_of[step.name]!r} and {family.name!r}"
                )
            family_of[step.name] = family.name
            decision = decisions.get(step.name)
            if decision is None or not _same(_numbers(step), _numbers(decision)):
                raise ValueError(
                    f"contrast {step.name!r}: its step in Holm family {family.name!r} is not its Holm decision"
                )
    missing = [name for name in decisions if name not in family_of]
    if missing:
        raise ValueError(f"contrast {missing[0]!r} has a Holm decision and is in no Holm family")
    return family_of


def _numbers(decision: HolmStep | HolmDecision) -> tuple[float, float, float, bool]:
    """What a step and a decision must agree on: the raw and adjusted p-value, the level and the verdict."""
    return decision.p_value, decision.p_holm, decision.level, decision.reject


def _same(left: Sequence[object], right: Sequence[object]) -> bool:
    """Equal field by field, a NaN equal to a NaN (an untestable member's p-values)."""
    return all(a == b or (a != a and b != b) for a, b in zip(left, right, strict=True))


def steps_lines(family: HolmFamily, table: Table, p: Format, num: Format) -> list[str]:
    """A family's steps from the smallest p-value up: each step's contrast, raw p, level (as alpha over the hypotheses
    still in the family), adjusted p and verdict."""
    size = len(family.steps)
    rows = [
        [s.step, s.name, p(s.p_value), f"{num(s.level)} (alpha/{size - s.step + 1})", p(s.p_holm), _verdict(s.reject)]
        for s in family.steps
    ]
    header = ["Step", "Contrast", "p", "Level", "Holm p", "Rejected"]
    return [f"Holm steps of {one_line(family.name)}:", "", *table(header, rows)]


def one_line(name: str) -> str:
    """A family's name as a heading or a line shows it: a TOML key may hold a line break, which would end the line."""
    return name.replace("\r", " ").replace("\n", " ")


def _verdict(reject: bool) -> str:
    return "yes" if reject else "no"
