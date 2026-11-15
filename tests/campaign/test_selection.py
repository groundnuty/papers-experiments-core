"""Selection shards (C38): the selection and its spec, and a selection's shares of the caps and the budget.

The shares are pure functions of the plan's run counts and one run's reservation per model, so the budget's figures
(the study's $60 with a $32.975 reservation per run) are tested here exactly, without running a campaign.
"""

import itertools
from fractions import Fraction

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.campaign.selection import RULE_SELECTION, Selection, SelectionSpec, spec_from_json
from expcore.campaign.selection_budget import (
    proportional_budget,
    proportional_caps,
    refuse_short,
    remaining_budget,
)
from expcore.campaign.shard import RULE, ShardError, ShardSpec, micro_usd

SCHEMA = FactorSchema(
    {"stage": ["early", "mid", "late", "last"], "probe": ["on", "off"]},
    ["task", "stage", "probe", "model", "seed"],
    {f"{s}-{p}": {"stage": s, "probe": p} for s in ("early", "mid", "late", "last") for p in ("on", "off")},
)


# --- the selection ---------------------------------------------------------------------------------------------------


def test_c38_a_selection_reads_factor_levels_and_keeps_the_runs_they_select():
    """C38: `--select stage=early,mid` keeps the runs whose stage is early or mid; several factors must all match."""
    one = Selection.parse(["stage=mid,early"]).checked(SCHEMA)
    assert one.to_json() == {"stage": ["early", "mid"]}  # the schema's order of levels
    assert one.selects({"stage": "early", "probe": "on"})
    assert not one.selects({"stage": "late", "probe": "on"})
    both = Selection.parse(["stage=late,last", "probe=on"]).checked(SCHEMA)
    assert both.text == "probe=on stage=late,last"
    assert both.selects({"stage": "last", "probe": "on"})
    assert not both.selects({"stage": "last", "probe": "off"})


def test_c38_an_unknown_factor_is_refused_naming_it_and_the_declared_ones():
    """C38: a factor the study does not declare is refused, naming it; the built-in ones are not selected on."""
    with pytest.raises(ShardError, match=r"'phase'.*declared factors: stage, probe"):
        Selection.parse(["phase=early"]).checked(SCHEMA)
    with pytest.raises(ShardError, match="'model'"):
        Selection.parse(["model=alpha"]).checked(SCHEMA)


def test_c38_an_unknown_level_is_refused_naming_it_and_the_factors_levels():
    """C38: a level the factor does not have is refused, naming the level and the factor's levels."""
    with pytest.raises(ShardError, match=r"'never'.*its levels: early, mid, late, last"):
        Selection.parse(["stage=early,never"]).checked(SCHEMA)


@pytest.mark.parametrize(
    ("texts", "fragment"),
    [
        ([], "at least one factor"),
        (["stage"], "FACTOR=LEVEL"),
        (["stage="], "names no level"),
        (["stage=early,early"], "repeats a level"),
        (["stage=early", "stage=mid"], "twice"),
    ],
)
def test_c38_a_selection_the_command_line_cannot_mean_is_refused(texts, fragment):
    """C38: no factor, no level, a repeated level or a factor given twice is refused before the study is read."""
    with pytest.raises(ShardError, match=fragment):
        Selection.parse(texts)


def test_c38_a_selection_spec_is_kept_under_its_own_rule_and_read_back():
    """C38: the spec is kept as `{rule, label, select, sequential}` under `expcore-shard/4`; a stored spec is read
    by its rule, and a shard spec cannot take the selection's rule."""
    spec = SelectionSpec("first", Selection.parse(["stage=early,mid"]), sequential=True)
    data = spec.to_json()
    assert data == {"rule": RULE_SELECTION, "label": "first", "select": {"stage": ["early", "mid"]}, "sequential": True}
    assert spec_from_json(data) == spec
    assert isinstance(spec_from_json(ShardSpec(1, 2, "a").to_json()), ShardSpec)
    with pytest.raises(ShardError):
        ShardSpec(1, 2, "a", RULE_SELECTION)
    with pytest.raises(ShardError, match="exactly the keys"):
        SelectionSpec.from_json({**data, "index": 1})
    assert ShardSpec(1, 2, "a").rule == RULE


def test_c38_a_selection_label_is_held_to_the_shard_labels_form():
    """C38: a label with a dot, which could be a host name, is refused unless the wide form is asked for."""
    with pytest.raises(ShardError, match="label"):
        SelectionSpec("host.example", Selection.parse(["stage=early"]))
    assert SelectionSpec("batch.one", Selection.parse(["stage=early"]), wide_label=True).label == "batch.one"


# --- the shares ------------------------------------------------------------------------------------------------------

RESERVE = {"dear": 0.8125, "cheap": 0.03, "free": 0.0}
PLANNED = {"dear": 40, "cheap": 40, "free": 40}


def _splits():
    """Ways of splitting the plan's runs of each model between selections (each a list of per-model counts)."""
    for dear, cheap, free in itertools.product((1, 7, 13, 20), (3, 19, 37), (0, 11)):
        yield [
            {"dear": dear, "cheap": cheap, "free": free},
            {"dear": 40 - dear, "cheap": 40 - cheap, "free": 40 - free},
        ]
    yield [
        {"dear": 13, "cheap": 13, "free": 13},
        {"dear": 13, "cheap": 13, "free": 13},
        {"dear": 14, "cheap": 14, "free": 14},
    ]


