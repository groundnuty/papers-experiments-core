"""The row the analysis consumes, in the field names of the campaign export."""

import dataclasses
import math

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.stats.rows import Row

FACTORS = {"task": "task-a", "gate": "open", "model": "model-a", "seed": 3}


def test_a_row_names_its_run_by_the_run_key_and_never_echoes_an_outcome():
    """Messages built from a row carry the run key, and nothing a scorer computed."""
    row = Row("task=task-a|model=model-a|seed=3", "cond-a", FACTORS, {"score": 0.123456}, {"group": "grp-a"})
    assert row.describe() == "run 'task=task-a|model=model-a|seed=3'"
    assert "0.1234" not in row.describe()


def test_the_built_in_factors_read_from_the_factors_with_the_seed_as_text():
    """Task, model and seed are the pairing identity; the seed is compared the way the run key writes it."""
    row = Row("key", "cond-a", FACTORS, {})
    assert (row.task, row.model, row.seed) == ("task-a", "model-a", "3")


def test_a_row_without_a_built_in_factor_is_refused():
    """Every pairing key is made of task, model and seed, so a row that lacks one is not an analysed run."""
    with pytest.raises(ValueError, match=r"run 'key': factors lack the built-in 'model', 'seed'"):
        Row("key", "cond-a", {"task": "task-a"}, {})


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (math.inf, "is not finite"),
        (-math.inf, "is not finite"),
        (math.nan, "is not finite"),
        (True, "must be a finite number or None, got bool"),
        ("0.5", "must be a finite number or None, got str"),
    ],
)
def test_s05_s01_an_outcome_that_is_not_a_finite_number_or_none_is_refused_naming_the_run_and_the_outcome(
    value, message
):
    """S05, S01: validate at the boundary. A missing value is None; NaN and the infinities are not stored values."""
    with pytest.raises(ValueError, match=message) as caught:
        Row("task=task-a|model=model-a|seed=3", "cond-a", FACTORS, {"good": 0.5, "score": value})
    assert "run 'task=task-a|model=model-a|seed=3'" in str(caught.value)
    assert "outcome 'score'" in str(caught.value)
    assert "outcome 'good'" not in str(caught.value)
    assert "0.5" not in str(caught.value)  # no outcome is echoed, the offending one or the good one


def test_s05_s01_a_row_built_from_the_export_refuses_a_non_finite_score_too():
    """S05, S01: the export boundary goes through the same check as the constructor."""
    entry = {"run_key": "key", "condition": "cond-a", "factors": FACTORS}
    with pytest.raises(ValueError, match="run 'key': outcome 'score' is not finite"):
        Row.from_export(entry, {"score": math.inf})
    with pytest.raises(ValueError, match="run 'key': outcome 'score' is not finite"):
        Row.from_export(entry, {"score": -math.inf})
    row = Row.from_export(entry, {"score": None, "other": 0, "third": 2.5, "fourth": -1})
    assert dict(row.outcomes) == {"score": None, "other": 0, "third": 2.5, "fourth": -1}


def test_a_row_is_immutable_and_its_attributes_default_to_empty():
    """Rows are values."""
    row = Row("key", "cond-a", FACTORS, {"score": None})
    assert row.attributes == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.condition = "other"


def test_from_export_reads_the_fields_the_campaign_export_writes():
    """The identity comes from an `analysed_attempts` entry as the export writes it; scores and attributes join it."""
    schema = FactorSchema({"gate": ["open", "shut"]}, ["task", "gate", "model", "seed"], {"cond-a": {"gate": "open"}})
    key = schema.key("cond-a", "task-a", "model-a", 3)
    entry = {
        "run_key": key.text,
        "digest": key.digest,
        "condition": "cond-a",
        "factors": key.factors,
        "attempt": 1,
        "run_dir": "somewhere",
        "treatment": False,
        "infra_kind": None,
    }
    row = Row.from_export(entry, {"score": 0.5}, {"group": "grp-a"})
    assert (row.run_key, row.condition, dict(row.factors)) == (key.text, "cond-a", key.factors)
    assert (row.task, row.model, row.seed) == ("task-a", "model-a", "3")
    assert (dict(row.outcomes), dict(row.attributes)) == ({"score": 0.5}, {"group": "grp-a"})
    with pytest.raises(ValueError, match="export entry has no 'condition'"):
        Row.from_export({"run_key": "key", "factors": FACTORS}, {})
