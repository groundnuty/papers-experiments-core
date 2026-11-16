"""Named Holm families and equivalence claims on an analysis result, for the tests of the sensitivity analyses a report
shows (S15, S17, S18): the result a test builds with its own contrasts gets families whose steps are its own decisions,
so the report's checks (`expcore.report_families`) take them as they take an analysis's."""

import dataclasses
from collections.abc import Mapping, Sequence

from expcore.stats.analysis import AnalysisResult, ContrastResult, EquivalenceFamily, HolmDecision, HolmFamily, HolmStep
from expcore.stats.contrasts import holm


def named_families(result: AnalysisResult, families: Mapping[str, Sequence[str]]) -> AnalysisResult:
    """`result` with its Holm decisions grouped into these named families (S17): every member of `result.holm` in
    exactly one, each step the member's own decision, in the order of the family's members."""
    decisions = {d.name: d for d in result.holm}
    built = []
    for name, members in families.items():
        steps = tuple(
            HolmStep(number, member, d.p_value, d.p_holm, d.level, d.reject)
            for number, member in enumerate(members, start=1)
            for d in [decisions[member]]
        )
        built.append(HolmFamily(name, tuple(members), steps))
    return dataclasses.replace(result, holm_families=tuple(built))


def claim(result: ContrastResult, tost_p: float, tost_p_holm: float, level: float, equivalent: bool) -> ContrastResult:
    """`result` as the analysis gives a member of an equivalence family: its margin, interval, TOST p-values and
    verdict, the interval at `level` (1 - 2 x the level Holm compared it with)."""
    return dataclasses.replace(
        result,
        p_value=0.4,
        significant=False,
        conclusion="equivalent" if equivalent else "not_detected",
        equivalence_margin=0.2,
        equivalence_interval=(-0.05, 0.06),
        equivalent=equivalent,
        in_equivalence_family=True,
        tost_p=tost_p,
        tost_p_holm=tost_p_holm,
        equivalence_level=level,
        equivalence_interval_adjusted=(-0.08, 0.09),
    )


def declared_equivalence(result: AnalysisResult, name: str, tost_p: Mapping[str, float]) -> AnalysisResult:
    """`result` with these contrasts as the claims of one declared equivalence family (S18): each claim's TOST p-value
    as given, Holm over them at alpha by the kernel the report's check recomputes it with, in the family's declared
    order, and each claim's numbers its step's."""
    members = tuple(tost_p)
    outcome = holm([tost_p[m] for m in members], result.alpha)
    order = sorted(range(len(members)), key=lambda i: outcome.levels[i])
    steps = tuple(
        HolmStep(number, members[i], tost_p[members[i]], outcome.adjusted[i], outcome.levels[i], outcome.reject[i])
        for number, i in enumerate(order, start=1)
    )
    by_step = {step.name: step for step in steps}
    claims = tuple(
        claim(r, s.p_value, s.p_holm, 1 - 2 * s.level, s.reject) if (s := by_step.get(r.name)) else r
        for r in result.contrasts
    )
    decisions = tuple(HolmDecision(s.name, s.p_value, s.p_holm, s.level, s.reject) for s in steps)
    family = EquivalenceFamily(name, members, steps)
    return dataclasses.replace(result, contrasts=claims, equivalence_holm=decisions, equivalence_families=(family,))


def flat_equivalence(result: AnalysisResult, tost_p: Mapping[str, float]) -> AnalysisResult:
    """`result` with these contrasts as the claims of the one flat equivalence family (S15), Holm over their TOST
    p-values at alpha."""
    members = tuple(tost_p)
    outcome = holm([tost_p[m] for m in members], result.alpha)
    by_name = {m: (tost_p[m], outcome.adjusted[i], outcome.levels[i], outcome.reject[i]) for i, m in enumerate(members)}
    claims = tuple(
        claim(r, by_name[r.name][0], by_name[r.name][1], 1 - 2 * by_name[r.name][2], by_name[r.name][3])
        if r.name in by_name
        else r
        for r in result.contrasts
    )
    decisions = tuple(HolmDecision(m, *by_name[m][:2], by_name[m][2], by_name[m][3]) for m in members)
    return dataclasses.replace(result, contrasts=claims, equivalence_holm=decisions)
