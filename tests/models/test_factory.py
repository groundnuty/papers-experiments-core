"""Building the backend a model entry names: B08 (frozen before any request), keys read at use time, C14 (override)."""

import asyncio

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
