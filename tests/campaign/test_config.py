"""campaign.toml: validated when it is loaded, with messages that name the file and the key."""

import pytest

from expcore.campaign.config import load_campaign
from expcore.campaign.shard import ModelShards
from expcore.config.loading import ConfigError

VALID = """\
order_seed = 7
seeds = [1, 2]
models = ["m-a", "m-b"]

[agent]
max_tool_rounds = 4
time_cap_s = 30
token_budget = 20000
output_cap_chars = 500
max_output_tokens = 256

[scheduler]
max_attempts = 2
workers = 2
retry_delay_s = 0

[scheduler.unit_caps]
m-a = 1

[budget]
max_usd = 5
per_model_usd = { m-a = 2.5 }

[endpoints.m-a]
unit = "pool"
"""


def load(tmp_path, text=VALID):
    path = tmp_path / "campaign.toml"
    path.write_text(text, encoding="utf-8")
    return load_campaign(path)


def refused(tmp_path, text, *fragments):
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, text)
    message = str(caught.value)
    assert "campaign.toml" in message
    for fragment in fragments:
        assert fragment in message
    return message


def test_a_valid_file_gives_the_plan_the_caps_the_limits_and_the_budget(tmp_path):
    campaign = load(tmp_path)
    assert (campaign.order_seed, campaign.seeds, campaign.models) == (7, (1, 2), ("m-a", "m-b"))
    assert campaign.agent.limits.max_tool_rounds == 4
    assert campaign.agent.limits.time_cap_s == 30.0
    assert (campaign.scheduler.max_attempts, campaign.scheduler.workers) == (2, 2)
    assert dict(campaign.scheduler.unit_caps) == {"m-a": 1}
    assert (campaign.scheduler.budget.max_usd, dict(campaign.scheduler.budget.per_model_usd)) == (5.0, {"m-a": 2.5})
    assert {m: (e.unit, e.base_url) for m, e in campaign.endpoints.items()} == {
        "m-a": ("pool", None),
        "m-b": ("m-b", None),
    }


def test_the_caps_are_plain_data_for_the_fingerprint(tmp_path):
    caps = load(tmp_path).caps()
    assert caps["max_attempts"] == 2
    assert caps["budget"]["max_usd"] == 5.0
    assert caps["agent"]["limits"]["max_tool_rounds"] == 4


def test_a_misspelt_key_is_refused_naming_it(tmp_path):
    refused(tmp_path, VALID.replace("seeds =", "seed ="), "'seed'", "not a known key")
    refused(tmp_path, VALID.replace("workers", "worker"), "[scheduler]", "'worker'")
    refused(tmp_path, VALID.replace("max_usd", "max_dollars"), "[budget]", "'max_dollars'")


@pytest.mark.parametrize(
    ("old", "new", "fragments"),
    [
        ("order_seed = 7\n", "", ("order_seed", "is required")),
        ("seeds = [1, 2]", "seeds = []", ("seeds", "non-empty array of integers")),
        ("seeds = [1, 2]", 'seeds = ["a"]', ("seeds", "integers")),
        ('models = ["m-a", "m-b"]', 'models = ["m-a", "m-a"]', ("models", "more than once")),
        ("max_tool_rounds = 4", 'max_tool_rounds = "four"', ("[agent]", "max_tool_rounds", "must be a int")),
        ("time_cap_s = 30\n", "", ("[agent]", "time_cap_s", "is required")),
        ("time_cap_s = 30", "time_cap_s = true", ("time_cap_s", "must be a float")),
        ("workers = 2", "workers = 0", ("[scheduler]", "at least 1")),
        ("m-a = 1", "m-a = 0", ("unit cap",)),
        ("max_usd = 5", "max_usd = 0", ("[budget]", "max_usd")),
        ("[endpoints.m-a]", "[endpoints.m-z]", ("[endpoints]", "m-z", "not one of the plan's models")),
    ],
)
def test_a_value_that_cannot_work_is_refused_naming_the_table_and_the_key(tmp_path, old, new, fragments):
    refused(tmp_path, VALID.replace(old, new, 1), *fragments)


SHARDED = (
    VALID.replace("[scheduler.unit_caps]\nm-a = 1", "[scheduler.unit_caps]\npool = 3\nm-b = 2")
    + "\n[shards]\nper_model = { m-b = 2, m-a = 3 }\n"
)


def test_c32_shards_per_model_is_read_in_declared_order_and_enters_the_caps_only_when_declared(tmp_path):
    """C32: the layout keeps the order the file declares (m-b's shards come first), as a list the fingerprint's
    canonical text cannot sort; a campaign without [shards] has the caps it always had."""
    campaign = load(tmp_path, SHARDED)
    assert campaign.shards == ModelShards((("m-b", 2), ("m-a", 3)))
    assert campaign.caps()["shards"] == {"per_model": [["m-b", 2], ["m-a", 3]]}
    plain = load(tmp_path)
    assert plain.shards is None
    assert set(plain.caps()) == {"agent", "max_attempts", "budget"}


