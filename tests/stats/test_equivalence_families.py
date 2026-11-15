"""Declared equivalence families (S18): loading, refusals, and Holm over each family's own TOST p-values.

The known answers below are worked out by hand at alpha = 0.05. A family of m orders its members by TOST p-value and
compares the k-th smallest with alpha / (m - k + 1), stopping at the first that misses; the adjusted p of the k-th is
the running maximum of (m - k + 1) x p, capped at 1. Every TOST p is planted exactly (arm A's outcome is it, arm B's is
0.0) and chosen clear of every level, so no case rests on a p-value that lands on its level (S06).
"""

import hashlib
import json
import math
import tomllib

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.config.loading import ConfigError
from expcore.report import render_report, summary_lines
from expcore.stats import estimators
from expcore.stats.analysis import EquivalenceFamily, HolmStep, analyse, equivalence_feasibility
from expcore.stats.estimators import EstimatorResult
from expcore.stats.prereg import load as load_checked
from expcore.stats.prereg import parse
from expcore.stats.rows import Row
from expcore.study.pipeline import _holm_entries, _json_text

ALPHA = 0.05
SCHEMA = FactorSchema({"arm": ["a", "b"]}, ["task", "arm", "model", "seed"], {f"cond-{a}": {"arm": a} for a in "ab"})
PHASES = {f"phase{k}": tuple(f"h{k}{x}" for x in "abc") for k in range(1, 5)}
NAMES = tuple(name for members in PHASES.values() for name in members)  # h1a, h1b, h1c, h2a, ..., h4c


def listed(names):
    return "[" + ", ".join(f'"{name}"' for name in names) + "]"


FOUR_BY_THREE = "[holm_families]\n" + "".join(f"{family} = {listed(m)}\n" for family, m in PHASES.items())
FLAT_HOLM = f"holm_family = {listed(NAMES)}\n"
TWELVE = f"[equivalence_families]\nh1 = {listed(NAMES)}\n"
FLAT_EQUIVALENCE = f"equivalence_family = {listed(NAMES)}\n"

