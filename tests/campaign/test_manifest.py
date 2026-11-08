"""The manifest: a seeded order that survives growth (C08, R11) and the fingerprint of an experiment (C06)."""

import copy

import pytest
from campaign_support import make_fingerprint, make_keys, make_manifest, make_schema, sha

from expcore.campaign.factors import FactorSchema, PlanError
from expcore.campaign.manifest import (
    build_fingerprint,
    build_manifest,
    compare_fingerprints,
    fingerprint_sha256,
    order_key,
    relative_order_problem,
)


def texts(manifest):
    return [row.key.text for row in manifest.rows]


def test_the_manifest_holds_every_planned_run_once_with_consecutive_places():
    manifest = make_manifest(make_schema())
    assert [row.seq for row in manifest.rows] == list(range(len(manifest.rows)))
    assert sorted(texts(manifest)) == sorted(k.text for k in make_keys(make_schema()))


def test_the_same_seed_gives_the_same_order_whatever_order_the_plan_was_listed_in():
    schema = make_schema()
    keys = make_keys(schema, tasks=("task-a", "task-b", "task-c"), models=("m1", "m2"))
    forward = build_manifest(keys, 11)
    backward = build_manifest(list(reversed(keys)), 11)
    assert texts(forward) == texts(backward)
    assert forward.order_sha256 == backward.order_sha256
    assert forward.plan_sha256 == backward.plan_sha256


def test_another_seed_gives_another_order_of_the_same_plan():
    schema = make_schema()
    one, two = make_manifest(schema, 1), make_manifest(schema, 2)
    assert texts(one) != texts(two)
    assert one.plan_sha256 == two.plan_sha256
    assert one.order_sha256 != two.order_sha256


def test_a_run_s_place_depends_on_the_seed_and_its_key_alone():
    key = make_keys(make_schema())[0]
    assert order_key(5, key) == order_key(5, key)
    assert order_key(5, key) != order_key(6, key)


@pytest.mark.parametrize(
    "grown",
    [
        {"tasks": ("task-a", "task-b", "task-c")},
        {"models": ("m1", "m2")},
        {"seeds": (1, 2, 3, 4)},
        {"tasks": ("task-0", "task-a", "task-b"), "models": ("m0", "m1"), "seeds": (0, 1, 2)},
    ],
)
def test_c08_adding_runs_keeps_the_relative_order_of_the_existing_ones(grown):
    """C08: adding runs keeps the relative order of existing ones."""
    schema = make_schema()
    small = make_manifest(schema, 3)
    large = make_manifest(schema, 3, **grown)
    assert len(large.rows) > len(small.rows)
    kept = [t for t in texts(large) if t in set(texts(small))]
    assert kept == texts(small)
    assert relative_order_problem(texts(small), texts(large)) is None


def test_c08_adding_a_condition_keeps_the_relative_order_of_the_existing_runs():
    """C08: a plan that gains a condition keeps the order of the runs it had."""
    schema = make_schema()
    bigger = FactorSchema(
        schema.declared,
        schema.order,
        {**schema.conditions, "mount-none": {"data_path": "mount", "tools": "none"}},
    )
    small, large = make_manifest(schema, 9), make_manifest(bigger, 9)
    assert len(large.rows) == len(small.rows) * 4 // 3
    assert [t for t in texts(large) if t in set(texts(small))] == texts(small)


def test_c08_a_relative_order_that_changed_is_described():
    """C08: runs the two plans share must come out in the same relative order."""
    one, two = texts(make_manifest(make_schema(), 1)), texts(make_manifest(make_schema(), 2))
    assert "relative order" in relative_order_problem(one, two)


def test_r11_a_run_key_listed_twice_is_refused():
    """R11: a duplicate run key is refused."""
    keys = make_keys(make_schema())
    with pytest.raises(PlanError, match="appears more than once"):
        build_manifest([*keys, keys[3]], 1)


@pytest.mark.parametrize("seed", ["1", 1.5, True, None])
def test_the_order_seed_must_be_an_integer(seed):
    with pytest.raises(PlanError, match="order seed must be an integer"):
        build_manifest(make_keys(make_schema()), seed)


# --- the fingerprint ------------------------------------------------------------------------------------------------


def test_the_fingerprint_is_plain_data_that_survives_json_and_is_hashed_canonically():
    schema = make_schema()
    fingerprint = make_fingerprint(schema)
    assert set(fingerprint) == {"schema", "tasks", "models", "caps", "prompts", "tools", "scorer_digest", "configs"}
    assert fingerprint["schema"] == schema.to_json()
    reordered = make_fingerprint(schema, task_ids=("task-b", "task-a"))
    assert fingerprint_sha256(fingerprint) == fingerprint_sha256(reordered)
    assert compare_fingerprints(fingerprint, copy.deepcopy(reordered)).same


OTHER_DIGESTS = {c: sha(f"other {c}") for c in make_schema().conditions}
CONFIGS = {"study.toml": sha("study"), "models.toml": sha("models"), "campaign.toml": sha("campaign")}
CHANGES = {
    "task content": ("tasks", {"task-a": sha("edited"), "task-b": sha("task task-b")}),
    "model settings": ("models", {"m1": sha("edited")}),
    "caps": ("caps", {"attempt_limit": 4, "time_cap_s": 600}),
    "prompt hash": ("prompts", OTHER_DIGESTS),
    "tool hash": ("tools", OTHER_DIGESTS),
    "scorer digest": ("scorer_digest", sha("another scorer")),
    "configuration hash": ("configs", {**CONFIGS, "study.toml": sha("edited")}),
}


