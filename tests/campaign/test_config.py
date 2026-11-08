"""campaign.toml: validated when it is loaded, with messages that name the file and the key."""

import pytest

from expcore.campaign.config import load_campaign
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


def test_a_missing_file_and_bad_toml_name_the_file(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_campaign(tmp_path / "campaign.toml")
    refused(tmp_path, "order_seed = ", "not valid TOML")
