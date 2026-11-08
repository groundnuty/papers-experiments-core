"""The record simulator: its row shape, its planted truth, and its refusal of parameters that would clip."""

import dataclasses

import numpy as np
import pytest

from expcore.stats.rows import Row
from expcore.stats.sim import OutcomeSpec, SimSpec, simulate

CONDITIONS = {"cond-a": {"gate": "open"}, "cond-b": {"gate": "shut"}}


def spec(**overrides):
    """A small valid spec with one bounded and one unbounded outcome."""
    outcomes = {
        "rate": OutcomeSpec("proportion", {"cond-a": 0.6, "cond-b": 0.4}),
        "cost": OutcomeSpec("real", {"cond-a": 10.0, "cond-b": 12.0}, run_halfwidth=0.4, scale=5.0),
    }
    base = SimSpec(CONDITIONS, outcomes, n_clusters=4, tasks_per_cluster=2, models=("model-a", "model-b"), seeds=(1, 2))
    return dataclasses.replace(base, **overrides)


def test_the_rows_cover_the_design_in_the_shape_the_pipeline_reads():
    """One row per cluster, task, condition, model and seed, with factors, condition and the cluster attribute."""
    rows = simulate(spec(), np.random.default_rng(0))
    assert len(rows) == 4 * 2 * 2 * 2 * 2
    assert all(isinstance(row, Row) for row in rows)
    first = rows[0]
    assert (first.task, first.model, first.seed, first.condition) == ("c00-t0", "model-a", 1, "cond-a")
    assert first.factors == {"gate": "open"}
    assert first.clusters == {"group": "c00"}
    assert set(first.outcomes) == {"rate", "cost"}
    assert {row.task for row in rows} == {f"c{i:02d}-t{j}" for i in range(4) for j in range(2)}


def test_the_same_seed_draws_the_same_rows_and_another_seed_does_not():
    """The simulation is a function of the generator."""
    first = simulate(spec(), np.random.default_rng(4))
    assert first == simulate(spec(), np.random.default_rng(4))
    assert first != simulate(spec(), np.random.default_rng(5))


def test_s09_the_true_contrast_is_the_plain_difference_of_the_stated_parameters():
    """S09: A - B by arithmetic on the inputs, model effects averaged over the models."""
    effects = {
        ("rate", "model-a", "cond-a"): 0.04,
        ("rate", "model-b", "cond-a"): 0.0,
        ("rate", "model-a", "cond-b"): -0.02,
    }
    s = spec(model_effect=effects)
    assert s.true_contrast("rate", "cond-a", "cond-b") == pytest.approx((0.6 + 0.02) - (0.4 - 0.01))
    assert s.true_contrast("cost", "cond-b", "cond-a") == pytest.approx(2.0)


def test_s09_a_spec_whose_noise_could_clip_a_probability_is_refused():
    """S09: refusal of parameters that would clip probabilities, for each bounded kind and each condition."""
    shared = 0.04 + 0.08 + 0.08  # model, cluster and cluster-by-condition half-widths
    tight = {"cond-a": 0.6, "cond-b": shared - 0.01}  # cond-b's mean sits inside the noise band
    with pytest.raises(ValueError, match=r"outcome 'p', condition 'cond-b'.*could leave \[0, 1\].*would clip"):
        simulate(spec(outcomes={"p": OutcomeSpec("binary", tight)}), np.random.default_rng(0))
    high = {"cond-a": 0.6, "cond-b": 1 - shared + 0.01}  # the upper edge clips as well
    with pytest.raises(ValueError, match=r"outcome 'p', condition 'cond-b'"):
        simulate(spec(outcomes={"p": OutcomeSpec("binary", high)}), np.random.default_rng(0))
    share = {"cond-a": 0.6, "cond-b": shared + 0.05 - 0.01}  # run noise counts for a share
    with pytest.raises(ValueError, match="would clip"):
        simulate(spec(outcomes={"p": OutcomeSpec("proportion", share)}), np.random.default_rng(0))


def test_s09_a_planted_model_effect_counts_toward_the_clipping_check():
    """S09: the worst model shift is added to the noise band."""
    means = {"cond-a": 0.3, "cond-b": 0.3}
    assert spec(outcomes={"p": OutcomeSpec("binary", means)}).validate() is None
    s = spec(outcomes={"p": OutcomeSpec("binary", means)}, model_effect={("p", "model-b", "cond-a"): 0.15})
    with pytest.raises(ValueError, match="outcome 'p', condition 'cond-a'"):
        s.validate()


def test_s09_the_edge_of_the_clipping_band_is_accepted_and_nothing_leaves_the_unit_interval():
    """S09: parameters exactly at the limit are valid, and a large draw stays inside [0, 1] without clipping."""
    shared = 0.04 + 0.08 + 0.08
    edge = {"cond-a": shared + 0.05, "cond-b": 1 - shared - 0.05}
    s = spec(
        outcomes={
            "p": OutcomeSpec("proportion", edge),
            "q": OutcomeSpec("binary", {"cond-a": shared, "cond-b": 1 - shared}),
        },
        n_clusters=30,
    )
    s.validate()
    rows = simulate(s, np.random.default_rng(11))
    shares = np.array([row.outcomes["p"] for row in rows])
    assert shares.min() >= 0.0
    assert shares.max() <= 1.0
    assert {row.outcomes["q"] for row in rows} <= {0.0, 1.0}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"conditions": {}}, "needs conditions, outcomes, models and seeds"),
        ({"n_clusters": 0}, "must be at least 1"),
        ({"model_shift": -0.1}, "half-widths cannot be negative"),
        ({"outcomes": {"p": OutcomeSpec("ordinal", {"cond-a": 0.5, "cond-b": 0.5})}}, "kind 'ordinal' is not one of"),
        ({"outcomes": {"p": OutcomeSpec("real", {"cond-a": 0.5})}}, "means must name exactly the conditions"),
        (
            {"outcomes": {"p": OutcomeSpec("real", {"cond-a": 0.5, "cond-b": 0.5}, missing_probability=1.0)}},
            "missing_probability",
        ),
    ],
)
def test_a_malformed_spec_is_refused_by_name(change, message):
    """The spec is validated before any draw."""
    with pytest.raises(ValueError, match=message):
        simulate(spec(**change), np.random.default_rng(0))


def test_outcomes_go_missing_independently_of_their_value():
    """A run's outcome is None with the stated probability, whatever its value would have been."""
    outcomes = {"p": OutcomeSpec("proportion", {"cond-a": 0.5, "cond-b": 0.5}, missing_probability=0.25)}
    rows = simulate(spec(outcomes=outcomes, n_clusters=40), np.random.default_rng(6))
    gone = np.array([row.outcomes["p"] is None for row in rows])
    assert gone.mean() == pytest.approx(0.25, abs=0.05)
    with_zero = simulate(
        spec(outcomes={"p": OutcomeSpec("proportion", {"cond-a": 0.5, "cond-b": 0.5})}), np.random.default_rng(6)
    )
    assert all(row.outcomes["p"] is not None for row in with_zero)
