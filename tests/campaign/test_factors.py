"""The factor schema: loading and validation of study.toml, run keys, and the plan (R11, C18)."""

import hashlib

import pytest
from campaign_support import STUDY_TOML, chain_schema, make_schema, treatment_schema, write_study

from expcore import errors
from expcore.campaign.factors import FactorSchema, PlanError, RunKey, load_factor_schema
from expcore.config.loading import ConfigError


def test_the_study_file_loads_into_a_schema(tmp_path):
    schema = load_factor_schema(write_study(tmp_path))
    assert list(schema.declared) == ["data_path", "tools"]
    assert schema.order == ("task", "data_path", "tools", "model", "seed")
    assert list(schema.conditions) == ["mount-read", "api-none", "download-write"]
    assert schema.to_json() == make_schema().to_json()
    assert schema.digest == make_schema().digest


def test_other_tables_of_the_study_file_are_not_this_loaders_business(tmp_path):
    path = write_study(tmp_path, STUDY_TOML + '\n[scorers]\nmain = "anything"\n')
    assert load_factor_schema(path).digest == make_schema().digest


def test_a_schema_with_no_declared_factors_is_one_condition_over_the_built_ins(tmp_path):
    path = write_study(tmp_path, '[identity]\norder = ["task", "model", "seed"]\n\n[conditions.only]\n')
    keys = load_factor_schema(path).expand(["task-a"], ["m1"], [1])
    assert [k.text for k in keys] == ["task=task-a|model=m1|seed=1"]


def load_text(tmp_path, text):
    return load_factor_schema(write_study(tmp_path, text))


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (
            ('tools = "read"\n\n[conditions.api-none]', "\n[conditions.api-none]"),
            "[conditions.mount-read]: key 'tools' is missing",
        ),
        (
            ('tools = "read"', 'tools = "read"\nspeed = "fast"'),
            "[conditions.mount-read]: key 'speed' is not a declared factor",
        ),
        (('tools = "read"', 'tools = "append"'), "[conditions.mount-read]: key 'tools' has unknown level 'append'"),
        (('tools = "read"', 'tools = "read"\nmodel = "m1"'), "[conditions.mount-read]: key 'model' is built in"),
        (
            ('data_path = "api"\ntools = "none"', 'data_path = "mount"\ntools = "read"'),
            "duplicates condition 'mount-read'",
        ),
        (
            (
                'order = ["task", "data_path", "tools", "model", "seed"]',
                'order = ["task", "data_path", "model", "seed"]',
            ),
            "[identity]: key 'order' omits factor 'tools'",
        ),
        (
            (
                'order = ["task", "data_path", "tools", "model", "seed"]',
                'order = ["task", "data_path", "tools", "tools", "model", "seed"]',
            ),
            "[identity]: key 'order' repeats factor 'tools'",
        ),
        (
            (
                'order = ["task", "data_path", "tools", "model", "seed"]',
                'order = ["task", "data_path", "tools", "model", "seed", "dose"]',
            ),
            "[identity]: key 'order' names unknown factor 'dose'",
        ),
        (
            ('levels = ["none", "read", "write"]', 'levels = ["none", "none"]'),
            "[factors.tools]: key 'levels' repeats 'none'",
        ),
        (
            ('levels = ["none", "read", "write"]', "levels = []"),
            "[factors.tools]: key 'levels' must be a non-empty array",
        ),
        (
            ('[factors.tools]\nlevels = ["none", "read", "write"]', '[factors.seed]\nlevels = ["a"]'),
            "[factors]: key 'seed' is built in",
        ),
        (("[factors.tools]", "[factors.Tools]"), "[factors]: key 'Tools' must start with a lower-case letter"),
        (("[factors.tools]", '[factors."x\' OR 1"]'), "must start with a lower-case letter"),
        (('levels = ["none", "read", "write"]', 'level = ["none"]'), "[factors.tools]: key 'level' is not a known key"),
    ],
)
def test_a_bad_study_file_is_refused_naming_file_and_key(tmp_path, edit, needle):
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, STUDY_TOML.replace(*edit))
    message = str(caught.value)
    assert message.startswith(str(tmp_path / "study.toml") + ":")
    assert needle in message


def test_a_study_file_without_conditions_or_identity_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"\[conditions\]: needs at least one condition"):
        load_text(tmp_path, '[identity]\norder = ["task", "model", "seed"]\n[conditions]\n')
    with pytest.raises(ConfigError, match="key 'identity' is required"):
        load_text(tmp_path, "[conditions.a]\n")


