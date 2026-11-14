"""The merge's checks of selections (C38), on facts built here: the plan bound to the fingerprint, a run judged by its
key, and the total spend compared exactly with the ceilings and the releases."""

from pathlib import Path

import pytest

from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import build_manifest
from expcore.campaign.merge import MergeRefused, ShardFacts
from expcore.campaign.merge_selection import check_selections
from expcore.campaign.selection import Selection, SelectionSpec
from expcore.campaign.selection_ledger import Pricing, Spend
from expcore.config.models import Limits

SCHEMA = FactorSchema(
    {"stage": ["early", "late", "last"]},
    ["task", "stage", "model", "seed"],
    {"a": {"stage": "early"}, "b": {"stage": "late"}, "c": {"stage": "last"}},
)
TASKS, MODELS, SEEDS = ("lake", "hill"), ("model-a",), (1,)


def fingerprint(budget: dict | None = None) -> dict:
    """The parts of a campaign fingerprint the selection checks read."""
    return {
        "schema": SCHEMA.to_json(),
        "tasks": dict.fromkeys(TASKS, "0" * 64),
        "models": dict.fromkeys(MODELS, "1" * 64),
        "seeds": {str(seed): True for seed in SEEDS},
        "caps": {"budget": budget},
    }


PLAN = build_manifest(SCHEMA.expand(TASKS, MODELS, SEEDS), 7)


def facts(label: str, levels: tuple[str, ...], *, budget=None, spent=0.0, released=None, runs=None) -> ShardFacts:
    """A concurrent selection of the plan's runs of `levels`, as a merge reads one."""
    spec = SelectionSpec(label, Selection((("stage", levels),)))
    held = {row.key.text: row.key.factors for row in PLAN.rows if row.key.factors["stage"] in levels}
    return ShardFacts(
        Path("out"),
        Path("snapshot"),
        "a" * 64,
        spec,
        "2026-11-14T10:00:00Z",
        {},
        {"unit_caps": {}, "budget": None},
        fingerprint(budget),
        held if runs is None else runs,
        ("2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"),
        released or {},
        Spend(spent, {"model-a": spent}),
    )


PRICING = Pricing(
    {}, Limits(max_tool_rounds=1, time_cap_s=1.0, token_budget=1, output_cap_chars=1, max_output_tokens=1)
)


def test_c38_selections_merge_when_they_cover_the_plan_the_fingerprint_declares():
    """C38: two selections that together hold every run of the fingerprint's plan pass, numbered by their levels."""
    ordered = check_selections([facts("two", ("late", "last")), facts("one", ("early",))], PLAN, None)
    assert [(s.spec.label, s.spec.index, s.spec.count) for s in ordered] == [("one", 1, 2), ("two", 2, 2)]


def test_c38_a_study_plan_cut_to_what_the_selections_ran_is_refused():
    """C38: merged against a study whose plan lacks condition c, selections of a and b would cover it; the plan must
    be the whole plan the selections' fingerprint declares."""
    cut = build_manifest([row.key for row in PLAN.rows if row.key.factors["stage"] != "last"], 7)
    with pytest.raises(MergeRefused, match="not the plan the selections' campaign fingerprint declares"):
        check_selections([facts("one", ("early",)), facts("two", ("late",))], cut, None)


def test_c38_a_run_is_judged_by_its_run_key_never_by_the_factors_its_store_gives():
    """C38: a store that holds another selection's run under factors rewritten to look like its own is refused."""
    one, two = facts("one", ("early",)), facts("two", ("late", "last"))
    stolen = next(key for key, f in two.runs.items() if f["stage"] == "late")
    forged = {**one.runs, stolen: {**two.runs[stolen], "stage": "early"}}
    rest = {k: v for k, v in two.runs.items() if k != stolen}
    with pytest.raises(MergeRefused, match="other factors than its run key has"):
        check_selections([facts("one", ("early",), runs=forged), facts("two", ("late", "last"), runs=rest)], PLAN, None)


@pytest.mark.parametrize(
    ("spent", "released", "refused"),
    [
        ((0.5, 0.5), None, False),
        ((0.5, 0.5000004), None, True),  # $0.0000004 over: refused, not rounded away
        ((0.5, 0.5000006), {"ALL": 0.0000006}, False),  # what a release allowed is allowed, to the last fraction
        ((0.5, 0.5000007), {"ALL": 0.0000006}, True),
    ],
)
def test_c38_the_total_spend_is_compared_exactly_with_the_ceiling_and_the_releases(spent, released, refused):
    """C38: the selections' spend together is held to `max_usd` plus what releases added, with no micro-dollar
    rounding either way."""
    budget = {"max_usd": 1.0, "per_model_usd": {}}
    shards = [
        facts("one", ("early",), budget=budget, spent=spent[0]),
        facts("two", ("late", "last"), budget=budget, spent=spent[1], released=released),
    ]
    if refused:
        with pytest.raises(MergeRefused, match=r"over \[budget\] max_usd"):
            check_selections(shards, PLAN, PRICING)
    else:
        check_selections(shards, PLAN, PRICING)


def test_c38_a_models_ceiling_is_held_across_selections_too():
    """C38: the selections' spend on a model is held to its `per_model_usd` ceiling."""
    budget = {"max_usd": 10.0, "per_model_usd": {"model-a": 1.0}}
    shards = [
        facts("one", ("early",), budget=budget, spent=0.6),
        facts("two", ("late", "last"), budget=budget, spent=0.6),
    ]
    with pytest.raises(MergeRefused, match=r"over \[budget.per_model_usd\] model-a"):
        check_selections(shards, PLAN, PRICING)
