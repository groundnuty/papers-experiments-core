"""What became of one arm's planned runs: measured, or lost for one cause each, and the share that finished."""

import math

import pytest

from expcore.stats.armruns import LOST_CAUSES, ArmRuns, count_arm


def test_s14_every_planned_run_is_measured_or_lost_for_one_cause():
    """S14: two measured, a treatment attempt and an ordinary run with no outcome, one run exhausted, one unfinished."""
    runs = count_arm(
        "cond-a", [(False, True), (False, True), (True, False), (False, False)], ["exhausted", "unfinished"]
    )
    assert (runs.planned, runs.finished, runs.measured) == (6, 4, 2)
    assert runs.lost == {
        "treatment_outcome_none": 1,
        "outcome_none": 1,
        "exhausted": 1,
        "unfinished": 1,
        "excluded": 0,
    }
    assert runs.finish_rate == pytest.approx(4 / 6)


def test_s14_an_arm_with_no_planned_run_has_no_finish_rate():
    """S14: 0 / 0 is not a rate of 0, so it is NaN, which the report shows as n/a."""
    runs = count_arm("cond-a", [], [])
    assert runs.planned == 0
    assert math.isnan(runs.finish_rate)


def test_the_causes_are_the_documented_five_in_order():
    """The report's columns and analysis.json's keys follow this order."""
    assert LOST_CAUSES == ("treatment_outcome_none", "outcome_none", "exhausted", "unfinished", "excluded")


@pytest.mark.parametrize(
    "fields",
    [
        {"planned": 3, "finished": 2, "measured": 2, "lost": dict.fromkeys(LOST_CAUSES, 0)},  # one planned run unseen
        {"planned": 2, "finished": 2, "measured": 1, "lost": dict.fromkeys(LOST_CAUSES, 0)},  # one finished run unseen
        {"planned": 1, "finished": 1, "measured": 1, "lost": {"outcome_none": 0}},  # a cause missing
    ],
)
def test_counts_that_do_not_add_up_are_refused(fields):
    """A run counted nowhere, or a cause left out, would let a loss go unshown."""
    with pytest.raises(ValueError, match=r"add up|every cause"):
        ArmRuns("cond-a", **fields)


def test_an_unknown_absent_reason_is_refused():
    """An absent run is exhausted, unfinished or excluded, as the export lists it."""
    with pytest.raises(ValueError, match="'lost'"):
        count_arm("cond-a", [], ["lost"])
