"""Several named Holm families in one preregistration (S17, #76): loading, refusals, and Holm within each family.

The known answers below are worked out by hand at alpha = 0.05. A family of m orders its members by p-value and
compares the k-th smallest with alpha / (m - k + 1), stopping at the first that misses; the adjusted p of the k-th is
the running maximum of (m - k + 1) x p, capped at 1. Every p-value is chosen clear of every level, so no case rests on
a p-value that lands on its level (S06).
"""

import hashlib
import json
import math
import tomllib

import numpy as np
import pytest
from stats_support import make_row

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.stats import estimators
from expcore.stats.analysis import HolmStep, analyse, equivalence_feasibility, power_study
from expcore.stats.estimators import EstimatorResult
from expcore.stats.prereg import load as load_checked
from expcore.stats.prereg import parse
from expcore.study.pipeline import _holm_entries, _json_text

ALPHA = 0.05
ARMS = "abcdefghij"
SCHEMA = FactorSchema(
    {"arm": list(ARMS)}, ["task", "arm", "model", "seed"], {f"cond-{arm}": {"arm": arm} for arm in ARMS}
)
NAMES = ("h1a", "h1b", "h1c", "h2a", "h2b")
PAIRS = dict(zip(NAMES, (("cond-a", "cond-b"), ("cond-c", "cond-d"), ("cond-e", "cond-f"), ("cond-g", "cond-h"),
                         ("cond-i", "cond-j")), strict=True))  # fmt: skip
FAMILIES = '[holm_families]\nphase1 = ["h1a", "h1b", "h1c"]\nphase2 = ["h2a", "h2b"]\n'
FLAT = 'holm_family = ["h1a", "h1b", "h1c", "h2a", "h2b"]\n'


def contrasts_text(estimator="planted_p", margins=None, names=NAMES):
    """One named-condition contrast per name, each on two arms of its own, with a margin where `margins` gives one."""
    text = ""
    for name in names:
        a, b = PAIRS[name]
        text += f'\n[contrasts.{name}]\nconditions = ["{a}", "{b}"]\noutcome = "score"\n'
        text += f'estimator = "{estimator}"\ncluster = "group"\n'
        if margins and name in margins:
            text += f"equivalence_margin = {margins[name]}\n"
    return text


def prereg_text(family=FAMILIES, *, top="", tables="", estimator="planted_p", margins=None, names=NAMES):
    """A preregistration with the five contrasts, declaring `family` (the named families unless told otherwise).

    `top` holds top-level keys, which TOML needs before the first table header (`family` may open one), and `tables`
    more tables, such as [power]."""
    head = f"alpha = {ALPHA}\n{top}{family}\n[bootstrap]\nreplicates = 50\nseed = 5\n{tables}"
    return head + contrasts_text(estimator, margins, names)


def parsed(text, sha="f" * 64):
    return parse(tomllib.loads(text), "inline", sha, SCHEMA)


def refusal(text):
    """The message a preregistration is refused with."""
    with pytest.raises(ConfigError) as caught:
        parsed(text)
    return str(caught.value)


@pytest.fixture
def planted_p(monkeypatch):
    """An estimator whose p-value is planted in the data: the first cluster's difference, A - B."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))

    def planted(paired, settings):
        value = float(paired.matrix[0, 0]) if paired.clusters else math.nan  # no pair: nothing to plant
        return EstimatorResult(value, value, -1.0, 1.0, len(paired.clusters))

    estimators.register("planted_p")(planted)


def rows_with_p(p_values):
    """One pair per contrast whose difference, arm A's score minus arm B's 0.0, is exactly the contrast's p-value."""
    rows = []
    for name, p in p_values.items():
        a, b = PAIRS[name]
        rows += [make_row("t0", "model-a", 1, p, condition=a), make_row("t0", "model-a", 1, 0.0, condition=b)]
    return rows


# --- loading -------------------------------------------------------------------------------------------------------


