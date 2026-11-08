"""Cohen's kappa and its bootstrap, with the cases where it is undefined (S07)."""

import math

import pytest

from expcore.coding import kappa
from expcore.coding.kappa import aligned_codes, cohen_kappa, kappa_ci, kappa_report


def test_kappa_by_hand_on_four_runs():
    """Hand-computed: agreement 3/4, expected agreement (2*1 + 2*3) / 16 = 1/2, so kappa = (0.75 - 0.5) / 0.5."""
    result = cohen_kappa(["x", "x", "y", "y"], ["x", "y", "y", "y"])
    assert result == {"kappa": pytest.approx(0.5), "po": 0.75, "pe": 0.5, "n": 4}


def test_kappa_is_one_for_full_agreement_and_minus_one_for_a_swap():
    """Two codes used equally: pe = 1/2, so full agreement gives 1 and complete disagreement gives -1."""
    assert cohen_kappa(["x", "y", "x", "y"], ["x", "y", "x", "y"])["kappa"] == 1.0
    assert cohen_kappa(["x", "y"], ["y", "x"])["kappa"] == -1.0


def test_s07_identical_single_code_ratings_give_nan():
    """S07: both coders used one code throughout, pe = 1, so kappa is undefined and reported as NaN."""
    result = cohen_kappa(["x"] * 5, ["x"] * 5)
    assert math.isnan(result["kappa"])
    assert (result["po"], result["pe"], result["n"]) == (1.0, 1.0, 5)


def test_s07_no_run_gives_nan_and_a_count_of_zero():
    """S07: nothing rated is not agreement."""
    result = cohen_kappa([], [])
    assert result["n"] == 0
    assert all(math.isnan(result[key]) for key in ("kappa", "po", "pe"))


def test_coders_must_rate_the_same_number_of_runs():
    """Mismatched lengths are refused."""
    with pytest.raises(ValueError, match="different numbers of runs"):
        cohen_kappa(["x"], ["x", "y"])


def test_s07_the_interval_is_nan_when_every_replicate_is_undefined():
    """S07: single-code ratings leave no valid replicate, so there is no interval."""
    result = kappa_ci(["x"] * 6, ["x"] * 6, n_boot=50)
    assert math.isnan(result["kappa"])
    assert math.isnan(result["ci_low"])
    assert math.isnan(result["ci_high"])
    assert result["n_boot_valid"] == 0


def test_s07_one_run_is_too_few_to_resample():
    """S07: with a single run there is no replicate, so the interval is NaN."""
    result = kappa_ci(["x"], ["y"], n_boot=20)
    assert result["n_boot_valid"] == 0
    assert math.isnan(result["ci_low"])


@pytest.mark.parametrize(("undefined", "has_interval"), [(4, True), (5, True), (6, False), (10, False)])
def test_s07_the_interval_needs_at_least_half_the_replicates_defined(monkeypatch, undefined, has_interval):
    """S07: NaN when fewer than half the replicates are valid; exactly half is enough."""
    calls = []

    def fake(a, b):
        calls.append(None)
        if len(calls) == 1:  # the point estimate
            return {"kappa": 0.5, "po": 0.75, "pe": 0.5, "n": len(a)}
        return {"kappa": math.nan if len(calls) - 1 <= undefined else 0.25}

    monkeypatch.setattr(kappa, "cohen_kappa", fake)
    result = kappa_ci(["x", "y", "x"], ["x", "y", "y"], n_boot=10)
    assert result["n_boot_valid"] == 10 - undefined
    assert math.isnan(result["ci_low"]) is (not has_interval)
    if has_interval:
        assert (result["ci_low"], result["ci_high"]) == (0.25, 0.25)


def test_the_interval_encloses_the_estimate_and_follows_the_seed():
    """A seeded percentile bootstrap over runs: reproducible, and a different seed draws different runs."""
    a = ["x"] * 8 + ["y"] * 7 + ["z"] * 5
    b = ["x"] * 7 + ["y"] * 8 + ["z"] * 4 + ["x"]
    first = kappa_ci(a, b, n_boot=400, seed=3)
    assert first == kappa_ci(a, b, n_boot=400, seed=3)
    assert first["ci_low"] < first["kappa"] < first["ci_high"]
    assert first["n_boot_valid"] == 400
    assert first["alpha"] == 0.05
    other = kappa_ci(a, b, n_boot=400, seed=4)
    assert (other["ci_low"], other["ci_high"]) != (first["ci_low"], first["ci_high"])
    wide = kappa_ci(a, b, n_boot=400, seed=3, alpha=0.5)
    assert wide["ci_high"] - wide["ci_low"] < first["ci_high"] - first["ci_low"]