def test_a_schema_built_in_code_is_validated_like_a_loaded_one():
    with pytest.raises(ConfigError, match=r"<schema>: .*key 'tools' is missing"):
        FactorSchema({"tools": ["a"]}, ["task", "tools", "model", "seed"], {"c": {}})


def test_r11_three_seeds_give_three_distinct_run_keys():
    """R11: expanding a factor schema with three seeds yields three distinct run keys."""
    schema = make_schema()
    keys = schema.expand(["task-a"], ["m1"], [1, 2, 3])
    assert len(keys) == 9  # three conditions x one task x one model x three seeds
    assert len({k.text for k in keys}) == 9
    assert len({k.digest for k in keys}) == 9
    one_condition = [k for k in keys if schema.condition_of(k) == "mount-read"]
    assert [k.factors["seed"] for k in one_condition] == [1, 2, 3]


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ((["task-a", "task-a"], ["m1"], [1]), "task 'task-a' appears more than once"),
        ((["task-a"], ["m1", "m1"], [1]), "model 'm1' appears more than once"),
        ((["task-a"], ["m1"], [1, 2, 1]), "seed 1 appears more than once"),
        (([], ["m1"], [1]), "at least one task"),
        ((["task-a"], ["m1"], []), "at least one seed"),
        ((["task-a"], [""], [1]), "model must be a non-empty string"),
        ((["task-a"], ["m1"], ["1"]), "seed must be an integer"),
        ((["task-a"], ["m1"], [True]), "seed must be an integer"),
    ],
)
def test_r11_a_plan_with_a_repeated_or_unusable_value_is_refused(plan, message):
    """R11: a duplicate run key is refused, here before the plan exists."""
    with pytest.raises(PlanError, match=message):
        make_schema().expand(*plan)


def test_r11_the_same_key_cannot_be_built_twice_with_different_text():
    """R11: a key's text and digest are functions of its names and values alone."""
    first = RunKey(("task", "seed"), ("a", 1))
    assert first == RunKey(("task", "seed"), ("a", 1))
    assert first.digest == hashlib.sha256(first.text.encode()).hexdigest()
    with pytest.raises(PlanError, match="one value for each distinct factor"):
        RunKey(("task", "task"), ("a", "b"))


def test_a_key_is_the_factor_values_in_the_declared_order_joined_canonically():
    key = make_schema().key("mount-read", "task-a", "m1", 3)
    assert key.text == "task=task-a|data_path=mount|tools=read|model=m1|seed=3"
    assert str(key) == key.text
    assert key.factors == {"task": "task-a", "data_path": "mount", "tools": "read", "model": "m1", "seed": 3}
    assert key.factors_json == '{"task":"task-a","data_path":"mount","tools":"read","model":"m1","seed":3}'


def test_no_value_can_forge_the_separator_so_different_runs_never_share_a_key():
    schema = make_schema()
    plain = schema.key("mount-read", "a", "m1", 1)
    forged = schema.key("mount-read", "a|data_path=mount", "m1", 1)
    escaped = schema.key("mount-read", "a%7Cdata_path%3Dmount", "m1", 1)
    assert len({plain.text, forged.text, escaped.text}) == 3
    assert "|data_path=mount|data_path=" not in forged.text


def test_c18_two_conditions_with_the_same_model_produce_disjoint_run_keys():
    """C18: conditions never share attempt keys, even for one route model, task and seed."""
    schema = make_schema()
    keys = {
        c: {k.text for k in schema.expand(["task-a", "task-b"], ["same-model"], [1, 2]) if schema.condition_of(k) == c}
        for c in schema.conditions
    }
    names = list(keys)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert keys[a].isdisjoint(keys[b])


def test_c18_a_schema_with_two_conditions_that_assign_the_same_levels_is_refused():
    """C18: the only way two conditions could share a key is equal levels, and that schema does not load."""
    with pytest.raises(ConfigError, match="duplicates condition 'a'"):
        FactorSchema({"x": ["1", "2"]}, ["task", "x", "model", "seed"], {"a": {"x": "1"}, "b": {"x": "1"}})


def test_the_condition_of_a_key_is_found_from_its_levels():
    schema = make_schema()
    for name in schema.conditions:
        assert schema.condition_of(schema.key(name, "t", "m", 0)) == name
    with pytest.raises(PlanError, match="unknown condition 'nope'"):
        schema.key("nope", "t", "m", 0)