def test_s17_named_families_load_in_declared_order_and_their_union_is_the_confirmatory_set(planted_p):
    """S17: each family keeps its members in declared order, the families keep theirs, and `holm_family` is every
    family's members one after the other, so every contrast in a family is confirmatory."""
    prereg = parsed(prereg_text())
    assert dict(prereg.holm_families) == {"phase1": ("h1a", "h1b", "h1c"), "phase2": ("h2a", "h2b")}
    assert prereg.holm_family == NAMES
    assert prereg.families() == (("phase1", ("h1a", "h1b", "h1c")), ("phase2", ("h2a", "h2b")))
    assert prereg.family_of("h2b") == ("phase2", ("h2a", "h2b"))
    assert prereg.family_of("h1a") == ("phase1", ("h1a", "h1b", "h1c"))


def test_s17_one_flat_family_is_one_unnamed_family(planted_p):
    """S17: `holm_family` alone is one family with no name, as before: no named family, every member in it."""
    prereg = parsed(prereg_text(FLAT))
    assert dict(prereg.holm_families) == {}
    assert prereg.holm_family == NAMES
    assert prereg.families() == ((None, NAMES),)
    assert prereg.family_of("h2b") == (None, NAMES)


def test_s17_a_stand_alone_confirmatory_test_is_a_family_of_one(planted_p):
    """S17: a family may hold one contrast: a stand-alone confirmatory test, outside every other family."""
    text = prereg_text('[holm_families]\nphase1 = ["h1a", "h1b", "h1c"]\nsolo = ["h2a"]\n')
    prereg = parsed(text)
    assert prereg.family_of("h2a") == ("solo", ("h2a",))
    assert "h2b" not in prereg.holm_family  # declared, in no family: exploratory


@pytest.mark.parametrize(
    ("family", "key", "message"),
    [
        (FLAT + FAMILIES, "holm_families", "is given with holm_family"),
        ("[holm_families]\n", "holm_families", "is empty"),
        ('[holm_families]\nphase1 = []\nphase2 = ["h2a"]\n', "phase1", "is empty"),
        ('[holm_families]\n" " = ["h1a"]\n', " ", "is a blank family name"),
        ('[holm_families]\nphase1 = ["h1a", "nope"]\n', "phase1", "names 'nope', which is not a declared contrast"),
        ('[holm_families]\nphase1 = ["h1a", "h1a"]\n', "phase1", "names a contrast twice"),
        (
            '[holm_families]\nphase1 = ["h1a", "h1b"]\nphase2 = ["h1b", "h2a"]\n',
            "phase2",
            "names 'h1b', which family 'phase1' names too: a contrast is in one family at most",
        ),
        ('[holm_families]\nphase1 = "h1a"\n', "phase1", "must be an array of non-empty strings"),
        ('[holm_families]\nphase1 = [["h1a"], ["h1b"]]\n', "phase1", "must be an array of non-empty strings"),
        ('holm_families = ["h1a"]\n', "holm_families", "must be a table"),
    ],
)
def test_s17_a_family_that_is_not_a_set_of_declared_contrasts_each_in_one_family_is_refused(
    planted_p, family, key, message
):
    """S17: both forms at once, an empty table or family, a blank name, an unknown or repeated contrast, a contrast
    in two families and a malformed table are refused, naming the file, the table and the key."""
    found = refusal(prereg_text(family))
    assert f"key {key!r} {message}" in found
    assert found.startswith("inline: ")


def test_s17_named_families_beside_exploratory_only_are_refused(planted_p):
    """S17, S12: `exploratory_only = true` says there is no family, so a table of families beside it is refused."""
    found = refusal(prereg_text(FAMILIES, top="exploratory_only = true\n"))
    assert "key 'holm_families' is given with exploratory_only = true" in found


def test_s17_neither_form_is_refused_naming_both_and_exploratory_only(planted_p):
    """S17, S12: with no family of either form and no exploratory_only, the refusal names all three ways out."""
    found = refusal(prereg_text(""))
    assert "key 'holm_family' is required" in found
    assert "[holm_families]" in found
    assert "exploratory_only = true" in found


