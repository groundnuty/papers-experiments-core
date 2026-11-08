"""Building the backend a model entry names: B08 (frozen before any request), keys read at use time, C14 (override)."""

import asyncio
import dataclasses

import pytest

from expcore.config.loading import ConfigError
from expcore.models.adapters import AnthropicAdapter, OpenAICompatAdapter, StubAdapter
from expcore.models.factory import build_backend


def test_b08_a_model_that_is_not_frozen_raises_before_anything_is_built_or_sent(make_spec, limits, fake_client):
    """B08: no client is touched, so no request can go out."""
    client = fake_client()
    with pytest.raises(ConfigError, match="settings are 'draft', not 'frozen'"):
        build_backend(make_spec(status="draft"), seed=1, limits=limits, client=client)
    assert client.requests == []


def test_b08_a_pilot_may_ask_for_an_unfrozen_model(make_spec, limits, fake_client):
    """B08: A pilot may ask for an unfrozen model."""
    backend = build_backend(
        make_spec(status="draft"), seed=1, limits=limits, client=fake_client(), require_frozen=False
    )
    assert isinstance(backend, OpenAICompatAdapter)


def test_the_entrys_backend_name_picks_the_adapter(make_spec, limits, fake_client):
    env = {"TEST_CLAUDE_KEY": "k"}
    assert isinstance(build_backend(make_spec(), seed=1, limits=limits, client=fake_client()), OpenAICompatAdapter)
    claude = make_spec(backend="anthropic")
    assert isinstance(
        build_backend(claude, seed=1, limits=limits, client=fake_client(api="anthropic"), env=env), AnthropicAdapter
    )
    stub = make_spec(backend="stub", base_url=None)
    built = build_backend(stub, seed=3, limits=limits, script=["hi"])
    assert isinstance(built, StubAdapter)
    assert built.info()["model"] == "test-model"
    assert built.output_limit == 4096


def test_the_key_is_read_when_the_backend_is_built_and_a_missing_one_names_the_variable(make_spec, limits):
    claude = make_spec(backend="anthropic")
    with pytest.raises(ConfigError, match="environment variable TEST_CLAUDE_KEY is not set"):
        build_backend(claude, seed=1, limits=limits, env={})
    backend = build_backend(claude, seed=1, limits=limits, env={"TEST_CLAUDE_KEY": "key-from-env"})
    assert backend._client.api_key == "key-from-env"
    asyncio.run(backend.aclose())


def test_a_model_with_no_key_reference_gets_a_placeholder_so_the_sdk_never_reads_the_environment(
    make_spec, limits, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-that-must-not-be-used")
    backend = build_backend(make_spec(), seed=1, limits=limits, env={})
    assert backend._client.api_key == "none"
    asyncio.run(backend.aclose())


def test_c14_a_base_url_override_is_the_one_the_backend_and_its_record_use(make_spec, limits):
    """C14: the endpoint the campaign admitted is the one the run uses."""
    backend = build_backend(make_spec(), seed=1, limits=limits, base_url="https://admitted.example.invalid/v1")
    assert backend.info()["base_url"] == "https://admitted.example.invalid/v1"
    assert str(backend._client.base_url).startswith("https://admitted.example.invalid/v1")
    asyncio.run(backend.aclose())


THINKING = {"thinking": {"type": "enabled", "budget_tokens": 4096}}


def test_b16_a_thinking_budget_not_below_the_effective_output_limit_is_refused_naming_the_model(
    make_spec, limits, fake_client
):
    """B16: budget 4096, model maximum 8192, study cap 2048: every request would carry max_tokens 2048."""
    claude = make_spec(backend="anthropic", max_output_tokens=8192, extra_body=THINKING)
    client = fake_client(api="anthropic")
    study = dataclasses.replace(limits, max_output_tokens=2048)
    with pytest.raises(ConfigError) as caught:
        build_backend(claude, seed=1, limits=study, client=client, env={"TEST_CLAUDE_KEY": "k"})
    message = str(caught.value)
    assert message.startswith("model 'test-model': ")
    assert "budget_tokens (4096) must be smaller than the effective output limit (2048)" in message
    assert "the lower of the study's max_output_tokens (2048) and the model's (8192)" in message
    assert client.requests == []


def test_b16_a_budget_equal_to_the_effective_limit_is_refused_whether_or_not_the_model_sets_a_maximum(
    make_spec, limits
):
    """B16: With no maximum on the entry, the study's own limit is the one the budget must stay under."""
    env = {"TEST_CLAUDE_KEY": "k"}
    capped = make_spec(backend="anthropic", max_output_tokens=8192, extra_body=THINKING)
    with pytest.raises(ConfigError, match=r"effective output limit \(4096\).*the model's \(8192\)"):
        build_backend(capped, seed=1, limits=limits, env=env)
    alone = make_spec(backend="anthropic", extra_body=THINKING)
    with pytest.raises(ConfigError, match=r"effective output limit \(4096\).*the model's \(unset\)"):
        build_backend(alone, seed=1, limits=limits, env=env)


def test_b16_a_thinking_budget_below_the_effective_output_limit_builds(make_spec, limits, fake_client):
    """B16: 4096 under 8192 builds, for a study run and a pilot alike."""
    roomy = dataclasses.replace(limits, max_output_tokens=8192)
    claude = make_spec(backend="anthropic", max_output_tokens=8192, extra_body=THINKING, status="draft")
    env = {"TEST_CLAUDE_KEY": "k"}
    backend = build_backend(
        claude, seed=1, limits=roomy, client=fake_client(api="anthropic"), env=env, require_frozen=False
    )
    assert isinstance(backend, AnthropicAdapter)
    assert backend.output_limit == 8192


def test_b16_the_thinking_rule_is_checked_for_a_pilot_too_and_for_no_other_backend(make_spec, limits, fake_client):
    """B16: A pilot may run unfrozen settings but not a request that cannot be sent; other backends have no rule."""
    claude = make_spec(backend="anthropic", extra_body=THINKING, status="draft")
    with pytest.raises(ConfigError, match="effective output limit"):
        build_backend(claude, seed=1, limits=limits, env={"TEST_CLAUDE_KEY": "k"}, require_frozen=False)
    same_extras = make_spec(extra_body={"thinking": {"type": "enabled", "budget_tokens": 99999}})
    assert isinstance(build_backend(same_extras, seed=1, limits=limits, client=fake_client()), OpenAICompatAdapter)