def test_a_key_whose_levels_fit_no_condition_is_refused():
    schema = make_schema()
    stray = RunKey(schema.order, ("task-a", "mount", "write", "m1", 1))  # mount and write are never assigned together
    with pytest.raises(PlanError, match="fits no condition"):
        schema.condition_of(stray)


def test_the_digest_changes_with_any_part_of_the_schema():
    base = make_schema()
    other_order = FactorSchema(base.declared, ["seed", "task", "data_path", "tools", "model"], base.conditions)
    other_levels = FactorSchema(
        {**base.declared, "tools": ["none", "read", "write", "admin"]}, base.order, base.conditions
    )
    assert len({base.digest, other_order.digest, other_levels.digest}) == 3


# --- a condition whose treatment is an infrastructure fault ---------------------------------------------------------

TREATMENT_TOML = STUDY_TOML.replace(
    '[conditions.api-none]\ndata_path = "api"\ntools = "none"',
    '[conditions.api-none]\ndata_path = "api"\ntools = "none"\ninfrastructure_is_treatment = ["sandbox", "endpoint"]',
)


@pytest.fixture
def registry(monkeypatch):
    """Start with only the core's infrastructure kinds and leave no platform kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})
    monkeypatch.setattr(errors, "_platform_untreatable", {})


def test_c02_a_condition_may_declare_infrastructure_kinds_as_its_treatment(tmp_path, registry):
    """C02: the declared kinds come from the closed vocabulary, and the setting is no factor of the condition."""
    schema = load_factor_schema(write_study(tmp_path, TREATMENT_TOML))
    assert schema.treatment_kinds == {"mount-read": (), "api-none": ("endpoint", "sandbox"), "download-write": ()}
    assert schema.conditions["api-none"] == {"data_path": "api", "tools": "none"}
    assert schema.to_json()["treatment"] == {
        "mount-read": [],
        "api-none": ["endpoint", "sandbox"],
        "download-write": [],
    }
    assert [k.text for k in schema.expand(["task-a"], ["m1"], [1])] == [
        k.text for k in make_schema().expand(["task-a"], ["m1"], [1])
    ]


def test_c06_the_treatment_declaration_is_part_of_the_schema_s_identity(registry):
    """C06: a schema that declares a treatment differs in digest from one that does not, so a resume sees it."""
    assert treatment_schema().digest != make_schema().digest
    assert treatment_schema(("endpoint",)).digest != treatment_schema(("endpoint", "sandbox")).digest
    assert treatment_schema(("sandbox", "endpoint")).digest == treatment_schema(("endpoint", "sandbox")).digest


def test_c02_an_unknown_infrastructure_kind_is_refused_naming_file_and_key(tmp_path, registry):
    """C02: a kind outside the registry is refused at load, as every other bad value is."""
    path = write_study(tmp_path, TREATMENT_TOML.replace('"sandbox", "endpoint"', '"endpoint", "wifi"'))
    with pytest.raises(ConfigError) as caught:
        load_factor_schema(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: [conditions.api-none]: key 'infrastructure_is_treatment'")
    assert "unknown infrastructure kind 'wifi'" in message
    assert "known kinds: endpoint" in message


def test_c02_a_kind_a_platform_registered_is_accepted_and_refused_before_it_is(tmp_path, registry):
    """C02: the registry is the platform's too, so a kind it adds can be a treatment."""
    path = write_study(tmp_path, TREATMENT_TOML.replace('"sandbox", "endpoint"', '"provider_down"'))
    with pytest.raises(ConfigError, match="unknown infrastructure kind 'provider_down'"):
        load_factor_schema(path)
    errors.register_infra_kind("provider_down", "a platform's own kind")
    assert load_factor_schema(path).treatment_kinds["api-none"] == ("provider_down",)


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ('["endpoint", "endpoint"]', "key 'infrastructure_is_treatment' repeats 'endpoint'"),
        (
            '"endpoint"',
            "key 'infrastructure_is_treatment' must be an array of infrastructure kinds, each a name or a table",
        ),
        ("[1]", "key 'infrastructure_is_treatment' must be an array of infrastructure kinds, each a name or a table"),
    ],
)
def test_c02_a_malformed_treatment_declaration_is_refused(tmp_path, registry, declaration, message):
    """C02: the declaration is an array of distinct kinds."""
    path = write_study(tmp_path, TREATMENT_TOML.replace('["sandbox", "endpoint"]', declaration))
    with pytest.raises(ConfigError, match=message):
        load_factor_schema(path)


