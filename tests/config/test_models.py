"""The model table: loading and validation, one entry per model (P06), frozen status (B08), rules (B16)."""

import textwrap

import pytest

from expcore.config.loading import ConfigError
from expcore.config.models import ClientSettings, ModelSpec, ModelTable, RuleViolation, assert_frozen, load_models

TABLE = """
[models.alpha]
backend = "openai_compat"
api_id = "vendor/alpha-1"
aliases = ["Alpha One"]
base_url = "https://llm.example.invalid/v1"
key_env = "TEST_ALPHA_KEY"
status = "frozen"
max_output_tokens = 4096
context_tokens = 32768
echo_reasoning_field = "reasoning_content"

[models.alpha.sampling]
temperature = 0.2

[models.alpha.extra_body]
chat_template_kwargs = { enable_thinking = true }

[models.beta]
backend = "anthropic"
api_id = "beta-large"
base_url = "https://api.example.invalid"
key_file = "~/keys/beta"
prompt_cache_ttl = "1h"

[models.scripted]
backend = "stub"
"""


def write(tmp_path, text, name="models.toml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def spec(**overrides):
    base = {
        "key": "m",
        "backend": "openai_compat",
        "api_id": "test-model",
        "status": "frozen",
        "base_url": "https://llm.example.invalid/v1",
    }
    return ModelSpec(**{**base, **overrides})


def test_the_table_loads_every_field(tmp_path):
    table = load_models(write(tmp_path, TABLE))
    alpha = table.resolve("alpha")
    assert (alpha.backend, alpha.api_id, alpha.status) == ("openai_compat", "vendor/alpha-1", "frozen")
    assert alpha.aliases == ("Alpha One",)
    assert alpha.base_url == "https://llm.example.invalid/v1"
    assert alpha.key_env == "TEST_ALPHA_KEY"
    assert alpha.sampling == {"temperature": 0.2}
    assert alpha.max_output_tokens == 4096
    assert alpha.context_tokens == 32768
    assert alpha.echo_reasoning_field == "reasoning_content"
    assert alpha.extra_body == {"chat_template_kwargs": {"enable_thinking": True}}
    beta = table.resolve("beta")
    assert (beta.key_file, beta.prompt_cache_ttl, beta.status) == ("~/keys/beta", "1h", "draft")
    scripted = table.resolve("scripted")
    assert (scripted.api_id, scripted.base_url) == ("scripted", None)
    assert list(table) == ["alpha", "beta", "scripted"]
    assert len(table) == 3


def test_p06_every_name_resolves_to_exactly_one_entry(tmp_path):
    """P06: a key, an API id and an alias each reach the same single entry, whatever their case."""
    table = load_models(write(tmp_path, TABLE))
    for name in ("alpha", "ALPHA", "vendor/alpha-1", "Vendor/Alpha-1", "Alpha One", "alpha one"):
        assert table.resolve(name).key == "alpha"
    assert table.resolve("beta-large").key == "beta"


def test_p06_a_name_two_entries_share_is_refused_at_load_and_names_both(tmp_path):
    """P06: the same API id under two keys would make a reported name ambiguous."""
    path = write(
        tmp_path,
        """
        [models.one]
        backend = "stub"
        api_id = "shared"
        [models.two]
        backend = "stub"
        api_id = "SHARED"
        """,
    )
    with pytest.raises(ConfigError, match=r"models 'one' and 'two' both answer to 'SHARED'"):
        load_models(path)


def test_p06_an_alias_that_is_another_models_key_is_refused(tmp_path):
    """P06: An alias that is another models key is refused."""
    path = write(
        tmp_path,
        """
        [models.one]
        backend = "stub"
        aliases = ["two"]
        [models.two]
        backend = "stub"
        """,
    )
    with pytest.raises(ConfigError, match="both answer to 'two'"):
        load_models(path)


def test_p06_a_table_built_in_memory_obeys_the_same_rule():
    """P06: A table built in memory obeys the same rule."""
    one = ModelSpec(key="one", backend="stub", api_id="x", status="draft")
    two = ModelSpec(key="two", backend="stub", api_id="X", status="draft")
    with pytest.raises(ConfigError, match="each model is named once"):
        ModelTable({"one": one, "two": two})


def test_p06_an_unknown_name_is_refused_and_the_table_is_listed(tmp_path):
    """P06: An unknown name is refused and the table is listed."""
    table = load_models(write(tmp_path, TABLE))
    with pytest.raises(ConfigError, match=r"unknown model 'gamma'; the table holds: alpha, beta, scripted"):
        table.resolve("gamma")


def test_p06_an_unknown_name_close_to_a_key_is_refused_with_the_key_it_was_likely_meant_to_be(tmp_path):
    """P06: the table is listed, and the closest key is named."""
    table = load_models(write(tmp_path, TABLE))
    with pytest.raises(
        ConfigError, match=r"unknown model 'alpah' \(did you mean 'alpha'\?\); the table holds: alpha, beta"
    ):
        table.resolve("alpah")


def test_b08_unfrozen_settings_raise_before_any_request(tmp_path):
    """B08: only a frozen model may start a study run."""
    table = load_models(write(tmp_path, TABLE))
    assert_frozen(table.resolve("alpha"))
    with pytest.raises(ConfigError, match="model beta: settings are 'draft', not 'frozen'"):
        assert_frozen(table.resolve("beta"))
    table.require_frozen(["alpha"])
    with pytest.raises(ConfigError, match="model beta"):
        table.require_frozen(["alpha", "beta"])


def test_b08_status_other_than_frozen_or_draft_is_refused_at_load(tmp_path):
    """B08: a typo in status must not read as an unfrozen model that is merely skipped."""
    path = write(tmp_path, '[models.a]\nbackend = "stub"\nstatus = "forzen"\n')
    with pytest.raises(ConfigError, match=r"models\.toml: \[models\.a\]: status must be one of frozen, draft"):
        load_models(path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"backend": "anthropic", "key_env": "K", "echo_reasoning_field": "reasoning_content"}, "openai_compat models"),
        ({"echo_reasoning_field": "content"}, "overwrite a standard message field"),
        ({"echo_reasoning_field": "_reasoning"}, "overwrite a standard message field"),
        ({"echo_reasoning_field": "not a name"}, "plain field name"),
        ({"extra_body": {"max_tokens": 5}}, "extra_body sets 'max_tokens', which the adapter sets itself"),
        ({"extra_body": {"messages": []}}, "extra_body sets 'messages'"),
        ({"sampling": {"seed": 1}}, "sampling sets 'seed', which the adapter sets itself"),
        ({"sampling": {"stream": True}}, "streaming is not supported"),
        ({"extra_body": {"stream": True}}, "streaming is not supported"),
        ({"sampling": {"temperature": 0.1}, "extra_body": {"temperature": 0.9}}, "one would silently win"),
        ({"backend": "anthropic", "key_env": "K", "sampling": {"seed": 1}}, "sampling keys seed are not accepted"),
        (
            {"backend": "anthropic", "key_env": "K", "sampling": {"frequency_penalty": 1}},
            "sampling keys frequency_penalty are not accepted",
        ),
        ({"prompt_cache_ttl": "1h"}, "applies to anthropic models only"),
        ({"backend": "anthropic", "key_env": "K", "prompt_cache_ttl": "2d"}, "prompt_cache_ttl must be one of"),
        ({"max_output_tokens": 0}, "max_output_tokens must be a positive integer"),
        ({"context_tokens": -5}, "context_tokens must be a positive integer"),
        ({"max_output_tokens": 9000, "context_tokens": 8000}, "smaller than the context window"),
        ({"backend": "anthropic"}, "an anthropic model needs key_env or key_file"),
        ({"key_env": "A", "key_file": "/f"}, "key_env or key_file, not both"),
        ({"key_env": "sk-live-not-a-variable-name"}, "name of an environment variable, never a key"),
        ({"base_url": None}, "base_url must be an http or https URL"),
        ({"base_url": "ftp://llm.example.invalid"}, "base_url must be an http or https URL"),
        ({"base_url": "https://user:pw@llm.example.invalid/v1"}, "must not carry credentials"),
        ({"backend": "stub", "base_url": "https://llm.example.invalid"}, "a stub model takes no base_url"),
        ({"backend": "stub", "base_url": None, "sampling": {"temperature": 0}}, "stub model takes no sampling"),
        ({"backend": "Gate way"}, "backend must be one of openai_compat, anthropic, stub, or the plain lower-case"),
        ({"backend": "toy_relay", "base_url": "ftp://x.invalid"}, "base_url must be an http or https URL"),
        ({"backend": "toy_relay", "key_env": "A", "key_file": "/f"}, "key_env or key_file, not both"),
        ({"backend": "toy_relay", "echo_reasoning_field": "x"}, "echo_reasoning_field applies to openai_compat"),
        ({"key": "bad key"}, "key must be letters, digits"),
        ({"api_id": " "}, "api_id must not be empty"),
    ],
)
def test_b16_an_incompatible_combination_is_refused_at_configuration(overrides, message):
    """B16: each incompatible per-model rule raises when the entry is built, before any request can exist."""
    with pytest.raises(RuleViolation, match=message):
        spec(**overrides)