IDS = ["r1", "r2", "r3", "r4"]
FIRST = {"r1": "x", "r2": "x", "r3": "y", "r4": "y"}
SHEET = [("r2", "y"), ("r1", "x"), ("r4", "y"), ("r3", " y ")]


def test_the_filled_sheet_is_aligned_to_the_sample_order_and_trimmed():
    """The sheet may come back in any order with stray spaces; codes line up by id."""
    assert aligned_codes(IDS, FIRST, SHEET) == (["x", "x", "y", "y"], ["x", "y", "y", "y"])


@pytest.mark.parametrize(
    ("ids", "first", "sheet", "message"),
    [
        (IDS, FIRST, [("r1", "x"), ("r2", "y"), ("r3", ""), ("r4", "y")], "1 sheet rows have no primary code"),
        (IDS, FIRST, [("r1", "x"), ("r2", None), ("r3", "  "), ("r4", "y")], "2 sheet rows have no primary code"),
        (
            IDS,
            FIRST,
            [("r1", "x"), ("r2", "y"), ("r3", "y"), ("r3", "y"), ("r4", "y")],
            "the filled sheet has a duplicate id",
        ),
        (IDS, FIRST, [("r1", "x"), ("r2", "y"), ("", "y"), ("r4", "y")], "the filled sheet has a blank id"),
        (IDS, FIRST, [("r1", "x"), ("r2", "y"), ("r3", "y")], "1 sampled runs missing from the filled sheet"),
        (IDS, FIRST, [*SHEET, ("r9", "x")], "1 runs on the filled sheet are not in the sample"),
        (["r1", "r1", "r2"], FIRST, SHEET, "the sample has a duplicate id"),
        (["r1", " ", "r2"], FIRST, SHEET, "the sample has a blank id"),
        (IDS, {"r1": "x", "r2": "x", "r3": "y"}, SHEET, "1 sampled runs without a first-coder code"),
        (IDS, {**FIRST, "r2": " "}, SHEET, "1 sampled runs without a first-coder code"),
    ],
)
def test_s07_blank_duplicate_or_missing_ids_raise(ids, first, sheet, message):
    """S07: a sheet that does not match the sample is refused, never silently reduced to the rows that match."""
    with pytest.raises(ValueError, match=message):
        aligned_codes(ids, first, sheet)


def test_the_report_has_a_row_for_all_runs_and_one_per_group_in_sorted_order():
    """Kappa overall and within each group label, with the same fields as the interval function."""
    rows = kappa_report(IDS, FIRST, SHEET, groups={"r1": "g2", "r2": "g2", "r3": "g1", "r4": "g1"}, n_boot=50)
    assert [row["scope"] for row in rows] == ["all", "g1", "g2"]
    assert rows[0]["kappa"] == pytest.approx(0.5)
    assert rows[0]["n"] == 4
    assert rows[1]["n"] == rows[2]["n"] == 2
    assert math.isnan(rows[1]["kappa"])  # in g1 both coders said y twice: one code throughout
    assert set(rows[0]) == {"scope", "kappa", "po", "pe", "n", "ci_low", "ci_high", "n_boot_valid", "alpha"}


def test_the_report_without_groups_is_the_single_overall_row():
    """No grouping asked, no grouping computed."""
    rows = kappa_report(IDS, FIRST, SHEET, n_boot=50)
    assert [row["scope"] for row in rows] == ["all"]


def test_the_report_refuses_an_unlabelled_run_and_a_sheet_that_does_not_match():
    """Both the group labels and the sheet are checked before any kappa is reported."""
    with pytest.raises(ValueError, match="1 sampled runs have no group label"):
        kappa_report(IDS, FIRST, SHEET, groups={"r1": "g", "r2": "g", "r3": "g"})
    with pytest.raises(ValueError, match="missing from the filled sheet"):
        kappa_report(IDS, FIRST, SHEET[:3])
