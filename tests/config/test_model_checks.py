"""Value checks on a model entry, refused at load with the file and the key: thinking budget (B16), types, URL."""

import textwrap

import pytest

from expcore.config.loading import ConfigError
from expcore.config.model_checks import base_url_problems, sampling_problems, thinking_problems
from expcore.config.models import ModelSpec, RuleViolation, load_models

ANTHROPIC = """
[models.claude]
backend = "anthropic"
api_id = "claude-test"
base_url = "https://api.example.invalid"
key_env = "TEST_KEY"
{head}
{body}
"""


def load(tmp_path, text):
    path = tmp_path / "models.toml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return load_models(path)


def anthropic(tmp_path, *, head="", body=""):
    return load(tmp_path, ANTHROPIC.format(head=head, body=body))


def openai_entry(*, sampling="", base_url="https://llm.example.invalid/v1"):
    return f'[models.local]\nbackend = "openai_compat"\napi_id = "m"\nbase_url = "{base_url}"\n{sampling}\n'


def test_b16_a_thinking_budget_at_or_above_the_output_maximum_is_refused_at_load_naming_file_and_key(tmp_path):
    """B16: the Messages API needs budget_tokens below max_tokens, so the entry can never send a valid request."""
    body = '[models.claude.extra_body.thinking]\ntype = "enabled"\nbudget_tokens = 4096\n'
    for limit in (4096, 2048):
        with pytest.raises(ConfigError) as caught:
            anthropic(tmp_path, head=f"max_output_tokens = {limit}", body=body)
        message = str(caught.value)
        assert message.startswith(f"{tmp_path / 'models.toml'}: [models.claude]: ")
        assert "extra_body.thinking.budget_tokens (4096) must be smaller than max_output_tokens" in message


def test_b16_a_thinking_budget_below_the_output_maximum_loads(tmp_path):
    """B16: One token of room is enough for the rule; the budget below the maximum loads."""
    body = '[models.claude.extra_body.thinking]\ntype = "enabled"\nbudget_tokens = 4095\n'
    table = anthropic(tmp_path, head="max_output_tokens = 4096", body=body)
    assert table.resolve("claude").extra_body["thinking"]["budget_tokens"] == 4095


def test_b16_the_budget_is_compared_only_where_there_is_something_to_compare(tmp_path):
    """B16: No output maximum on the entry, no thinking table, or another backend: the rule has nothing to say."""
    body = '[models.claude.extra_body.thinking]\ntype = "enabled"\nbudget_tokens = 99999\n'
    anthropic(tmp_path, body=body)  # the study's own limit is not known when the table is loaded
    anthropic(tmp_path, head="max_output_tokens = 10", body='[models.claude.extra_body.output_config]\neffort = "x"\n')
    assert thinking_problems("openai_compat", {"thinking": {"budget_tokens": 5}}, 4) == []
    assert thinking_problems("anthropic", {"thinking": {"type": "disabled"}}, 4) == []


def test_b16_a_budget_that_is_not_an_integer_is_refused_not_compared(tmp_path):
    """B16: A budget of the wrong type is a configuration error, not a crash in the comparison."""
    body = '[models.claude.extra_body.thinking]\ntype = "enabled"\nbudget_tokens = "big"\n'
    with pytest.raises(
        ConfigError, match=r"models\.toml: \[models\.claude\]: .*budget_tokens must be an integer, got str"
    ):
        anthropic(tmp_path, head="max_output_tokens = 4096", body=body)
    assert thinking_problems("anthropic", {"thinking": {"budget_tokens": True}}, 4) != []


def test_b16_a_thinking_budget_in_an_entry_built_in_memory_is_refused_too():
    """B16: The rule lives in the entry, so a table built in code cannot skip it."""
    with pytest.raises(RuleViolation, match="budget_tokens"):
        ModelSpec(
            key="m",
            backend="anthropic",
            api_id="x",
            status="frozen",
            base_url="https://api.example.invalid",
            key_env="K",
            max_output_tokens=1024,
            extra_body={"thinking": {"type": "enabled", "budget_tokens": 1024}},
        )


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('temperature = "0.2"', "sampling.temperature must be a number, got str"),
        ("temperature = true", "sampling.temperature must be a number, got bool"),
        ("temperature = nan", "sampling.temperature must be a finite number"),
        ("top_p = [0.9]", "sampling.top_p must be a number, got list"),
        ("top_p = inf", "sampling.top_p must be a finite number"),
        ("top_k = 20.5", "sampling.top_k must be an integer, got float"),
        ("top_k = true", "sampling.top_k must be an integer, got bool"),
        ('top_k = "20"', "sampling.top_k must be an integer, got str"),
    ],
)
def test_a_sampling_value_of_the_wrong_type_is_refused_at_load_naming_file_and_key(tmp_path, line, message):
    with pytest.raises(ConfigError) as caught:
        anthropic(tmp_path, body=f"[models.claude.sampling]\n{line}\n")
    assert str(caught.value).startswith(f"{tmp_path / 'models.toml'}: [models.claude]: ")
    assert message in str(caught.value)


def test_numbers_and_integers_of_the_right_type_load(tmp_path):
    text = openai_entry(sampling="[models.local.sampling]\ntemperature = 1\ntop_p = 0.95\ntop_k = 40\n")
    assert load(tmp_path, text).resolve("local").sampling == {"temperature": 1, "top_p": 0.95, "top_k": 40}
    assert sampling_problems({"seed": 3}) == []
    assert sampling_problems({"seed": 3.0, "top_k": None}) != []


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://llm.example.invalid:99999/v1", "base_url has an invalid port"),
        ("http://llm.example.invalid:abc/v1", "base_url has an invalid port"),
        ("http://llm.example.invalid:0/v1", "base_url has an invalid port"),
        ("http://[::1/v1", "base_url cannot be parsed as a URL"),
        ("ftp://llm.example.invalid/v1", "base_url must be an http or https URL"),
        ("llm.example.invalid/v1", "base_url must be an http or https URL"),
        ("http:///v1", "base_url must be an http or https URL"),
        ("https://:8080/v1", "base_url must be an http or https URL"),
        ("https://user:pw@llm.example.invalid/v1", "base_url must not carry credentials"),
    ],
)
def test_a_base_url_that_cannot_be_used_is_refused_at_load_naming_file_and_key(tmp_path, url, message):
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, openai_entry(base_url=url))
    assert str(caught.value).startswith(f"{tmp_path / 'models.toml'}: [models.local]: ")
    assert message in str(caught.value)


def test_a_refused_base_url_is_not_echoed_because_it_may_carry_a_secret(tmp_path):
    url = "http://user:s3cret-value@[::1/v1"
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, openai_entry(base_url=url))
    assert "s3cret" not in str(caught.value)
    assert "s3cret" not in "; ".join(base_url_problems(url))


@pytest.mark.parametrize(
    "url",
    [
        "https://llm.example.invalid/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:65535",
        "https://[::1]:8443/v1",
        "HTTPS://LLM.example.invalid",
    ],
)
def test_a_usable_base_url_loads(tmp_path, url):
    assert load(tmp_path, openai_entry(base_url=url)).resolve("local").base_url == url