# The main case. Four Holm families of three significance tests; twelve equivalence claims, one per contrast.
TOST_P = {
    "h1a": 0.001, "h1b": 0.012, "h1c": 0.030,
    "h2a": 0.002, "h2b": 0.020, "h2c": 0.040,
    "h3a": 0.003, "h3b": 0.015, "h3c": 0.200,
    "h4a": 0.004, "h4b": 0.300, "h4c": 0.500,
}  # fmt: skip
# One equivalence family of the twelve (m = 12), the TOST p-values from the smallest up:
#   step 1  h1a 0.001 <= 0.05/12 = 0.004167, rejected; adjusted 12 x 0.001 = 0.012
#   step 2  h2a 0.002 <= 0.05/11 = 0.004545, rejected; adjusted 11 x 0.002 = 0.022
#   step 3  h3a 0.003 <= 0.05/10 = 0.005,    rejected; adjusted 10 x 0.003 = 0.030
#   step 4  h4a 0.004 <= 0.05/9  = 0.005556, rejected; adjusted  9 x 0.004 = 0.036
#   step 5  h1b 0.012 >  0.05/8  = 0.00625: Holm stops, and this and every later claim is not rejected;
#           adjusted 8 x 0.012 = 0.096, then 7 x 0.015 = 0.105, 6 x 0.020 = 0.120, 5 x 0.030 = 0.150,
#           4 x 0.040 = 0.160, 3 x 0.200 = 0.600, max(0.600, 2 x 0.300) = 0.600, max(0.600, 0.500) = 0.600
TWELVE_STEPS = (  # step, name, TOST p, level, adjusted, equivalent
    (1, "h1a", 0.001, ALPHA / 12, 0.012, True),
    (2, "h2a", 0.002, ALPHA / 11, 0.022, True),
    (3, "h3a", 0.003, ALPHA / 10, 0.030, True),
    (4, "h4a", 0.004, ALPHA / 9, 0.036, True),
    (5, "h1b", 0.012, ALPHA / 8, 0.096, False),
    (6, "h3b", 0.015, ALPHA / 7, 0.105, False),
    (7, "h2b", 0.020, ALPHA / 6, 0.120, False),
    (8, "h1c", 0.030, ALPHA / 5, 0.150, False),
    (9, "h2c", 0.040, ALPHA / 4, 0.160, False),
    (10, "h3c", 0.200, ALPHA / 3, 0.600, False),
    (11, "h4b", 0.300, ALPHA / 2, 0.600, False),
    (12, "h4c", 0.500, ALPHA / 1, 0.600, False),
)
# The same claims as one flat equivalence_family, adjusted within each Holm family of three (#277: m = 3 each):
#   phase1: 0.001 <= 0.05/3 = 0.01667, 0.012 <= 0.025, 0.030 <= 0.05: all rejected; adjusted 3 x 0.001 = 0.003,
#           max(0.003, 2 x 0.012) = 0.024, max(0.024, 0.030) = 0.030
#   phase2: 0.002, 0.020 <= 0.025, 0.040 <= 0.05: all rejected; adjusted 0.006, 0.040, max(0.040, 0.040) = 0.040
#   phase3: 0.003, 0.015 <= 0.025 rejected; 0.200 > 0.05 not; adjusted 0.009, 0.030, 0.200
#   phase4: 0.004 rejected; 0.300 > 0.025 stops it, so 0.500 is not either; adjusted 0.012, 0.600, 0.600
WITHIN_HOLM = {  # name: (level, adjusted, equivalent)
    "h1a": (ALPHA / 3, 0.003, True), "h1b": (ALPHA / 2, 0.024, True), "h1c": (ALPHA / 1, 0.030, True),
    "h2a": (ALPHA / 3, 0.006, True), "h2b": (ALPHA / 2, 0.040, True), "h2c": (ALPHA / 1, 0.040, True),
    "h3a": (ALPHA / 3, 0.009, True), "h3b": (ALPHA / 2, 0.030, True), "h3c": (ALPHA / 1, 0.200, False),
    "h4a": (ALPHA / 3, 0.012, True), "h4b": (ALPHA / 2, 0.600, False), "h4c": (ALPHA / 1, 0.600, False),
}  # fmt: skip


@pytest.fixture
def planted(monkeypatch):
    """`planted_tost`: an estimator whose TOST p is planted in the data (the first cluster's difference) and whose
    significance p is 0.5 for every contrast, so no significance test is rejected; `no_tost` registers no TOST."""
    monkeypatch.setattr(estimators, "_REGISTRY", dict(estimators._REGISTRY))
    real = estimators.tost_function(estimators.WILCOXON)

    def estimate(paired, settings):
        value = float(paired.matrix[0, 0]) if paired.clusters else math.nan
        return EstimatorResult(value, 0.5, -1.0, 1.0, len(paired.clusters))

    tost = estimators.Tost(lambda paired, settings, margin: float(paired.matrix[0, 0]), real.interval, real.p_values)
    estimators.register("planted_tost", tost=tost)(estimate)
    estimators.register("no_tost")(estimate)


def contrast_text(name, estimator="planted_tost", margin=0.2):
    """A contrast of cond-a against cond-b reading its own outcome, named after it."""
    text = f'\n[contrasts.{name}]\nconditions = ["cond-a", "cond-b"]\noutcome = "{name}"\n'
    text += f'estimator = "{estimator}"\ncluster = "group"\n'
    return text + ("" if margin is None else f"equivalence_margin = {margin}\n")


