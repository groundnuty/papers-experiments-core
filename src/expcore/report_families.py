# Provenance: new
"""The parts of the report that several named Holm families add (S17, #76): each family's steps, and the check that
the families and the Holm decisions are one computation.

A preregistration with one flat `holm_family` has no named family, and its report is the text it was before families
were named: nothing here is called for it. With named families, `expcore.report` shows each family's contrasts in a
table of their own, followed by the family's steps from `steps_lines`, and refuses a result `check_families` refuses,
since showing it either way would show a member adjusted in another family than its own.

Declared equivalence families (S18) are checked the same way (`check_equivalence_families`), and each one's steps
over its claims' TOST p-values are shown by `equivalence_steps_lines`; without them nothing of that is called.
"""

from collections.abc import Callable, Sequence

from expcore.stats.analysis import AnalysisResult, EquivalenceFamily, HolmDecision, HolmFamily, HolmStep

Table = Callable[[Sequence[str], Sequence[Sequence[object]]], list[str]]
Format = Callable[[float | None], str]


def check_families(result: AnalysisResult) -> dict[str, str]:
    """Each confirmatory contrast's family, by contrast name; refuse a result whose families and Holm decisions
    disagree: a member with two decisions, a decision in no family or in two, a family member with no decision, a
    step whose numbers are not its member's decision, or equivalence decisions that are not the family's own (S15).
    A result with no named family (one flat family) has nothing to check."""
    if not result.holm_families:
        return {}
    decisions = _unique(result.holm, "Holm decisions")
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
    _check_equivalence(result, family_of)
    return family_of


def _unique(decisions: Sequence[HolmDecision], what: str) -> dict[str, HolmDecision]:
    """The decisions by contrast name; refuse a contrast with two, which would give a reader two verdicts."""
    by_name: dict[str, HolmDecision] = {}
    for decision in decisions:
        if decision.name in by_name:
            count = sum(d.name == decision.name for d in decisions)
            raise ValueError(f"contrast {decision.name!r} has {count} {what}; a member has one")
        by_name[decision.name] = decision
    return by_name


def _check_equivalence(result: AnalysisResult, family_of: dict[str, str]) -> None:
    """Each family's equivalence decisions are exactly its own members' claims as `equivalence_holm` decided them:
    none of another family's member, none with other numbers, and none of its claims left out (S15, S17). Under
    declared equivalence families no Holm family holds one, and those families are checked instead (S18)."""
    if result.equivalence_families:
        check_equivalence_families(result)
        return
    claims = _unique(result.equivalence_holm, "equivalence decisions")
    held: dict[str, str] = {}
    for family in result.holm_families:
        for decision in family.equivalence:
            if family_of.get(decision.name) != family.name:
                raise ValueError(
                    f"Holm family {family.name!r} holds the equivalence decision of {decision.name!r}, which is not "
                    "its member"
                )
            claim = claims.get(decision.name)
            if decision.name in held or claim is None or not _same(_numbers(decision), _numbers(claim)):
                raise ValueError(
                    f"contrast {decision.name!r}: its equivalence decision in Holm family {family.name!r} is not "
                    "its one decision in equivalence_holm"
                )
            held[decision.name] = family.name
    left_out = [name for name in claims if name not in held]
    if left_out:
        raise ValueError(f"contrast {left_out[0]!r} has an equivalence decision that no Holm family holds")


def check_equivalence_families(result: AnalysisResult) -> dict[str, str]:
    """Each equivalence claim's declared equivalence family, by contrast name (S18); refuse a result whose families
    and equivalence decisions disagree: a claim with two decisions, in no family or in two, a step whose numbers are
    not its claim's decision in `equivalence_holm`, a family member that is not an equivalence claim, or a Holm family
    holding an equivalence decision, which would show a claim adjusted in another family than its own. A result with
    no declared equivalence family has nothing to check."""
    if not result.equivalence_families:
        return {}
    claims = _unique(result.equivalence_holm, "equivalence decisions")
    flagged = [r.name for r in result.contrasts if r.in_equivalence_family]
    family_of: dict[str, str] = {}
    for family in result.equivalence_families:
        if sorted(family.members) != sorted(s.name for s in family.steps):
            raise ValueError(f"equivalence family {family.name!r}: its members and its steps name different contrasts")
        for step in family.steps:
            if step.name in family_of:
                raise ValueError(
                    f"contrast {step.name!r} is in two equivalence families, {family_of[step.name]!r} and "
                    f"{family.name!r}"
                )
            family_of[step.name] = family.name
            claim = claims.get(step.name)
            if claim is None or not _same(_numbers(step), _numbers(claim)):
                raise ValueError(
                    f"contrast {step.name!r}: its step in equivalence family {family.name!r} is not its decision in "
                    "equivalence_holm"
                )
    left_out = [name for name in (*claims, *flagged) if name not in family_of]
    if left_out:
        raise ValueError(f"contrast {left_out[0]!r} is an equivalence claim that no equivalence family holds")
    unflagged = [name for name in family_of if name not in flagged]
    if unflagged:
        raise ValueError(
            f"contrast {unflagged[0]!r} is in equivalence family {family_of[unflagged[0]]!r} but is not an "
            "equivalence claim"
        )
    holding = [family.name for family in result.holm_families if family.equivalence]
    if holding:
        raise ValueError(
            f"Holm family {holding[0]!r} holds equivalence decisions, but the claims are adjusted in their declared "
            "equivalence families, apart from every Holm family"
        )
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
    header = ["Step", "Contrast", "p", "Level", "Holm p", "Rejected"]
    return [f"Holm steps of {one_line(family.name)}:", "", *table(header, _step_rows(family.steps, p, num))]


def equivalence_steps_lines(family: EquivalenceFamily, table: Table, p: Format, num: Format) -> list[str]:
    """A declared equivalence family's steps from the smallest TOST p-value up (S18): each step's claim, raw TOST p,
    level (as alpha over the claims still in the family), adjusted TOST p and whether it is equivalent."""
    header = ["Step", "Contrast", "TOST p", "Level", "Holm TOST p", "Equivalent"]
    heading = f"TOST steps of equivalence family {one_line(family.name)}:"
    return [heading, "", *table(header, _step_rows(family.steps, p, num))]


def _step_rows(steps: Sequence[HolmStep], p: Format, num: Format) -> list[list[object]]:
    """Each step's number, contrast, raw p, level as alpha / (m - step + 1), adjusted p and verdict."""
    size = len(steps)
    return [
        [s.step, s.name, p(s.p_value), f"{num(s.level)} (alpha/{size - s.step + 1})", p(s.p_holm), _verdict(s.reject)]
        for s in steps
    ]


def one_line(name: str) -> str:
    """A family's name as a heading or a line shows it: a TOML key may hold a line break, which would end the line."""
    return name.replace("\r", " ").replace("\n", " ")


def _verdict(reject: bool) -> str:
    return "yes" if reject else "no"