def test_b16_an_unknown_backend_is_said_before_a_missing_api_id_naming_the_factory_that_would_serve_it(tmp_path):
    """B16: a plugin's factory may serve any plain backend name, so the entry is not refused for it; but when the entry
    has a problem, that the core does not build its backend comes first, so a missing api_id does not hide it."""
    path = write(tmp_path, '[models.toy-model]\nbackend = "toy"\n')
    with pytest.raises(ConfigError) as caught:
        load_models(path)
    assert str(caught.value) == (
        f"{path}: [models.toy-model]: key 'backend' names 'toy', a backend the core does not build (openai_compat, "
        "anthropic, stub); the plugin must supply a backend_factory that serves it; key 'api_id' is required"
    )


@pytest.mark.parametrize(
    ("entry", "first", "last"),
    [
        (
            'backend = "openai_compta"\napi_id = "x"\nbase_url = "ftp://x.invalid"',
            "key 'backend' names 'openai_compta' (did you mean 'openai_compat'?), a backend the core does not build",
            "base_url must be an http or https URL",
        ),
        (
            'backend = "OpenAI_Compat"',
            "backend must be one of openai_compat, anthropic, stub, or the plain lower-case name of a backend the "
            "study's plugin serves, got 'OpenAI_Compat' (did you mean 'openai_compat'?)",
            "key 'api_id' is required",
        ),
    ],
)
def test_b16_a_backend_name_close_to_a_cores_is_said_first_with_the_name_it_was_likely_meant_to_be(
    tmp_path, entry, first, last
):
    """B16: a misspelt core backend is said first, before a rule or a missing key, with the closest core backend."""
    with pytest.raises(ConfigError) as caught:
        load_models(write(tmp_path, f"[models.m]\n{entry}\n"))
    problems = str(caught.value).split(": [models.m]: ", 1)[1].split("; ")
    assert problems[0].startswith(first)
    assert problems[-1].startswith(last)


