"""Power by simulation, driven by the preregistration's power settings."""

import dataclasses
import math

import numpy as np
import pytest
from scipy import stats

from expcore.stats.power import (
    PowerSettings,
    minimum_detectable_effect,
    pilot_cluster_sd,
    pilot_power,
    power_sim,
    power_table,
    signed_rank_p_values,
)

SETTINGS = PowerSettings(seed=5, n_simulations=40, target_clusters=25, target_difference=0.2, sd_grid=(0.1, 0.3))


def test_power_is_zero_when_the_smallest_attainable_p_value_exceeds_alpha():
    """Hand-computed: five clusters all positive give two-sided p = 2 / 2**5 = 0.0625, so alpha = 0.05 is never met."""
    rng = np.random.default_rng(1)
    assert power_sim(sd=0.01, n=5, delta=1.0, alpha=0.05, n_sim=50, rng=rng, method="exact") == 0.0
    assert power_sim(sd=0.01, n=5, delta=1.0, alpha=0.1, n_sim=50, rng=rng, method="exact") == 1.0


def test_power_is_near_alpha_without_an_effect_and_near_one_with_a_large_one():
    """The simulated test holds its level at delta = 0 and detects a difference of two standard deviations."""
    rng = np.random.default_rng(2)
    assert 0.02 < power_sim(0.1, 25, 0.0, 0.05, 600, rng) < 0.09
    assert power_sim(0.1, 25, 0.2, 0.05, 60, rng) > 0.97


def test_power_grows_with_the_effect_and_falls_with_the_spread():
    """Monotone in the two inputs a study chooses."""
    small, large = (power_sim(0.2, 20, delta, 0.05, 150, np.random.default_rng(3)) for delta in (0.05, 0.2))
    tight, loose = (power_sim(sd, 20, 0.1, 0.05, 150, np.random.default_rng(3)) for sd in (0.1, 0.4))
    assert small < large
    assert loose < tight


def test_the_simulation_is_a_function_of_the_generator():
    """The same seed gives the same power."""
    first = power_sim(0.2, 15, 0.1, 0.05, 100, np.random.default_rng(9))
    assert first == power_sim(0.2, 15, 0.1, 0.05, 100, np.random.default_rng(9))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sd": 0.0}, "sd must be a positive number"),
        ({"sd": math.nan}, "sd must be a positive number"),
        ({"n": 0}, "n and n_sim must be at least 1"),
        ({"n_sim": 0}, "n and n_sim must be at least 1"),
        ({"alpha": 0.0}, "alpha must be strictly between 0 and 1"),
        ({"alpha": 1.0}, "alpha must be strictly between 0 and 1"),
    ],
)
def test_power_sim_refuses_parameters_that_cannot_be_simulated(kwargs, message):
    """Bad inputs fail by name rather than returning a power."""
    params = {"sd": 0.1, "n": 5, "delta": 0.1, "alpha": 0.05, "n_sim": 5, "rng": np.random.default_rng(0)} | kwargs
    with pytest.raises(ValueError, match=message):
        power_sim(**params)


def test_the_table_takes_its_targets_from_the_settings_at_two_alpha_levels():
    """Each SD of the grid gets a row at alpha and one at alpha divided by the number of contrasts."""
    table = power_table(SETTINGS, alpha=0.05, n_contrasts=4)
    assert [(row["sd"], row["alpha_level"], row["alpha"]) for row in table] == [
        (0.1, "alpha", 0.05),
        (0.1, "alpha_over_contrasts", 0.0125),
        (0.3, "alpha", 0.05),
        (0.3, "alpha_over_contrasts", 0.0125),
    ]
    assert {(row["n_clusters"], row["delta"]) for row in table} == {(25, 0.2)}
    assert all(0.0 <= row["power"] <= 1.0 for row in table)
    assert table[0]["power"] >= table[1]["power"]  # a stricter alpha cannot detect more
    assert table[0]["power"] > table[2]["power"]  # and a wider spread cannot either


def test_the_table_is_reproducible_and_can_skip_the_simulation():
    """One seeded generator serves the rows in order; `simulate=False` leaves every power NaN."""
    assert power_table(SETTINGS, 0.05, 2) == power_table(SETTINGS, 0.05, 2)
    skipped = power_table(SETTINGS, 0.05, 2, simulate=False)
    assert len(skipped) == 4
    assert all(math.isnan(row["power"]) for row in skipped)


def test_a_nan_sd_keeps_its_row_with_a_nan_power():
    """A spread that could not be estimated shows as a gap, not as a made-up power."""
    table = power_table(SETTINGS, 0.05, 2, sds=[math.nan, 0.1])
    assert math.isnan(table[0]["power"])
    assert math.isnan(table[1]["power"])
    assert not math.isnan(table[2]["power"])