def prereg_text(holm=FOUR_BY_THREE, equivalence=TWELVE, *, top="", extra=""):
    """The twelve contrasts, each with a margin of 0.2, declaring `holm` and `equivalence` as their families.

    `top` holds more top-level keys, which TOML needs before the first table header, and `extra` more contrasts or
    tables after the twelve."""
    flat = [part for part in (holm, equivalence) if not part.startswith("[")]
    tables = [part for part in (holm, equivalence) if part.startswith("[")]
    head = f"alpha = {ALPHA}\n{top}{''.join(flat)}{''.join(tables)}\n[bootstrap]\nreplicates = 50\nseed = 5\n"
    return head + "".join(contrast_text(name) for name in NAMES) + extra


def parsed(text, sha="f" * 64):
    return parse(tomllib.loads(text), "inline", sha, SCHEMA)


def refusal(text):
    """The message a preregistration is refused with."""
    with pytest.raises(ConfigError) as caught:
        parsed(text)
    return str(caught.value)


def rows_with_tost(tost_p):
    """One pair, in one cluster: arm A's outcome of each contrast is its planted TOST p, arm B's is 0.0."""
    factors = {"task": "t0", "model": "model-a", "seed": 1}
    return [
        Row(f"task=t0|model=model-a|seed=1|condition={arm}", arm, factors, outcomes, {"group": "c0"})
        for arm, outcomes in (("cond-a", dict(tost_p)), ("cond-b", dict.fromkeys(tost_p, 0.0)))
    ]


# --- Holm over each equivalence family: known answers --------------------------------------------------------------


def test_s18_twelve_claims_across_four_holm_families_are_one_family_whose_first_step_is_alpha_over_12(planted):
    """S18: the twelve claims of [equivalence_families] h1, spanning four Holm families of three, are adjusted over
    the twelve: alpha/12, alpha/11, ... alpha/1, the hand-worked adjusted p-values above, and Holm stops at the fifth
    step, so only h1a, h2a, h3a and h4a are equivalent. No Holm family holds an equivalence decision."""
    result = analyse(rows_with_tost(TOST_P), parsed(prereg_text()))
    [family] = result.equivalence_families
    assert (family.name, family.members) == ("h1", NAMES)
    assert family.steps[0].level == ALPHA / 12
    for found, (step, name, tost, level, adjusted, equivalent) in zip(family.steps, TWELVE_STEPS, strict=True):
        assert (found.step, found.name, found.p_value, found.reject) == (step, name, tost, equivalent)
        assert found.level == pytest.approx(level, rel=1e-12)
        assert found.p_holm == pytest.approx(adjusted, rel=1e-12)
        contrast = result.contrast(name)
        assert (contrast.in_equivalence_family, contrast.tost_p, contrast.equivalent) == (True, tost, equivalent)
        assert contrast.tost_p_holm == pytest.approx(adjusted, rel=1e-12)
        assert contrast.equivalence_level == pytest.approx(1 - 2 * level, rel=1e-12)
        assert contrast.conclusion == ("equivalent" if equivalent else "not_detected")  # no test is significant
    assert [d.name for d in result.equivalence_holm] == list(NAMES)  # the families' members in declared order
    assert [f.equivalence for f in result.holm_families] == [(), (), (), ()]
    assert [f.name for f in result.holm_families] == list(PHASES)


def test_s18_the_same_claims_without_the_table_are_adjusted_within_each_holm_family_at_alpha_over_3(planted):
    """S18, S17: the same twelve claims as one flat equivalence_family keep #277's adjustment within each Holm family
    of three (alpha/3 first), which declares nine equivalent where the one family of twelve declares four."""
    rows = rows_with_tost(TOST_P)
    within = analyse(rows, parsed(prereg_text(equivalence=FLAT_EQUIVALENCE)))
    assert within.equivalence_families == ()
    for name, (level, adjusted, equivalent) in WITHIN_HOLM.items():
        contrast = within.contrast(name)
        assert contrast.equivalent is equivalent, name
        assert contrast.tost_p_holm == pytest.approx(adjusted, rel=1e-12)
        assert contrast.equivalence_level == pytest.approx(1 - 2 * level, rel=1e-12)
    for family in within.holm_families:
        assert [d.name for d in family.equivalence] == list(PHASES[family.name])
        assert sorted(d.level for d in family.equivalence) == pytest.approx([ALPHA / 3, ALPHA / 2, ALPHA])
    declared = analyse(rows, parsed(prereg_text()))
    twelve = {name for name in NAMES if declared.contrast(name).equivalent}
    assert twelve == {"h1a", "h2a", "h3a", "h4a"}
    assert {name for name in NAMES if within.contrast(name).equivalent} == twelve | {"h1b", "h1c", "h2b", "h2c", "h3b"}


