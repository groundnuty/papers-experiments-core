"""The row the analysis consumes."""

import dataclasses

import pytest

from expcore.stats.rows import Row


def test_a_row_names_its_run_by_the_built_in_factors_and_never_echoes_an_outcome():
    """Messages built from a row carry the task, the model and the seed, and nothing a scorer computed."""
    row = Row("task-a", "model-a", 3, {"score": 0.123456}, {"group": "grp-a"})
    assert row.describe() == "task 'task-a', model 'model-a', seed 3"
    assert "0.1234" not in row.describe()


def test_a_row_is_immutable_and_its_optional_parts_default_to_empty():
    """Rows are values: they carry no condition and no factors unless given."""
    row = Row("task-a", "model-a", 1, {"score": None})
    assert (row.clusters, row.factors, row.condition) == ({}, {}, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.task = "other"