def test_c02_a_treatment_for_a_condition_that_does_not_exist_is_refused(registry):
    """C02: a declaration built in code names a real condition."""
    with pytest.raises(ConfigError, match="key 'nope' declares infrastructure_is_treatment but is no condition"):
        FactorSchema({}, ["task", "model", "seed"], {"a": {}}, treatment={"nope": ["endpoint"]})


def test_c02_the_setting_s_name_cannot_also_be_a_factor(registry):
    """C02: the name is reserved, so a condition's setting is never read as a level."""
    with pytest.raises(ConfigError, match="key 'infrastructure_is_treatment' is reserved"):
        FactorSchema(
            {"infrastructure_is_treatment": ["a"]},
            ["task", "infrastructure_is_treatment", "model", "seed"],
            {"c": {"infrastructure_is_treatment": "a"}},
        )


# --- a condition that runs on a subset of the tasks -----------------------------------------------------------------

CHAIN_TOML = STUDY_TOML.replace(
    '[conditions.download-write]\ndata_path = "download"\ntools = "write"',
    '[conditions.download-write]\ndata_path = "download"\ntools = "write"\ntasks = ["task-b", "task-a"]',
)
PLAN_TASKS = ["task-a", "task-b", "task-c", "task-d"]


def test_r11_a_condition_with_a_task_list_is_expanded_over_its_own_tasks_only():
    """R11: adds that a chain design's subset arm gets keys for its own tasks, and the other arms keep every task."""
    schema = chain_schema(("task-b", "task-a"))
    keys = schema.expand(PLAN_TASKS, ["m1"], [1, 2])
    by_condition = {c: [k for k in keys if schema.condition_of(k) == c] for c in schema.conditions}
    assert {c: len(ks) for c, ks in by_condition.items()} == {"mount-read": 8, "api-none": 8, "download-write": 4}
    assert {k.factors["task"] for k in by_condition["download-write"]} == {"task-a", "task-b"}
    assert [k.factors["task"] for k in by_condition["download-write"]][:2] == ["task-a", "task-a"]  # the plan's order
    assert len({k.text for k in keys}) == len(keys) == 20


def test_r11_a_condition_without_a_task_list_runs_on_every_task_of_the_plan():
    """R11: adds the default: no list, every task of the plan, so existing schemas expand as before."""
    schema = make_schema()
    assert schema.condition_tasks == {"mount-read": None, "api-none": None, "download-write": None}
    assert len(schema.expand(PLAN_TASKS, ["m1"], [1])) == 12


def test_r11_the_study_file_may_list_a_conditions_tasks_and_the_list_is_no_factor(tmp_path):
    """R11: adds the loading of `tasks` in a condition, apart from the factors the condition assigns."""
    schema = load_factor_schema(write_study(tmp_path, CHAIN_TOML))
    assert schema.condition_tasks == {"mount-read": None, "api-none": None, "download-write": ("task-b", "task-a")}
    assert schema.conditions["download-write"] == {"data_path": "download", "tools": "write"}
    assert schema.to_json()["condition_tasks"]["download-write"] == {"task-b": True, "task-a": True}


def test_r11_a_listed_task_that_is_not_in_the_plan_is_refused_naming_file_and_key(tmp_path):
    """R11: adds that a task list is checked against the plan's tasks when the plan is expanded."""
    path = write_study(tmp_path, CHAIN_TOML.replace('"task-b", "task-a"', '"task-a", "task-zz"'))
    schema = load_factor_schema(path)
    with pytest.raises(ConfigError) as caught:
        schema.expand(PLAN_TASKS, ["m1"], [1])
    message = str(caught.value)
    assert message.startswith(f"{path}: [conditions.download-write]: key 'tasks'")
    assert "names unknown task 'task-zz'" in message
    assert "the plan's tasks are: task-a, task-b, task-c, task-d" in message


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("[]", "key 'tasks' must be a non-empty array of task names"),
        ('["task-a", "task-a"]', "key 'tasks' repeats 'task-a'"),
        ('"task-a"', "key 'tasks' must be an array of non-empty strings"),
        ("[1]", "key 'tasks' must be an array of non-empty strings"),
    ],
)
def test_r11_a_malformed_task_list_is_refused_at_load(tmp_path, declaration, message):
    """R11: adds the shape checks on a condition's task list."""
    path = write_study(tmp_path, CHAIN_TOML.replace('["task-b", "task-a"]', declaration))
    with pytest.raises(ConfigError, match=message):
        load_factor_schema(path)