TWO_FAMILIES = '[equivalence_families]\noutcomes = ["h1b", "h2b"]\nstates = ["h1c", "h2c", "h3b"]\n'


def test_s18_each_equivalence_family_is_adjusted_over_its_own_members_only(planted):
    """S18: two equivalence families under one flat Holm family of twelve. outcomes (m = 2): 0.012 <= 0.025 and
    0.020 <= 0.05, both rejected; adjusted 0.024, max(0.024, 0.020) = 0.024. states (m = 3): h3b 0.015 <= 0.01667
    rejected (adjusted 0.045); h1c 0.030 > 0.025 stops it (adjusted 0.060), so h2c at 0.040, below alpha, is not
    equivalent (adjusted max(0.060, 0.040) = 0.060). The five as one flat equivalence_family (m = 5) reject none:
    0.012 > 0.05/5 = 0.01, every adjusted p 0.060. A contrast with a margin in no equivalence family is no claim."""
    rows = rows_with_tost(TOST_P)
    result = analyse(rows, parsed(prereg_text(FLAT_HOLM, TWO_FAMILIES)))
    families = {family.name: family for family in result.equivalence_families}
    assert list(families) == ["outcomes", "states"]
    assert families["outcomes"].steps == (
        HolmStep(1, "h1b", 0.012, pytest.approx(0.024), ALPHA / 2, True),
        HolmStep(2, "h2b", 0.020, pytest.approx(0.024), ALPHA / 1, True),
    )
    assert families["states"].members == ("h1c", "h2c", "h3b")
    assert families["states"].steps == (
        HolmStep(1, "h3b", 0.015, pytest.approx(0.045), ALPHA / 3, True),
        HolmStep(2, "h1c", 0.030, pytest.approx(0.060), ALPHA / 2, False),
        HolmStep(3, "h2c", 0.040, pytest.approx(0.060), ALPHA / 1, False),
    )
    claims = ("h1b", "h2b", "h3b", "h1c", "h2c")
    assert [result.contrast(name).equivalent for name in claims] == [True, True, True, False, False]
    assert (result.contrast("h1a").in_equivalence_family, result.contrast("h1a").tost_p) == (False, None)
    one = f"equivalence_family = {listed(['h1b', 'h2b', 'h1c', 'h2c', 'h3b'])}\n"
    flat = analyse(rows, parsed(prereg_text(FLAT_HOLM, one)))
    assert [flat.contrast(name).equivalent for name in claims] == [False] * 5
    assert {round(d.p_holm, 12) for d in flat.equivalence_holm} == {0.06}


def test_s18_s02_an_untestable_claim_keeps_its_place_in_its_equivalence_family(planted):
    """S18, S02: a claim with no pair counts as p = 1 in its equivalence family, which keeps its size (twelve), so
    h1a is still judged at alpha/12; it is never equivalent."""
    rows = rows_with_tost(TOST_P)
    for row in rows:
        row.outcomes["h4c"] = None
    [family] = analyse(rows, parsed(prereg_text())).equivalence_families
    assert family.steps[0] == HolmStep(1, "h1a", 0.001, pytest.approx(0.012), ALPHA / 12, True)
    last = family.steps[-1]
    assert (last.name, math.isnan(last.p_value), last.level, last.reject) == ("h4c", True, ALPHA, False)