def test_the_pilot_sd_is_the_sample_sd_of_the_defined_clusters():
    """Hand-computed: 1, 2, 3 have sample SD 1; fewer than two defined clusters give NaN."""
    assert pilot_cluster_sd([1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert pilot_cluster_sd([1.0, 2.0, 3.0, math.nan]) == pytest.approx(1.0)
    assert math.isnan(pilot_cluster_sd([1.0, math.nan]))
    assert math.isnan(pilot_cluster_sd([]))


def test_pilot_power_gives_each_contrast_its_own_sd_and_keeps_the_ones_it_cannot_estimate():
    """Per contrast: its pilot clusters, its SD and table rows; one defined cluster leaves NaN rows, not a gap."""
    pilot = {"a-vs-b": [0.1, 0.3, 0.2, 0.4], "c-vs-d": [0.5, math.nan]}
    rows = pilot_power(pilot, SETTINGS, alpha=0.05)
    assert [(r["contrast"], r["pilot_clusters"], r["alpha_level"]) for r in rows] == [
        ("a-vs-b", 4, "alpha"),
        ("a-vs-b", 4, "alpha_over_contrasts"),
        ("c-vs-d", 1, "alpha"),
        ("c-vs-d", 1, "alpha_over_contrasts"),
    ]
    assert rows[0]["sd"] == pytest.approx(np.std([0.1, 0.3, 0.2, 0.4], ddof=1))
    assert rows[1]["alpha"] == pytest.approx(0.025)  # divided by the two contrasts in the pilot
    assert not math.isnan(rows[0]["power"])
    assert math.isnan(rows[2]["sd"])
    assert math.isnan(rows[2]["power"])


def test_the_vectorised_signed_rank_p_value_is_the_hand_computed_one():
    """Hand-computed: ranks 1..5 all positive give W = 0 and p = 2 / 2**5; one negative sixth rank gives 28 / 64."""
    p = signed_rank_p_values(np.array([[1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 1.0, 4.0, 2.0, 3.0]]))
    assert p == pytest.approx([0.0625, 0.0625])
    assert signed_rank_p_values(np.array([[1.0, 2.0, 3.0, 4.0, 5.0, -6.0]])) == pytest.approx([0.4375])


@pytest.mark.parametrize("n", [1, 3, 7, 25, 50])
def test_the_vectorised_signed_rank_p_value_agrees_with_scipys_exact_test(n):
    """On continuous draws, which have no ties, every row gives scipy's exact p-value."""
    draws = np.random.default_rng(n).normal(0.3, 1.0, size=(60, n))
    expected = [stats.wilcoxon(row, method="exact").pvalue for row in draws]
    assert signed_rank_p_values(draws) == pytest.approx(expected, abs=1e-12)


def test_beyond_the_exact_limit_the_normal_curve_stands_in_and_is_close_to_scipys():
    """With 250 clusters the null distribution is the normal one; the p-values stay within a hundredth of scipy's."""
    draws = np.random.default_rng(9).normal(0.1, 1.0, size=(30, 250))
    expected = [stats.wilcoxon(row, method="approx").pvalue for row in draws]
    assert signed_rank_p_values(draws) == pytest.approx(expected, abs=0.01)


def test_the_signed_rank_p_values_refuse_a_wrong_shape():
    """A (studies x clusters) array is required."""
    with pytest.raises(ValueError, match="studies x clusters"):
        signed_rank_p_values(np.array([1.0, 2.0]))


def test_the_minimum_detectable_effect_is_near_the_textbook_figure_and_reaches_the_target_power():
    """With 25 clusters at alpha 0.05 and power 0.8 it is about 2.8 / 5 of the SD (a little more for a rank test)."""
    settings = PowerSettings(seed=1, n_simulations=1500, target_clusters=25, target_difference=0.1, sd_grid=(0.1,))
    mde = minimum_detectable_effect(0.2, 25, 0.05, settings, np.random.default_rng(3))
    assert 0.2 * 0.5 < mde < 0.2 * 0.68
    reached = power_sim(0.2, 25, mde, 0.05, 300, np.random.default_rng(4))
    assert reached == pytest.approx(0.8, abs=0.1)


def test_the_minimum_detectable_effect_scales_with_the_sd_and_shrinks_with_more_clusters():
    """The test sees ranks, so doubling the SD doubles the effect; more clusters detect a smaller one."""
    settings = PowerSettings(seed=1, n_simulations=500, target_clusters=25, target_difference=0.1, sd_grid=(0.1,))
    one = minimum_detectable_effect(1.0, 20, 0.05, settings, np.random.default_rng(5))
    two = minimum_detectable_effect(2.0, 20, 0.05, settings, np.random.default_rng(5))
    assert two == pytest.approx(2 * one)
    assert minimum_detectable_effect(1.0, 60, 0.05, settings, np.random.default_rng(5)) < one
    stricter = minimum_detectable_effect(1.0, 20, 0.01, settings, np.random.default_rng(5))
    assert stricter > one


def test_the_minimum_detectable_effect_follows_the_target_power_in_the_settings():
    """A higher target power needs a larger effect, and the default target is 0.8."""
    low = PowerSettings(1, 500, 25, 0.1, (0.1,), target_power=0.6)
    high = dataclasses.replace(low, target_power=0.95)
    assert minimum_detectable_effect(1.0, 20, 0.05, low, np.random.default_rng(6)) < minimum_detectable_effect(
        1.0, 20, 0.05, high, np.random.default_rng(6)
    )
    assert PowerSettings(1, 10, 5, 0.1, (0.1,)).target_power == 0.8


def test_the_minimum_detectable_effect_is_nan_when_the_test_can_never_reject():
    """Five clusters cannot give a two-sided p-value below 0.0625, so nothing is detectable at alpha 0.05."""
    settings = PowerSettings(1, 200, 5, 0.1, (0.1,))
    assert math.isnan(minimum_detectable_effect(1.0, 5, 0.05, settings, np.random.default_rng(7)))
    assert not math.isnan(minimum_detectable_effect(1.0, 6, 0.05, settings, np.random.default_rng(7)))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sd": 0.0}, "sd must be a positive number"),
        ({"sd": math.nan}, "sd must be a positive number"),
        ({"n": 0}, "n must be at least 1"),
        ({"alpha": 1.0}, "alpha must be strictly between 0 and 1"),
    ],
)
def test_the_minimum_detectable_effect_refuses_parameters_it_cannot_search(kwargs, message):
    """Bad inputs fail by name."""
    params = {"sd": 1.0, "n": 10, "alpha": 0.05, "settings": SETTINGS, "rng": np.random.default_rng(0)} | kwargs
    with pytest.raises(ValueError, match=message):
        minimum_detectable_effect(**params)