@pytest.mark.parametrize("label", CHANGES)
def test_c06_changing_any_fingerprinted_input_is_refused(label):
    """C06: changing any fingerprinted input refuses the resume."""
    schema = make_schema()
    section, value = CHANGES[label]
    comparison = compare_fingerprints(make_fingerprint(schema), make_fingerprint(schema, **{section: value}))
    assert comparison.refused
    assert not comparison.grown


def test_c06_a_changed_condition_or_factor_level_or_order_is_refused():
    """C06: the conditions and the factor schema are fingerprinted too."""
    schema = make_schema()
    base = make_fingerprint(schema)
    moved = FactorSchema(
        schema.declared,
        schema.order,
        {
            **schema.conditions,
            "mount-read": {"data_path": "mount", "tools": "write"},
            "download-write": {"data_path": "download", "tools": "read"},
        },
    )
    reordered = FactorSchema(schema.declared, ["seed", *schema.order[:-1]], schema.conditions)
    for variant in (moved, reordered):
        comparison = compare_fingerprints(base, make_fingerprint(variant))
        assert comparison.refused
        assert not comparison.grown


def test_c06_the_refusal_names_the_path_and_shows_a_short_value_only():
    """C06: a refusal says which input differs and never prints a long value."""
    schema = make_schema()
    comparison = compare_fingerprints(
        make_fingerprint(schema), make_fingerprint(schema, caps={"attempt_limit": 4, "time_cap_s": 600})
    )
    assert comparison.refused == ("caps.attempt_limit: recorded 3, now 4",)
    long_change = compare_fingerprints(
        make_fingerprint(schema), make_fingerprint(schema, scorer_digest=sha("x"))
    ).refused[0]
    assert long_change.startswith("scorer_digest: recorded '")
    assert len(long_change) < 160


def test_c06_removing_a_recorded_task_model_or_config_is_refused_and_never_called_growth():
    """C06: a resume may add to the plan, never take from it."""
    schema = make_schema()
    base = make_fingerprint(schema, task_ids=("task-a", "task-b"), model_keys=("m1", "m2"))
    for smaller in (
        make_fingerprint(schema, task_ids=("task-a",), model_keys=("m1", "m2")),
        make_fingerprint(schema, task_ids=("task-a", "task-b"), model_keys=("m1",)),
    ):
        comparison = compare_fingerprints(base, smaller)
        assert any("now absent" in line for line in comparison.refused)
        assert not comparison.grown


def test_c08_new_tasks_models_and_conditions_are_growth_not_a_different_experiment():
    """C08: the plan may grow by whole tasks, models and conditions."""
    schema = make_schema()
    bigger_schema = FactorSchema(
        schema.declared, schema.order, {**schema.conditions, "mount-none": {"data_path": "mount", "tools": "none"}}
    )
    base = make_fingerprint(schema)
    grown = make_fingerprint(bigger_schema, task_ids=("task-a", "task-b", "task-c"), model_keys=("m1", "m2"))
    comparison = compare_fingerprints(base, grown)
    assert comparison.refused == ()
    assert comparison.grown == (
        "models.m2",
        "prompts.mount-none",
        "schema.conditions.mount-none",
        "tasks.task-c",
        "tools.mount-none",
    )
    assert not comparison.same


def test_c06_a_new_key_inside_a_recorded_entry_is_a_change_not_growth():
    """C06: only whole entries are growth; anything added inside a recorded entry is refused."""
    schema = make_schema()
    base = make_fingerprint(schema, models={"m1": {"settings": sha("m1")}})
    nested = make_fingerprint(schema, models={"m1": {"settings": sha("m1"), "caps": {"turns": 5}}})
    comparison = compare_fingerprints(base, nested)
    assert comparison.refused == ("models.m1.caps.turns: absent when the campaign started, now 5",)
    assert comparison.grown == ()


def test_c06_new_campaign_wide_inputs_are_refused_not_growth():
    """C06: a new cap or configuration file is a different experiment."""
    schema = make_schema()
    base = make_fingerprint(schema)
    for section, value in (
        ("caps", {"attempt_limit": 3, "time_cap_s": 600, "budget": 10}),
        (
            "configs",
            {
                "study.toml": sha("study"),
                "models.toml": sha("models"),
                "campaign.toml": sha("campaign"),
                "platform.toml": sha("p"),
            },
        ),
    ):
        comparison = compare_fingerprints(base, make_fingerprint(schema, **{section: value}))
        assert comparison.refused
        assert comparison.grown == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tasks", {}, "tasks must be a non-empty mapping"),
        ("tasks", {"task-a": "not-a-digest"}, r"tasks\['task-a'\] must be a 64-character"),
        ("models", {}, "models must be a non-empty mapping"),
        ("prompts", {"mount-read": sha("p")}, "must cover exactly the conditions"),
        (
            "tools",
            {"mount-read": sha("p"), "api-none": sha("p"), "download-write": sha("p"), "extra": sha("p")},
            "unexpected",
        ),
        ("scorer_digest", "abc", "scorer_digest must be a 64-character"),
        ("configs", {"study.toml": 12}, r"configs\['study.toml'\] must be a 64-character"),
        ("caps", {"limit": float("nan")}, "not finite"),
        ("caps", {"limit": object()}, "not plain data"),
    ],
)
def test_the_fingerprint_takes_digests_and_plain_numbers_only(field, value, message):
    with pytest.raises(PlanError, match=message):
        make_fingerprint(make_schema(), **{field: value})


def test_building_a_fingerprint_needs_the_schema_s_conditions_to_be_covered():
    schema = make_schema()
    with pytest.raises(PlanError):
        build_fingerprint(
            schema,
            tasks={"t": sha("t")},
            models={"m": sha("m")},
            caps={},
            prompts={},
            tools={},
            scorer_digest=sha("s"),
            configs={"c": sha("c")},
        )