def test_s18_the_feasibility_of_each_claim_is_judged_at_alpha_over_its_equivalence_familys_size():
    """S18, S15: each of the twelve claims is simulated at alpha/12, Holm's first step in its declared family;
    without the table, at alpha/3, the first step within each Holm family of three."""
    power = "\n[power]\nseed = 3\nn_simulations = 20\ntarget_clusters = 10\ntarget_difference = 0.1\nsd_grid = [0.05]\n"
    real = prereg_text(extra=power).replace("planted_tost", "wilcoxon_cluster_bootstrap")
    rows = equivalence_feasibility(parsed(real)).rows
    assert {row["contrast"]: row["alpha"] for row in rows} == dict.fromkeys(NAMES, ALPHA / 12)
    within = prereg_text(equivalence=FLAT_EQUIVALENCE, extra=power)
    rows = equivalence_feasibility(parsed(within.replace("planted_tost", "wilcoxon_cluster_bootstrap"))).rows
    assert {row["contrast"]: row["alpha"] for row in rows} == dict.fromkeys(NAMES, ALPHA / 3)


# --- loading ---------------------------------------------------------------------------------------------------------


def test_s18_declared_equivalence_families_load_in_order_and_their_union_is_the_equivalence_family(planted):
    """S18: each family keeps its members in declared order, the families keep theirs, and `equivalence_family` is
    every family's members one after the other, so every claim is decided by TOST."""
    prereg = parsed(prereg_text(FLAT_HOLM, TWO_FAMILIES))
    assert dict(prereg.equivalence_families) == {"outcomes": ("h1b", "h2b"), "states": ("h1c", "h2c", "h3b")}
    assert prereg.equivalence_family == ("h1b", "h2b", "h1c", "h2c", "h3b")
    assert dict(parsed(prereg_text(equivalence=FLAT_EQUIVALENCE)).equivalence_families) == {}


LONER = contrast_text("loner")


@pytest.mark.parametrize(
    ("holm", "equivalence", "extra", "key", "message"),
    [
        # a member outside every Holm family, under named families and under one flat family
        (FOUR_BY_THREE, '[equivalence_families]\nh1 = ["h1a", "loner"]\n', LONER, "h1", "names 'loner', which is in "
         "no family of [holm_families]: an equivalence claim the family controls is confirmatory"),
        (FLAT_HOLM, '[equivalence_families]\nh1 = ["h1a", "loner"]\n', LONER, "h1", "names 'loner', which is not in "
         "holm_family"),
        (FOUR_BY_THREE, '[equivalence_families]\nh1 = ["h1a", "nope"]\n', "", "h1", "names 'nope', which is not a "
         "declared contrast"),
        (FOUR_BY_THREE, "[equivalence_families]\nh1 = []\n", "", "h1", "is empty"),
        (FOUR_BY_THREE, "[equivalence_families]\n", "", "equivalence_families", "is empty"),
        (FOUR_BY_THREE, '[equivalence_families]\n" " = ["h1a"]\n', "", " ", "is a blank family name"),
        (FOUR_BY_THREE, '[equivalence_families]\nh1 = ["h1a", "h1a"]\n', "", "h1", "names a contrast twice"),
        (FOUR_BY_THREE, '[equivalence_families]\nh1 = ["h1a", "h1b"]\nh2 = ["h2a", "h1b"]\n', "", "h2",
         "names 'h1b', which equivalence family 'h1' names too: a claim is in one equivalence family at most"),
        (FOUR_BY_THREE, '[equivalence_families]\nh1 = "h1a"\n', "", "h1", "must be an array of non-empty strings"),
        (FOUR_BY_THREE, 'equivalence_families = ["h1a"]\n', "", "equivalence_families", "must be a table"),
    ],
    ids=["outside-named", "outside-flat", "unknown", "empty-family", "empty-table", "blank", "twice", "in-two",
         "not-an-array", "not-a-table"],
)  # fmt: skip
def test_s18_an_equivalence_family_that_is_not_a_set_of_confirmatory_claims_each_in_one_family_is_refused(
    planted, holm, equivalence, extra, key, message
):
    """S18: a member in no Holm family (its significance test must be confirmatory), an unknown contrast, an empty
    family or table, a blank name, a contrast twice or in two families, and a malformed table are refused, naming
    the file, the table and the family."""
    found = refusal(prereg_text(holm, equivalence, extra=extra))
    assert f"key {key!r} {message}" in found
    assert found.startswith("inline: ")


