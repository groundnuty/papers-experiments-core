"""S06: the one rule that decides a rejection, and the three places that use it."""

import math

import numpy as np

from expcore.stats import analysis, contrasts, power
from expcore.stats.rejection import rejects


def test_s06_a_p_value_equal_to_its_level_rejects_and_a_nan_never_does():
    """S06: the boundary is inclusive for a number and for an array, and an undefined p-value is no rejection."""
    assert rejects(0.05, 0.05)
    assert not rejects(0.0500001, 0.05)
    assert not rejects(math.nan, 0.05)
    assert list(rejects(np.array([0.01, 0.05, 0.06, math.nan]), 0.05)) == [True, True, False, False]


def test_s06_holm_the_analysis_and_the_power_study_decide_a_boundary_p_value_alike():
    """S06: a p-value of exactly 0.0625, the smallest five clusters give, is a rejection at alpha 0.0625 in each."""
    assert contrasts.holm([0.0625], 0.0625).reject == (True,)
    assert contrasts.holm([0.0625], 0.0624).reject == (False,)
    assert analysis.rejects is contrasts.rejects is power.rejects