def test_s17_s10_a_tolerance_on_a_contrast_in_no_named_family_is_refused_naming_the_table(planted_p):
    """S17, S10: max_unscored_treatment applies only to a family member; on a contrast in no family it names
    [holm_families]."""
    arms = '[contrasts.h2a]\nconditions = ["cond-g", "cond-h"]\n'
    tolerance = 'treatment_outcomes = "scored"\nmax_unscored_treatment = 1\n'
    text = prereg_text('[holm_families]\nphase1 = ["h1a", "h1b", "h1c"]\n').replace(arms, arms + tolerance)
    assert "key 'holm_families' does not name 'h2a', whose max_unscored_treatment would do nothing" in refusal(text)


def test_s17_s15_an_equivalence_member_in_no_named_family_is_refused(planted_p):
    """S17, S15: an equivalence claim the families control is confirmatory, so its contrast must be in a family."""
    text = prereg_text(
        '[holm_families]\nphase1 = ["h1a", "h1b", "h1c"]\n',
        top='equivalence_family = ["h2a"]\n',
        estimator="wilcoxon_cluster_bootstrap",
        margins={"h2a": 0.2},
    )
    assert "key 'equivalence_family' names 'h2a', which is in no family of [holm_families]" in refusal(text)


def test_s17_a_preregistration_built_in_code_keeps_its_families_and_their_union_together(planted_p):
    """S17: a family union that disagrees with `holm_family`, a contrast in two families, or an empty family is
    refused however the preregistration is built; with `holm_family` left empty it is the families' union."""
    prereg = parsed(prereg_text())
    with pytest.raises(ValueError, match="holm_family must be the families' members in order"):
        prereg.with_override(holm_families={"phase1": ("h1a",)})
    with pytest.raises(ValueError, match="is in two families"):
        prereg.with_override(holm_families={"one": ("h1a",), "two": ("h1a",)}, holm_family=())
    with pytest.raises(ValueError, match="is empty"):
        prereg.with_override(holm_families={"one": ()}, holm_family=())
    copy = prereg.with_override(holm_families={"one": ("h2b", "h1a")}, holm_family=())
    assert (copy.holm_family, copy.sha256) == (("h2b", "h1a"), "f" * 64 + "+override")


@pytest.mark.parametrize(
    "families",
    [{"one": {"h1a", "h1b"}}, {"one": "h1a"}, {"one": {"h1a": 1}}, {"one": ("h1a", 2)}, [("one", ("h1a",))]],
    ids=["set", "string", "mapping", "not-a-name", "not-a-mapping"],
)
def test_s17_a_preregistration_built_in_code_refuses_families_that_are_not_lists_of_names(planted_p, families):
    """S17, P04: a family given as a set (which freezing would leave the caller's to change), a string (whose
    letters are not contrasts), a mapping or anything but a list of names is refused, as the loader refuses it."""
    with pytest.raises(ValueError, match="Holm famil"):
        parsed(prereg_text()).with_override(holm_families=families, holm_family=())