def with_bare(text):
    """`text` with a contrast `bare` in a Holm family of its own and first in equivalence family h1."""
    text = text.replace(FOUR_BY_THREE, FOUR_BY_THREE + 'p5 = ["bare"]\n')
    return text.replace('h1 = ["h1a"', 'h1 = ["bare", "h1a"')


def test_s18_a_member_without_an_equivalence_margin_is_refused(planted):
    """S18: a claim needs bounds to be equivalent within."""
    text = with_bare(prereg_text(extra=contrast_text("bare", margin=None)))
    assert "key 'h1' names 'bare', which declares no equivalence_margin to be equivalent within" in refusal(text)


def test_s18_a_member_whose_estimator_registers_no_tost_is_refused(planted):
    """S18: a claim is decided by its estimator's two one-sided tests, so an estimator without them is refused."""
    text = with_bare(prereg_text(extra=contrast_text("bare", estimator="no_tost")))
    assert "key 'h1' names 'bare', whose estimator 'no_tost' registers no TOST p-value" in refusal(text)


def test_s18_the_flat_equivalence_family_and_the_table_together_are_refused_naming_both(planted):
    """S18: one flat list adjusted within each Holm family, or named families each adjusted over its own members:
    both at once would leave which adjustment holds to a guess."""
    found = refusal(prereg_text(top=FLAT_EQUIVALENCE))
    assert "key 'equivalence_families' is given with equivalence_family" in found
    assert "not both" in found


def test_s18_the_table_beside_exploratory_only_is_refused(planted):
    """S18, S12: `exploratory_only = true` makes no confirmatory claim, of equivalence or other."""
    found = refusal(prereg_text("", top="exploratory_only = true\n"))
    assert "key 'equivalence_families' is given with exploratory_only = true" in found


def test_s18_a_preregistration_built_in_code_keeps_its_equivalence_families_and_their_union_together(planted):
    """S18: a union that disagrees with `equivalence_family`, a contrast in two families, an empty family, a member
    outside the Holm families and a family that is not a list of names are refused however the preregistration is
    built; with `equivalence_family` left empty it is the families' union."""
    prereg = parsed(prereg_text())
    with pytest.raises(ValueError, match="equivalence_family must be the equivalence families' members in order"):
        prereg.with_override(equivalence_families={"one": ("h1a",)})
    with pytest.raises(ValueError, match="is in two equivalence families"):
        prereg.with_override(equivalence_families={"one": ("h1a",), "two": ("h1a",)}, equivalence_family=())
    with pytest.raises(ValueError, match="equivalence family 'one' is empty"):
        prereg.with_override(equivalence_families={"one": ()}, equivalence_family=())
    with pytest.raises(ValueError, match="'h1a' must be in the Holm family"):
        prereg.with_override(holm_families={"phase2": PHASES["phase2"]}, holm_family=())
    with pytest.raises(ValueError, match="equivalence famil"):
        prereg.with_override(equivalence_families={"one": {"h1a"}}, equivalence_family=())
    copy = prereg.with_override(equivalence_families={"one": ("h2b", "h1a")}, equivalence_family=())
    assert (copy.equivalence_family, copy.sha256) == (("h2b", "h1a"), "f" * 64 + "+override")


