"""Power by simulation, driven by the preregistration's power settings."""

import math

import numpy as np
import pytest

from expcore.stats.power import PowerSettings, pilot_cluster_sd, pilot_power, power_sim, power_table

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