def test_r11_a_task_list_for_a_condition_that_does_not_exist_is_refused():
    """R11: adds that a list built in code names a real condition."""
    with pytest.raises(ConfigError, match="key 'nope' declares tasks but is no condition"):
        FactorSchema({}, ["task", "model", "seed"], {"a": {}}, tasks={"nope": ["task-a"]})


def test_r11_the_name_tasks_cannot_also_be_a_factor():
    """R11: adds that the name is reserved, so a condition's list is never read as a level."""
    with pytest.raises(ConfigError, match="key 'tasks' is reserved"):
        FactorSchema({"tasks": ["a"]}, ["task", "tasks", "model", "seed"], {"c": {"tasks": "a"}})


def test_r11_a_key_for_a_task_the_condition_does_not_run_is_refused():
    """R11: adds that no key can be made, or accepted, for a task outside a condition's list."""
    schema = chain_schema(("task-a",))
    assert schema.key("download-write", "task-a", "m1", 1).factors["task"] == "task-a"
    with pytest.raises(PlanError, match="task 'task-b' is not one of the tasks of condition 'download-write': task-a"):
        schema.key("download-write", "task-b", "m1", 1)
    outside = make_schema().key("download-write", "task-b", "m1", 1)  # made under a schema that has no such list
    with pytest.raises(PlanError, match="is on a task that condition 'download-write' does not run"):
        schema.check_key(outside)
    assert schema.check_key(make_schema().key("download-write", "task-a", "m1", 1)) == "download-write"


def test_c06_the_task_lists_are_part_of_the_schema_s_identity_whatever_their_order():
    """C06: adds that a changed list changes the digest, and a reordered one does not."""
    assert chain_schema(("task-a", "task-b")).digest != make_schema().digest
    assert chain_schema(("task-a", "task-b")).digest != chain_schema(("task-a",)).digest
    assert chain_schema(("task-a", "task-b")).digest == chain_schema(("task-b", "task-a")).digest


def test_c06_the_conditions_treatment_kinds_are_found_from_a_run_key(registry):
    """C06: adds the lookup the campaign passes to the record, from a run key to its condition's declared kinds."""
    schema = treatment_schema(("sandbox", "endpoint"))
    declared = schema.key("api-none", "task-a", "m1", 1)
    other = schema.key("mount-read", "task-a", "m1", 1)
    assert schema.treatment_kinds_of(declared) == ("endpoint", "sandbox")
    assert schema.treatment_kinds_of(other) == ()


def test_o05_declaring_tool_plane_integrity_as_treatment_is_refused_at_load_naming_file_and_key(tmp_path, registry):
    """O05: a run that failed an integrity check of its tool plane can never be scored as a condition's manipulation,
    so the schema refuses the declaration when it is loaded, naming the file, the condition and the key."""
    path = write_study(tmp_path, TREATMENT_TOML.replace('"sandbox", "endpoint"', '"endpoint", "tool_plane_integrity"'))
    with pytest.raises(ConfigError) as caught:
        load_factor_schema(path)
    message = str(caught.value)
    assert message.startswith(f"{path}: [conditions.api-none]: key 'infrastructure_is_treatment'")
    assert "'tool_plane_integrity'" in message
    assert "can never be a treatment" in message


def test_o05_a_kind_a_platform_registered_as_not_treatable_is_refused_and_a_treatable_one_is_not(tmp_path, registry):
    """O05: the refusal is the registry's property, so it covers a platform's kinds too."""
    errors.register_infra_kind("provider_tampered", "a provider whose state was changed", treatable=False)
    errors.register_infra_kind("provider_down", "a provider that is down")
    declared = TREATMENT_TOML.replace('"sandbox", "endpoint"', '"provider_down"')
    assert load_factor_schema(write_study(tmp_path, declared)).treatment_kinds["api-none"] == ("provider_down",)
    with pytest.raises(
        ConfigError, match=r"key 'infrastructure_is_treatment' names infrastructure kind 'provider_tamp"
    ):
        load_factor_schema(write_study(tmp_path, declared.replace('"provider_down"', '"provider_tampered"')))


def test_o05_a_schema_built_in_code_refuses_an_untreatable_kind_too(registry):
    """O05: the check is the schema's, not the loader's."""
    with pytest.raises(ConfigError, match="can never be a treatment"):
        treatment_schema(("tool_plane_integrity",))