@pytest.mark.parametrize(
    ("entry", "then"),
    [
        ("api_id = 5", "key 'api_id' must be"),
        ('api_id = "x"\nmax_output_tokens = "many"', "key 'max_output_tokens' must be"),
        ('api_id = "x"\ncolour = "blue"', "key 'colour' is not a known key"),
        ('api_id = "x"\n[models.m.price]\ninput_per_mtok = "cheap"', "key 'input_per_mtok' must be a number"),
    ],
    ids=["type", "integer", "unknown-key", "price"],
)
def test_b16_the_backend_and_its_suggestion_come_first_whatever_key_fails_to_read(tmp_path, entry, then):
    """B16: a key that cannot even be read (a wrong type, an unknown key, a bad price) does not hide that the core does
    not build the backend, or the core backend it was likely meant to be."""
    path = write(tmp_path, f'[models.m]\nbackend = "openai_compta"\n{entry}\n')
    with pytest.raises(ConfigError) as caught:
        load_models(path)
    message = str(caught.value)
    first = f"{path}: [models.m]: key 'backend' names 'openai_compta' (did you mean 'openai_compat'?), a backend"
    assert message.startswith(first)
    assert then in message.split("backend_factory that serves it; ", 1)[1]


def test_b16_a_backend_of_the_study_s_own_is_an_entry_the_core_does_not_build_and_needs_no_url():
    """B16: a plain lower-case backend name the core does not know is accepted at the entry, without a URL, so the
    study's plugin can serve it; the core's rules for its own adapters do not apply, and the plugin is refused at load
    without a factory (tests/study/test_backend_factory.py)."""
    own = spec(backend="toy_relay", base_url=None, sampling={"temperature": 0})
    assert (own.backend, own.core_backend, own.seed_supported) == ("toy_relay", False, False)
    assert spec().core_backend
    assert spec(backend="stub", base_url=None).core_backend


def test_b16_all_broken_rules_of_an_entry_are_reported_together():
    """B16: All broken rules of an entry are reported together."""
    with pytest.raises(RuleViolation) as caught:
        spec(backend="anthropic", key_env="K", echo_reasoning_field="x", prompt_cache_ttl="2d", max_output_tokens=-1)
    assert len(caught.value.problems) == 3
    assert str(caught.value).startswith("model 'm': ")