def test_s17_p04_the_families_enter_the_preregistration_hash_every_output_carries(planted_p, tmp_path):
    """S17, P04: the same five contrasts as two families or as one flat family are two preregistrations, with two
    hashes in every output manifest, so no output computed under one can pass for the other."""
    hashes = []
    for name, family in (("named", FAMILIES), ("flat", FLAT)):
        path = tmp_path / f"{name}.toml"
        path.write_text(prereg_text(family), encoding="utf-8")
        prereg = load_checked(path, SCHEMA)
        assert prereg.manifest()["preregistration_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(prereg.manifest()["preregistration_sha256"])
    assert hashes[0] != hashes[1]


# --- Holm within each family: known answers ------------------------------------------------------------------------

# Case A. phase1 (m = 3): 0.012 <= 0.05/3 = 0.01667, 0.020 <= 0.05/2 = 0.025, 0.040 <= 0.05/1: all rejected; adjusted
# 3 x 0.012 = 0.036, max(0.036, 2 x 0.020) = 0.040, max(0.040, 0.040) = 0.040. phase2 (m = 2): 0.015 <= 0.025,
# 0.030 <= 0.05: both rejected; adjusted 2 x 0.015 = 0.030, max(0.030, 0.030) = 0.030.
# One flat family of the five (m = 5): the smallest, 0.012, misses 0.05/5 = 0.01, so none is rejected; adjusted
# 5 x 0.012, 4 x 0.015, 3 x 0.020, 2 x 0.030, 0.040 under the running maximum: 0.060 for every member.
CASE_A = {"h1a": 0.012, "h1b": 0.020, "h1c": 0.040, "h2a": 0.015, "h2b": 0.030}
CASE_A_FAMILIES = {  # name: (family, step, level, adjusted p, rejected)
    "h1a": ("phase1", 1, ALPHA / 3, 0.036, True),
    "h1b": ("phase1", 2, ALPHA / 2, 0.040, True),
    "h1c": ("phase1", 3, ALPHA / 1, 0.040, True),
    "h2a": ("phase2", 1, ALPHA / 2, 0.030, True),
    "h2b": ("phase2", 2, ALPHA / 1, 0.030, True),
}
CASE_A_FLAT = {  # name: (step, level, adjusted p, rejected)
    "h1a": (1, ALPHA / 5, 0.060, False),
    "h2a": (2, ALPHA / 4, 0.060, False),
    "h1b": (3, ALPHA / 3, 0.060, False),
    "h2b": (4, ALPHA / 2, 0.060, False),
    "h1c": (5, ALPHA / 1, 0.060, False),
}

# Case B. phase1 (m = 3): 0.008 <= 0.01667 is rejected; 0.030 misses 0.025, so the family stops there and 0.040 is
# not rejected though it is below 0.05; adjusted 3 x 0.008 = 0.024, max(0.024, 2 x 0.030) = 0.060, max(0.060,
# 0.040) = 0.060. phase2 (m = 2): 0.020 <= 0.025, 0.045 <= 0.05: both rejected; adjusted 0.040, max(0.040, 0.045) =
# 0.045. One flat family of five: 0.008 <= 0.01 is rejected, then 0.020 misses 0.05/4 = 0.0125 and it stops; adjusted
# 5 x 0.008 = 0.040, 4 x 0.020 = 0.080, 3 x 0.030 = 0.090, max(0.090, 2 x 0.040), max(0.090, 0.045): 0.090.
CASE_B = {"h1a": 0.008, "h1b": 0.030, "h1c": 0.040, "h2a": 0.020, "h2b": 0.045}
CASE_B_FAMILIES = {
    "h1a": ("phase1", 1, ALPHA / 3, 0.024, True),
    "h1b": ("phase1", 2, ALPHA / 2, 0.060, False),
    "h1c": ("phase1", 3, ALPHA / 1, 0.060, False),
    "h2a": ("phase2", 1, ALPHA / 2, 0.040, True),
    "h2b": ("phase2", 2, ALPHA / 1, 0.045, True),
}
CASE_B_FLAT = {
    "h1a": (1, ALPHA / 5, 0.040, True),
    "h2a": (2, ALPHA / 4, 0.080, False),
    "h1b": (3, ALPHA / 3, 0.090, False),
    "h1c": (4, ALPHA / 2, 0.090, False),
    "h2b": (5, ALPHA / 1, 0.090, False),
}


def check_families(result, expected):
    """Each family's steps, levels, adjusted p-values and verdicts, and each contrast's conclusion with them."""
    families = {family.name: family for family in result.holm_families}
    assert list(families) == ["phase1", "phase2"]
    assert families["phase1"].members == ("h1a", "h1b", "h1c")
    assert families["phase2"].members == ("h2a", "h2b")
    for name, (family, step, level, adjusted, rejected) in expected.items():
        [found] = [s for s in families[family].steps if s.name == name]
        assert (found.step, found.reject) == (step, rejected), name
        assert found.level == pytest.approx(level, rel=1e-12)
        assert found.p_holm == pytest.approx(adjusted, rel=1e-12)
        contrast = result.contrast(name)
        assert (contrast.in_family, contrast.exploratory, contrast.significant) == (True, False, rejected)
        assert contrast.conclusion == ("difference" if rejected else "not_detected")
    for family in families.values():  # the steps run from the smallest p-value up
        assert [s.step for s in family.steps] == list(range(1, len(family.members) + 1))


def check_flat(result, expected):
    """The one flat family's decisions, with no named family."""
    assert result.holm_families == ()
    decisions = {d.name: d for d in result.holm}
    for name, (_, level, adjusted, rejected) in expected.items():
        assert decisions[name].reject is rejected, name
        assert decisions[name].level == pytest.approx(level, rel=1e-12)
        assert decisions[name].p_holm == pytest.approx(adjusted, rel=1e-12)
        assert result.contrast(name).significant is rejected


@pytest.mark.parametrize(
    ("p_values", "families", "flat"),
    [(CASE_A, CASE_A_FAMILIES, CASE_A_FLAT), (CASE_B, CASE_B_FAMILIES, CASE_B_FLAT)],
    ids=["all-rejected-in-families-none-flat", "a-family-stops-and-the-flat-family-stops-sooner"],
)
def test_s17_each_family_is_adjusted_on_its_own_and_one_flat_family_of_the_five_decides_otherwise(
    planted_p, p_values, families, flat
):
    """S17: the same p-values under two families (alpha/3, alpha/2, alpha/1 and alpha/2, alpha/1) and under one flat
    family of five (alpha/5 to alpha/1) give the hand-worked levels, adjusted p-values and verdicts of each, and the
    two designs reject different contrasts."""
    rows = rows_with_p(p_values)
    named = analyse(rows, parsed(prereg_text()))
    merged = analyse(rows, parsed(prereg_text(FLAT)))
    for name, p in p_values.items():
        assert named.contrast(name).p_value == p  # planted exactly
    check_families(named, families)
    check_flat(merged, flat)
    assert {n for n, f in families.items() if f[4]} != {n for n, f in flat.items() if f[3]}


def test_s17_the_flat_holm_decisions_are_each_familys_in_declared_order(planted_p):
    """S17: `holm` holds every member's decision as its own family made it, the families one after the other, so
    whatever reads it by name finds the level and the adjusted p of the member's own family."""
    result = analyse(rows_with_p(CASE_B), parsed(prereg_text()))
    assert [d.name for d in result.holm] == list(NAMES)
    steps = {s.name: s for family in result.holm_families for s in family.steps}
    for decision in result.holm:
        step = steps[decision.name]
        assert (decision.p_value, decision.p_holm, decision.level, decision.reject) == (
            step.p_value,
            step.p_holm,
            step.level,
            step.reject,
        )


def test_s17_a_family_of_one_is_judged_at_alpha_and_is_confirmatory(planted_p):
    """S17: a stand-alone test at p = 0.04, a family of one, is compared with alpha / 1 and rejected, and stays
    confirmatory; a contrast in no family stays exploratory."""
    text = prereg_text('[holm_families]\nphase1 = ["h1a", "h1b"]\nsolo = ["h2a"]\n')
    result = analyse(rows_with_p({**CASE_A, "h2a": 0.04}), parsed(text))
    solo = next(family for family in result.holm_families if family.name == "solo")
    assert solo.steps == (HolmStep(1, "h2a", 0.04, 0.04, ALPHA, True),)
    assert (result.contrast("h2a").exploratory, result.contrast("h2a").conclusion) == (False, "difference")
    for name in ("h1c", "h2b"):
        assert result.contrast(name).exploratory
    assert {d.name for d in result.holm} == {"h1a", "h1b", "h2a"}


def test_s17_s02_an_untestable_member_keeps_its_place_in_its_own_family_only(planted_p):
    """S17, S02: a member with no pair counts as p = 1 in its own family, whose size it keeps; the other family is
    untouched by it."""
    rows = rows_with_p({name: p for name, p in CASE_A.items() if name != "h2b"})
    result = analyse(rows, parsed(prereg_text()))
    phase2 = next(family for family in result.holm_families if family.name == "phase2")
    by_name = {s.name: s for s in phase2.steps}
    assert by_name["h2a"].level == ALPHA / 2  # m stays 2
    assert (by_name["h2b"].reject, math.isnan(by_name["h2b"].p_value)) == (False, True)
    assert result.contrast("h2b").conclusion == "untestable"
    phase1 = next(family for family in result.holm_families if family.name == "phase1")
    assert all(s.reject for s in phase1.steps)


def test_s17_analysis_json_gives_each_family_its_members_and_steps_and_one_flat_family_gives_no_such_key(planted_p):
    """S17: the JSON of the decisions holds `holm` as before and, only for named families, `holm_families`: each
    family's name, members, and steps with the raw and adjusted p-value, the level and the verdict."""
    named = json.loads(_json_text(_holm_entries(analyse(rows_with_p(CASE_B), parsed(prereg_text())))))
    assert [d["name"] for d in named["holm"]] == list(NAMES)
    phase1, phase2 = named["holm_families"]
    assert (phase1["name"], phase1["members"]) == ("phase1", ["h1a", "h1b", "h1c"])
    assert phase1["steps"][1] == {
        "step": 2,
        "name": "h1b",
        "p_value": 0.03,
        "p_holm": pytest.approx(0.06),
        "level": ALPHA / 2,
        "reject": False,
    }
    assert (phase2["name"], [s["reject"] for s in phase2["steps"]]) == ("phase2", [True, True])
    assert phase1["equivalence"] == []
    flat = json.loads(_json_text(_holm_entries(analyse(rows_with_p(CASE_B), parsed(prereg_text(FLAT))))))
    assert list(flat) == ["holm"]


# --- equivalence under several families (S15) ----------------------------------------------------------------------


@pytest.fixture
def planted_tost(monkeypatch):
    """The Wilcoxon cluster bootstrap with a TOST p planted: the first cluster's difference."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    wilcoxon, real = estimators.get(estimators.WILCOXON), estimators.tost_function(estimators.WILCOXON)
    planted = estimators.Tost(lambda paired, settings, margin: paired.matrix[0, 0], real.interval, real.p_values)
    estimators.register("planted_tost", check_params=lambda params: None, tost=planted)(wilcoxon)


def rows_with_tost(tost_p):
    """Eight clusters per contrast within 0.01 of 0, the first cluster's difference set to the contrast's TOST p."""
    rows = []
    for name, p in tost_p.items():
        a, b = PAIRS[name]
        for i in range(8):
            difference = p if i == 0 else 0.01 * (1 if i % 2 else -1) * (i + 1) / 8
            rows.append(make_row(f"t{i}", "model-a", 1, 0.5 + difference, condition=a, cluster=f"c{i}"))
            rows.append(make_row(f"t{i}", "model-a", 1, 0.5, condition=b, cluster=f"c{i}"))
    return rows


def equivalence_text(family, equivalence, estimator="planted_tost", power=""):
    """The first three contrasts with a margin of 0.2 each, declaring `family` and `equivalence` as their families."""
    names = ("h1a", "h1b", "h2a")
    top = f"equivalence_family = {equivalence}\n"
    margins = dict.fromkeys(names, 0.2)
    return prereg_text(family, top=top, tables=power, estimator=estimator, margins=margins, names=names)


TWO_BY_ONE = '[holm_families]\nphase1 = ["h1a", "h1b"]\nphase2 = ["h2a"]\n'


def test_s17_s15_each_equivalence_claim_is_adjusted_within_its_own_holm_family(planted_tost):
    """S17, S15: TOST p 0.04 for h1a and h2a, each alone among its family's equivalence members, is judged at
    alpha / 1 and is equivalent; the same two claims in one flat Holm family face alpha / 2 first, and neither is."""
    rows = rows_with_tost({"h1a": 0.04, "h1b": 0.3, "h2a": 0.04})
    named = analyse(rows, parsed(equivalence_text(TWO_BY_ONE, '["h2a", "h1a"]')))
    for name in ("h1a", "h2a"):
        contrast = named.contrast(name)
        assert contrast.tost_p == pytest.approx(0.04)
        assert (contrast.equivalent, contrast.conclusion, contrast.equivalence_level) == (
            True,
            "equivalent",
            pytest.approx(1 - 2 * ALPHA),
        )
    assert [d.name for d in named.equivalence_holm] == ["h2a", "h1a"]  # the equivalence family's declared order
    families = {family.name: family for family in named.holm_families}
    assert [(d.name, d.level, d.reject) for d in families["phase1"].equivalence] == [("h1a", ALPHA, True)]
    assert [(d.name, d.level, d.reject) for d in families["phase2"].equivalence] == [("h2a", ALPHA, True)]
    flat = analyse(rows, parsed(equivalence_text('holm_family = ["h1a", "h1b", "h2a"]\n', '["h2a", "h1a"]')))
    assert [flat.contrast(name).equivalent for name in ("h1a", "h2a")] == [False, False]
    assert sorted(d.level for d in flat.equivalence_holm) == [ALPHA / 2, ALPHA]


def test_s17_s15_two_equivalence_claims_of_one_family_are_adjusted_together_and_apart_from_the_others(planted_tost):
    """S17, S15: h1a and h1b share phase1, so their TOST p-values 0.04 and 0.04 face alpha / 2 first and neither is
    equivalent; h2a, alone in phase2, is equivalent at 0.04."""
    rows = rows_with_tost({"h1a": 0.04, "h1b": 0.04, "h2a": 0.04})
    result = analyse(rows, parsed(equivalence_text(TWO_BY_ONE, '["h1a", "h1b", "h2a"]')))
    assert [result.contrast(name).equivalent for name in ("h1a", "h1b", "h2a")] == [False, False, True]
    phase1 = next(family for family in result.holm_families if family.name == "phase1")
    assert sorted(d.level for d in phase1.equivalence) == [ALPHA / 2, ALPHA]


def test_s17_s15_the_feasibility_of_a_claim_is_judged_at_alpha_over_its_familys_equivalence_claims():
    """S17, S15: the simulated level of each claim is alpha over the equivalence claims of its own Holm family."""
    power = "\n[power]\nseed = 3\nn_simulations = 20\ntarget_clusters = 10\ntarget_difference = 0.1\nsd_grid = [0.05]\n"
    text = equivalence_text(TWO_BY_ONE, '["h1a", "h1b", "h2a"]', estimator="wilcoxon_cluster_bootstrap", power=power)
    rows = equivalence_feasibility(parsed(text)).rows
    assert {row["contrast"]: row["alpha"] for row in rows} == {"h1a": ALPHA / 2, "h1b": ALPHA / 2, "h2a": ALPHA}


# --- the power settings under several families -----------------------------------------------------------------------

POWER = "\n[power]\nseed = 3\nn_simulations = 20\ntarget_clusters = 10\ntarget_difference = 0.1\nsd_grid = [0.1]\n"


def test_s17_the_power_table_is_at_alpha_over_the_largest_family(planted_p):
    """S17: the power table's corrected row is at alpha over the size of the largest family, the hardest first step
    any family has; a flat family of five is at alpha / 5."""
    named = power_study(parsed(prereg_text(tables=POWER)), simulate=False).rows
    assert [row["alpha"] for row in named] == [ALPHA, ALPHA / 3]
    flat = power_study(parsed(prereg_text(FLAT, tables=POWER)), simulate=False).rows
    assert [row["alpha"] for row in flat] == [ALPHA, ALPHA / 5]


def spread_rows(names):
    """Eight clusters per contrast with distinct differences around 0.1, so a minimum detectable effect is found."""
    rows = []
    for name in names:
        a, b = PAIRS[name]
        for i in range(8):
            rows.append(make_row(f"t{i}", "model-a", 1, 0.5 + 0.1 + 0.02 * i, condition=a, cluster=f"c{i}"))
            rows.append(make_row(f"t{i}", "model-a", 1, 0.5, condition=b, cluster=f"c{i}"))
    return rows


def test_s17_a_members_minimum_detectable_effect_is_at_alpha_over_its_own_familys_size():
    """S17: h2a in a family of two among five confirmatory contrasts has the minimum detectable effect it has in a
    flat family of two, and not the one of a flat family of five."""
    rows = spread_rows(NAMES)
    estimator = "wilcoxon_cluster_bootstrap"
    named = analyse(rows, parsed(prereg_text(tables=POWER, estimator=estimator))).contrast("h2a")
    two = prereg_text('holm_family = ["h2a", "h2b"]\n', tables=POWER, estimator=estimator)
    pair = analyse(rows, parsed(two)).contrast("h2a")
    five = analyse(rows, parsed(prereg_text(FLAT, tables=POWER, estimator=estimator))).contrast("h2a")
    assert np.isfinite(named.minimum_detectable_effect)
    assert named.minimum_detectable_effect == pair.minimum_detectable_effect
    assert named.minimum_detectable_effect < five.minimum_detectable_effect