@pytest.mark.parametrize("split", list(_splits()))
def test_c38_proportional_budget_shares_sum_to_at_most_the_totals(split):
    """C38: disjoint selections that cover the plan get parts of `max_usd` and of each ceiling that sum to at most
    the totals, in whole micro-dollars (the unit the parts are made in), and the parts of `max_usd` follow the
    reserved bound: within one micro-dollar of each selection's exact proportion."""
    parts = [proportional_budget(60.0, {"dear": 33.3, "free": 1.0}, RESERVE, s, PLANNED) for s in split]
    assert sum(micro_usd(p["max_usd"]) for p in parts) <= micro_usd(60.0)
    for model, ceiling in (("dear", 33.3), ("free", 1.0)):
        assert sum(micro_usd(p["per_model_usd"][model]) for p in parts) <= micro_usd(ceiling)
    whole = sum(Fraction(RESERVE[m]) * n for m, n in PLANNED.items())
    for part, selected in zip(parts, split, strict=True):
        exact = Fraction(micro_usd(60.0)) * sum(Fraction(RESERVE[m]) * n for m, n in selected.items()) / whole
        assert exact - 1 < micro_usd(part["max_usd"]) <= exact


def test_c38_a_selection_of_free_models_gets_none_of_max_usd():
    """C38: `max_usd` follows the reserved bound, so a selection of a model that costs nothing gets 0 of it."""
    part = proportional_budget(60.0, {}, RESERVE, {"free": 40}, PLANNED)
    assert part["max_usd"] == 0.0


def test_c38_the_studys_budget_split_in_half_is_refused_naming_the_model():
    """C38: $60 split over two batches of one model whose run reserves $32.975 leaves each $30, so no run of the
    model could start: refused at start, naming the model, and pointing to --sequential."""
    reserve, planned = {"model-a": 32.975}, {"model-a": 20}
    half = proportional_budget(60.0, {}, reserve, {"model-a": 10}, planned)
    assert half["max_usd"] == 30.0
    with pytest.raises(ShardError, match=r"model model-a.*30\.000000 USD.*32\.975000 USD.*--sequential"):
        refuse_short(half, {}, reserve, {"model-a": 10}, "its proportion")


def test_c38_sequentially_the_first_gets_the_whole_budget_and_the_second_what_the_first_left():
    """C38: under --sequential the first selection gets the whole $60, which pays a $32.975 run, and the second gets
    $60 minus what the first actually spent."""
    reserve, selected = {"model-a": 32.975}, {"model-a": 10}
    first = remaining_budget(60.0, {}, 0.0, {})
    assert first == {"max_usd": 60.0, "per_model_usd": {}}
    refuse_short(first, {}, reserve, selected, "the whole budget")  # one run fits: not refused
    second = remaining_budget(60.0, {}, 20.000_000_4, {"model-a": 20.000_000_4})
    assert second["max_usd"] == 39.999999  # what was spent is rounded up to the micro-dollar, what is left down
    refuse_short(second, {}, reserve, selected, "what the earlier selections left")


def test_c38_a_later_selection_left_too_little_is_refused_naming_the_model():
    """C38: a later sequential selection that the earlier ones left less than one run's reservation is refused."""
    reserve, selected = {"model-a": 32.975}, {"model-a": 10}
    second = remaining_budget(60.0, {}, 29.0, {})
    with pytest.raises(ShardError, match=r"model model-a.*31\.000000 USD"):
        refuse_short(second, {}, reserve, selected, "what the earlier selections left")


def test_c38_a_models_ceiling_is_checked_too_and_a_ceiling_of_zero_is_not_refused():
    """C38: a part of a model's `per_model_usd` below one run's reservation is refused, naming it; a ceiling of 0,
    which holds the model by design, is kept at 0 and not refused."""
    reserve, planned = {"model-a": 1.0, "model-b": 1.0}, {"model-a": 10, "model-b": 10}
    part = proportional_budget(100.0, {"model-a": 1.5, "model-b": 0.0}, reserve, {"model-a": 5, "model-b": 5}, planned)
    assert part["per_model_usd"] == {"model-a": 0.75, "model-b": 0.0}
    with pytest.raises(ShardError, match=r"\[budget.per_model_usd\] model-a"):
        refuse_short(part, {"model-a": 1.5, "model-b": 0.0}, reserve, {"model-a": 5, "model-b": 5}, "its proportion")
    refuse_short(part, {"model-b": 0.0}, reserve, {"model-b": 5}, "its proportion")


def test_c38_unit_cap_shares_sum_to_at_most_the_cap_and_a_zero_share_is_refused():
    """C38: a unit's cap is split by the planned runs on it, rounded down; a selection whose part of a unit it runs
    on is 0 is refused, naming the unit and the model."""
    unit_of = {"a": "unit-a", "b": "unit-b"}
    planned = {"a": 10, "b": 10}
    one = proportional_caps({"unit-a": 4, "unit-b": 3}, {"a": 6, "b": 5}, planned, unit_of)
    two = proportional_caps({"unit-a": 4, "unit-b": 3}, {"a": 4, "b": 5}, planned, unit_of)
    assert one == {"unit-a": 2, "unit-b": 1}
    assert two == {"unit-a": 1, "unit-b": 1}
    assert proportional_caps({"unit-a": 4}, {"b": 10}, planned, unit_of) == {"unit-a": 0}  # runs nothing there
    with pytest.raises(ShardError, match=r"unit_caps\] unit-a = 1.*model a.*--sequential"):
        proportional_caps({"unit-a": 1}, {"a": 4}, planned, unit_of)