@pytest.mark.parametrize(
    ("old", "new", "fragments"),
    [
        ("m-a = 3 }", "m-a = 4 }", ("[shards.per_model]", "'m-a'", "is 4, above", "[scheduler.unit_caps] pool = 3")),
        ("m-b = 2, m-a", "m-b = 3, m-a", ("'m-b'", "is 3, above", "[scheduler.unit_caps] m-b = 2")),
        ("m-a = 3 }", "m-a = 0 }", ("'m-a'", "is 0", "no shard runs")),
        ("m-a = 3 }", 'm-a = "3" }', ("'m-a'", "must be a int")),
        ("m-b = 2, m-a = 3", "m-b = 2", ("[shards]", "'per_model'", "no count for model m-a")),
        ("m-a = 3 }", "m-a = 3, m-z = 1 }", ("'m-z'", "not one of the plan's models")),
        ("per_model =", "per_models =", ("[shards]", "'per_models'", "not a known key")),
    ],
    ids=["above its unit's cap", "above its own cap", "zero", "not a number", "one left out", "unknown", "misspelt"],
)
def test_c32_a_shard_count_that_cannot_work_is_refused_at_load_naming_the_key(tmp_path, old, new, fragments):
    """C32: each shard runs one run at a time, so a model's count is its concurrent sessions: above its unit's cap it
    is refused; every model of the plan needs a count of at least 1."""
    refused(tmp_path, SHARDED.replace(old, new, 1), *fragments)


def test_c32_the_models_of_one_unit_are_held_to_its_cap_together(tmp_path):
    """C32: two models on one unit share its cap, so their shard counts are summed against it."""
    text = SHARDED + '\n[endpoints.m-b]\nunit = "pool"\n'  # m-b counts in pool too
    refused(tmp_path, text, "'m-b'", "with the other models of unit pool gives m-b 2 + m-a 3 = 5 shards", "pool = 3")


def test_c32_a_unit_without_a_cap_takes_any_count_but_a_layout_needs_two_shards(tmp_path):
    """C32: an uncapped unit has nothing to exceed; one shard in all is not a sharded campaign."""
    assert load(tmp_path, SHARDED.replace("pool = 3\n", "").replace("m-a = 3 }", "m-a = 40 }")).shards.count == 42
    one = SHARDED.replace('models = ["m-a", "m-b"]', 'models = ["m-a"]').replace("m-b = 2, m-a = 3", "m-a = 1")
    refused(tmp_path, one.replace("per_model_usd = { m-a = 2.5 }", ""), "'per_model'", "sums to 1", "at least 2")


def test_a_missing_file_and_bad_toml_name_the_file(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_campaign(tmp_path / "campaign.toml")
    refused(tmp_path, "order_seed = ", "not valid TOML")


def test_l17_tool_server_call_bound_s_is_optional_and_enters_the_caps_only_when_declared(tmp_path):
    """L17: `[agent] tool_server_call_bound_s` is read as seconds (an integer may stand for a float). Undeclared, the
    loop's caps in the fingerprint are what they were before the key existed; declared, they differ, so a store started
    without it cannot be resumed with it."""
    plain = load(tmp_path)
    assert plain.agent.tool_server_call_bound_s is None
    assert "tool_server_call_bound_s" not in plain.caps()["agent"]
    declared = load(
        tmp_path, VALID.replace("max_output_tokens = 256", "max_output_tokens = 256\ntool_server_call_bound_s = 300")
    )
    assert declared.agent.tool_server_call_bound_s == 300.0
    assert declared.caps()["agent"]["tool_server_call_bound_s"] == 300.0
    assert declared.caps() != plain.caps()


@pytest.mark.parametrize(
    ("value", "fragments"),
    [
        ("0", ("[agent]", "tool_server_call_bound_s", "positive")),
        ("-5", ("[agent]", "tool_server_call_bound_s", "positive")),
        ("inf", ("[agent]", "tool_server_call_bound_s", "finite")),
        ("nan", ("[agent]", "tool_server_call_bound_s", "finite")),
        ('"300"', ("[agent]", "tool_server_call_bound_s", "must be a float")),
        ("true", ("[agent]", "tool_server_call_bound_s", "must be a float")),
    ],
)
def test_l17_a_tool_call_bound_that_cannot_work_is_refused_naming_the_table_and_the_key(tmp_path, value, fragments):
    """L17: refused at load, naming the file, the table and the key."""
    text = VALID.replace("max_output_tokens = 256", f"max_output_tokens = 256\ntool_server_call_bound_s = {value}")
    refused(tmp_path, text, *fragments)


def test_l17_the_old_name_of_the_bound_is_refused_naming_the_new_key_and_why(tmp_path):
    """L17: `[agent] tool_call_bound_s` capped every tool call, the code tool's included, which was not what it was
    for. It was renamed `tool_server_call_bound_s` so that its name says its scope, and the old key is refused at load
    with the new name and the reason, not read under the new meaning and not left to the generic message of an unknown
    key."""
    text = VALID.replace("max_output_tokens = 256", "max_output_tokens = 256\ntool_call_bound_s = 300")
    message = refused(tmp_path, text, "[agent]", "tool_call_bound_s", "tool_server_call_bound_s", "tool server")
    assert "is not a known key" not in message


def test_l17_the_old_name_is_refused_even_beside_the_new_one(tmp_path):
    """L17: a file that declares both is refused too: the old key is never quietly dropped."""
    both = "max_output_tokens = 256\ntool_call_bound_s = 300\ntool_server_call_bound_s = 300"
    refused(tmp_path, VALID.replace("max_output_tokens = 256", both), "tool_call_bound_s", "tool_server_call_bound_s")