def test_s18_p04_the_equivalence_families_enter_the_preregistration_hash_every_output_carries(planted, tmp_path):
    """S18, P04: the same twelve claims as one declared family or as one flat list are two preregistrations, with
    two hashes in every output manifest."""
    hashes = []
    for name, equivalence in (("declared", TWELVE), ("flat", FLAT_EQUIVALENCE)):
        path = tmp_path / f"{name}.toml"
        path.write_text(prereg_text(equivalence=equivalence), encoding="utf-8")
        prereg = load_checked(path, SCHEMA)
        assert prereg.manifest()["preregistration_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(prereg.manifest()["preregistration_sha256"])
    assert hashes[0] != hashes[1]


# --- analysis.json -------------------------------------------------------------------------------------------------


def test_s18_analysis_json_gives_each_equivalence_family_its_members_and_steps(planted):
    """S18: `equivalence_families` holds each family's name, members and steps (raw and adjusted TOST p, level,
    verdict); each Holm family's `equivalence` is empty, since no Holm family adjusted a claim."""
    data = json.loads(_json_text(_holm_entries(analyse(rows_with_tost(TOST_P), parsed(prereg_text())))))
    [family] = data["equivalence_families"]
    assert (family["name"], family["members"]) == ("h1", list(NAMES))
    assert family["steps"][0] == {
        "step": 1,
        "name": "h1a",
        "p_value": 0.001,
        "p_holm": pytest.approx(0.012),
        "level": ALPHA / 12,
        "reject": True,
    }
    assert [s["name"] for s in family["steps"]] == [step[1] for step in TWELVE_STEPS]
    assert [f["equivalence"] for f in data["holm_families"]] == [[], [], [], []]


def test_s18_without_the_table_analysis_json_has_no_equivalence_families_key(planted):
    """S18: a flat equivalence_family writes no such key, and each Holm family holds its own claims as before."""
    result = analyse(rows_with_tost(TOST_P), parsed(prereg_text(equivalence=FLAT_EQUIVALENCE)))
    data = json.loads(_json_text(_holm_entries(result)))
    assert list(data) == ["holm", "holm_families"]
    assert [len(f["equivalence"]) for f in data["holm_families"]] == [3, 3, 3, 3]


EXPORT = {  # the least export the report reads: no run, so only the contrasts' sections carry anything
    "planned_runs": 0,
    "exhausted_runs": [],
    "unfinished_runs": [],
    "analysed_attempts": [],
    "infrastructure_by_condition": [],
    "retries_by_kind": [],
    "budget_holds": [],
}


@pytest.mark.parametrize("holm", [FOUR_BY_THREE, FLAT_HOLM], ids=["named-holm-families", "flat-holm-family"])
def test_s18_the_report_accepts_what_the_analysis_made_and_shows_alpha_over_12_first(planted, holm):
    """S18: the report's checks recompute each family's Holm over its own claims and accept the analysis's own
    result, untestable claim included, under named and flat Holm families: the first TOST step is at alpha/12."""
    rows = rows_with_tost(TOST_P)
    for row in rows:
        row.outcomes["h4c"] = None
    result = analyse(rows, parsed(prereg_text(holm)))
    code = {"commit": "c" * 40, "tree_sha256": "d" * 64, "dirty": False, "why": None}
    report = render_report(result, EXPORT, study="a-study", code=code, core_version="1.2.3")
    steps = report.split("TOST steps of equivalence family h1:", 1)[1]
    assert "| 1 | h1a | 0.0010 | 0.004167 (alpha/12) | 0.0120 | yes |" in steps
    assert "| 5 | h1b | 0.0120 | 0.00625 (alpha/8) | 0.0960 | no |" in steps
    lines = {line.split(":")[0]: line for line in summary_lines(result)}
    assert lines["h1a"].endswith("Holm TOST p 0.0120 (equivalence family h1)")


def test_s18_the_equivalence_family_type_holds_name_members_and_steps():
    """S18: the record of one declared equivalence family."""
    steps = (HolmStep(1, "h1a", 0.001, 0.002, 0.025, True), HolmStep(2, "h1b", 0.3, 0.3, 0.05, False))
    family = EquivalenceFamily("h1", ("h1a", "h1b"), steps)
    assert (family.name, family.members, family.steps) == ("h1", ("h1a", "h1b"), steps)