def test_b16_a_bad_entry_in_a_file_names_the_file_and_the_key(tmp_path):
    """B16: A bad entry in a file names the file and the key."""
    path = write(
        tmp_path,
        """
        [models.good]
        backend = "stub"
        [models.bad]
        backend = "anthropic"
        api_id = "bad-large"
        base_url = "https://api.example.invalid"
        key_env = "K"
        echo_reasoning_field = "reasoning_content"
        """,
    )
    with pytest.raises(ConfigError, match=r"models\.toml: \[models\.bad\]: echo_reasoning_field applies"):
        load_models(path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", r"key 'models' is required and must hold at least one model"),
        ("[models]\n", r"key 'models' is required and must hold at least one model"),
        ("[other]\nx = 1\n", r"key 'other' is not a known key; known keys: models"),
        ("[models]\nalpha = 3\n", r"\[models\]: key 'alpha' must be a table, got int"),
        ('[models.a]\nbackend = "stub"\napi_key = "sk-not-here"\n', r"\[models\.a\]: key 'api_key' is not a known key"),
        ('[models.a]\nbackend = "stub"\nsampling = 3\n', r"\[models\.a\]: key 'sampling' must be a table"),
        ("[models.a]\nstatus = 'frozen'\n", r"\[models\.a\]: key 'backend' is required"),
        (
            '[models.a]\nbackend = "anthropic"\napi_id = "x"\nkey_env = "K"\n',
            r"base_url must be an http or https URL",
        ),
        (
            '[models.a]\nbackend = "openai_compat"\nbase_url = "https://x.invalid"\n',
            r"\[models\.a\]: key 'api_id' is required",
        ),
        ('[models.a]\nbackend = "stub"\nmax_output_tokens = "big"\n', r"key 'max_output_tokens' must be an integer"),
    ],
)
def test_a_bad_file_is_refused_with_the_file_and_the_key(tmp_path, text, message):
    with pytest.raises(ConfigError, match=rf"models\.toml: .*{message}|models\.toml: {message}"):
        load_models(write(tmp_path, text))


def test_a_secret_written_in_the_table_is_refused_without_being_echoed(tmp_path):
    path = write(tmp_path, '[models.a]\nbackend = "stub"\napi_key = "sk-supersecret-value"\n')
    with pytest.raises(ConfigError) as caught:
        load_models(path)
    assert "supersecret" not in str(caught.value)


def test_b11_the_output_limit_is_the_lower_of_the_study_and_the_model():
    """B11: max_tokens is the study's value, or the model's own lower maximum."""
    capped = spec(max_output_tokens=4096)
    assert capped.output_tokens(16384) == 4096
    assert capped.output_tokens(1024) == 1024
    assert spec().output_tokens(16384) == 16384


def test_the_key_is_read_from_the_environment_when_asked_not_when_loaded(tmp_path):
    table = load_models(write(tmp_path, TABLE))  # TEST_ALPHA_KEY is not set, and loading does not mind
    alpha = table.resolve("alpha")
    assert alpha.read_key({"TEST_ALPHA_KEY": "  value-1  "}) == "value-1"
    with pytest.raises(ConfigError, match="environment variable TEST_ALPHA_KEY is not set or is empty"):
        alpha.read_key({})
    with pytest.raises(ConfigError, match="TEST_ALPHA_KEY"):
        table.require_keys(["alpha"], env={"TEST_ALPHA_KEY": " "})
    table.require_keys(["alpha", "scripted"], env={"TEST_ALPHA_KEY": "v"})


def test_the_key_is_read_from_a_file(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("file-key\n", encoding="utf-8")
    assert spec(key_file=str(key_file)).read_key() == "file-key"
    with pytest.raises(ConfigError, match="cannot be read") as caught:
        spec(key_file=str(tmp_path / "absent")).read_key()
    assert "file-key" not in str(caught.value)
    key_file.write_text(" \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        spec(key_file=str(key_file)).read_key()


def test_a_model_with_no_key_reference_reads_none():
    assert spec().read_key({}) is None


def test_client_settings_refuse_values_that_cannot_work():
    assert ClientSettings().max_retries == 8
    with pytest.raises(ConfigError, match="retry_jitter must be between 0 and 1"):
        ClientSettings(retry_jitter=1.5)
    with pytest.raises(ConfigError, match="retry_base_s must be positive and no larger than retry_max_s"):
        ClientSettings(retry_base_s=10.0, retry_max_s=5.0)
    with pytest.raises(ConfigError, match="max_retries must not be negative"):
        ClientSettings(max_retries=-1)
    with pytest.raises(ConfigError, match="timeouts must be positive"):
        ClientSettings(connect_timeout_s=0)
